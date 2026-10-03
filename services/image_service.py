"""Image service for downloading product images and uploading them to R2"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import boto3
import requests
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from mysql.connector import Error as MySQLError

from config.database import DatabaseManager
from config.settings import settings

logger = logging.getLogger(__name__)

# R2 key prefix. Kept as "starter" so existing object paths stay valid.
R2_FOLDER = "starter"
IMAGE_WORKERS = 20
MAX_DOWNLOAD_RETRIES = 3

CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

TRANSIENT_ERRORS = (requests.RequestException, OSError)


class ImageService:
    """Downloads product images and mirrors them to R2"""

    def __init__(self):
        config = Config(max_pool_connections=50)
        self.s3_client = boto3.client(
            "s3",
            endpoint_url=settings.R2_ENDPOINT_URL,
            aws_access_key_id=settings.R2_ACCESS_KEY_ID,
            aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
            region_name="auto",
            config=config,
        )
        self.bucket_name = settings.R2_BUCKET_NAME
        self.temp_dir = settings.BASE_DIR / "temp_images"
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    def _is_r2_url(self, url: str) -> bool:
        """Whether a URL already points at our R2 bucket"""
        if not settings.R2_PUBLIC_URL:
            return False
        return bool(url) and url.startswith(settings.R2_PUBLIC_URL)

    def _download(self, url: str, temp_path: Path, timeout: int = 360) -> None:
        """Stream a URL to disk; raises on any failure"""
        referer = "/".join(url.split("/")[:3]) + "/"
        headers = {"User-Agent": settings.USER_AGENT, "Referer": referer}
        response = requests.get(url, timeout=timeout, stream=True, headers=headers)
        response.raise_for_status()
        with open(temp_path, "wb") as f:
            f.writelines(response.iter_content(chunk_size=8192))

    def download_file(self, url: str, temp_path: Path) -> bool:
        """Download a file, retrying with backoff. Returns False on failure."""
        for attempt in range(MAX_DOWNLOAD_RETRIES):
            try:
                self._download(url, temp_path)
                return True
            except TRANSIENT_ERRORS as e:
                if attempt < MAX_DOWNLOAD_RETRIES - 1:
                    wait_time = 2**attempt
                    logger.warning(
                        f"Download attempt {attempt + 1} failed, retrying in "
                        f"{wait_time}s: {e}"
                    )
                    time.sleep(wait_time)
                else:
                    logger.error(f"Error downloading {url}: {e}")
        return False

    def upload_to_r2(self, local_path: Path, r2_key: str) -> str | None:
        """Upload a file to R2, retrying with backoff. Returns the public URL."""
        content_type = CONTENT_TYPES.get(
            local_path.suffix.lower(), "application/octet-stream"
        )

        for attempt in range(MAX_DOWNLOAD_RETRIES):
            try:
                with open(local_path, "rb") as f:
                    self.s3_client.upload_fileobj(
                        f,
                        self.bucket_name,
                        r2_key,
                        ExtraArgs={"ContentType": content_type},
                    )
                return f"{settings.R2_PUBLIC_URL}/{r2_key}"
            except (BotoCoreError, ClientError, OSError) as e:
                if attempt < MAX_DOWNLOAD_RETRIES - 1:
                    logger.warning(
                        f"Upload attempt {attempt + 1} failed, retrying: {e}"
                    )
                    time.sleep(2**attempt)
                else:
                    logger.error(f"Error uploading to R2: {e}")
        return None

    def _mirror_image(
        self, store_id: int, product_id: int, url: str, filename: str
    ) -> str | None:
        """Download one image and upload it to R2, returning its public URL"""
        temp_path = self.temp_dir / filename
        try:
            if not self.download_file(url, temp_path):
                return None
            return self.upload_to_r2(temp_path, f"{store_id}/images/{filename}")
        finally:
            temp_path.unlink(missing_ok=True)

    def _process_single(self, product: dict) -> tuple[int, str | None]:
        """Mirror the main product image to R2."""
        store_id = product["store_id"]
        product_id = product["id"]
        source_url = product["source_image_url"]

        filename = f"{product_id}_{source_url.split('/')[-1]}"
        main_url = self._mirror_image(store_id, product_id, source_url, filename)
        return product_id, main_url

    def _process_batch(self, products: list[dict]) -> tuple[int, int]:
        """Mirror a batch of products in parallel; returns (success, failed)"""
        success = failed = 0

        with ThreadPoolExecutor(max_workers=IMAGE_WORKERS) as executor:
            futures = [executor.submit(self._process_single, p) for p in products]

            for future in as_completed(futures):
                try:
                    product_id, r2_url = future.result()
                except (MySQLError, OSError, ValueError) as e:
                    logger.error(f"Unexpected error in image processing thread: {e}")
                    failed += 1
                    continue

                if not r2_url:
                    failed += 1
                    continue

                try:
                    with DatabaseManager.get_connection() as conn:
                        cursor = conn.cursor()
                        cursor.execute(
                            """UPDATE products
                            SET image_url = %s, updated_at = updated_at
                            WHERE id = %s""",
                            (r2_url, product_id),
                        )
                except MySQLError as e:
                    logger.error(f"Error updating image URLs for {product_id}: {e}")
                    failed += 1
                    continue

                success += 1
                if success % 100 == 0:
                    logger.info(f"Processed {success} product images...")

        return success, failed

    def process_images(self) -> None:
        """Mirror every product's images to R2 in place"""
        with DatabaseManager.get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                """SELECT p.id, p.store_id, p.image_url, p.source_image_url,
                FROM products p
                WHERE p.source_image_url IS NOT NULL
                AND p.source_image_url != ''
                AND p.source_image_url NOT LIKE %s
                AND (
                    p.image_url IS NULL
                    OR p.image_url = ''
                    OR p.image_url NOT LIKE %s
                )""",
                (f"{settings.R2_PUBLIC_URL}%", f"{settings.R2_PUBLIC_URL}%"),
            )
            products = cursor.fetchall()

        if not products:
            logger.info("No product images to process")
            return

        logger.info(f"Processing images for {len(products)} products...")
        success, failed = self._process_batch(products)
        logger.info(f"Image processing complete: {success} success, {failed} failed")

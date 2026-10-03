"""CSV generation service for subscription data"""

import csv
import json
import logging
import shutil
from pathlib import Path
from uuid import uuid4

import requests
from mysql.connector import Error as MySQLError

from config.database import DatabaseManager
from config.settings import settings
from utils.timeutil import now

logger = logging.getLogger(__name__)

FIELD_NAMES = [
    "id",
    "store_id",
    "store_name",
    "product_id",
    "product_name",
    "product_url",
    "image_url",
    "product_images",
    "current_price",
    "original_price",
    "stock_status",
    "is_active",
    "last_synced_at",
    "created_at",
    "updated_at",
    "has_variants",
    "variants",
    "short_description",
    "description",
    "attributes",
    "categories",
]


class CSVService:
    """Service for generating CSV files for subscriptions"""

    BASE_CSV_DIR = settings.BASE_DIR / "csv_exports"

    @staticmethod
    def generate_csv_for_subscription(subscription_id: int) -> str | None:
        """Generate a CSV file for a subscription, returning its path"""
        try:
            with DatabaseManager.get_connection() as conn:
                cursor = conn.cursor(dictionary=True)

                cursor.execute(
                    """SELECT buyer_domain
                    FROM api_subscriptions
                    WHERE id = %s AND expires_at > NOW()""",
                    (subscription_id,),
                )
                subscription = cursor.fetchone()

                if not subscription:
                    logger.warning(f"No active subscription found: {subscription_id}")
                    return None

                buyer_domain = subscription["buyer_domain"]

                cursor.execute(
                    """SELECT s.store_id, s.last_complete_product_scrape_at
                    FROM subscription_permissions sp
                    JOIN stores s ON s.store_id = sp.store_id
                    WHERE sp.subscription_id = %s
                    ORDER BY s.store_id""",
                    (subscription_id,),
                )
                selected_stores = cursor.fetchall()

                if not selected_stores:
                    logger.warning(
                        f"No selected stores available for subscription {subscription_id}"
                    )
                    return None

                eligible_store_ids = [
                    row["store_id"]
                    for row in selected_stores
                    if row["last_complete_product_scrape_at"] is not None
                ]
                excluded_store_ids = [
                    row["store_id"]
                    for row in selected_stores
                    if row["last_complete_product_scrape_at"] is None
                ]

                if excluded_store_ids:
                    logger.warning(
                        f"Subscription {subscription_id}: excluding stores with no "
                        f"recorded complete scrape: {excluded_store_ids}"
                    )

                if not eligible_store_ids:
                    logger.warning(
                        f"Subscription {subscription_id}: no stores eligible for export"
                    )
                    return None

                placeholders = ", ".join(["%s"] * len(eligible_store_ids))

                query = f"""
                    SELECT
                        p.id, p.store_id, p.store_name,
                        p.external_product_id as product_id,
                        p.product_name, p.product_url, p.image_url,
                        p.product_images,
                        p.current_price, p.original_price, p.stock_status, p.is_active,
                        p.last_synced_at, p.created_at, p.updated_at,
                        p.has_variants, p.variants,
                        p.short_description, p.description, p.attributes,
                        GROUP_CONCAT(DISTINCT c.category_id
                            ORDER BY c.category_id SEPARATOR ', ') as categories
                    FROM products p
                    LEFT JOIN product_categories pc ON p.id = pc.product_id
                    LEFT JOIN categories c ON pc.category_id = c.category_id
                    WHERE p.image_url IS NOT NULL AND p.image_url != ''
                    AND p.is_active = 1
                    AND p.stock_status = 'in_stock'
                    AND p.store_id IN ({placeholders})
                    GROUP BY p.id
                """
                cursor.execute(query, tuple(eligible_store_ids))
                products = cursor.fetchall()

            if not products:
                logger.info(
                    f"No in-stock products for subscription {subscription_id}; "
                    "generating a header-only CSV"
                )

            csv_dir = Path(CSVService.BASE_CSV_DIR) / f"subscription_{subscription_id}"
            csv_dir.mkdir(parents=True, exist_ok=True)

            timestamp = now().strftime("%Y%m%d_%H%M%S")
            csv_path = csv_dir / (
                f"subscription_{subscription_id}_{timestamp}_{uuid4().hex}.csv"
            )

            metadata_path = csv_path.with_suffix(".json")

            try:
                with open(csv_path, "w", newline="", encoding="utf-8") as csvfile:
                    writer = csv.DictWriter(csvfile, fieldnames=FIELD_NAMES)
                    writer.writeheader()
                    writer.writerows(products)

                metadata_path.write_text(
                    json.dumps(
                        {
                            "subscription_id": subscription_id,
                            "store_ids": eligible_store_ids,
                            "excluded_store_ids": excluded_store_ids,
                        }
                    ),
                    encoding="utf-8",
                )
            except (OSError, ValueError, TypeError, csv.Error) as e:
                for artifact_path in (csv_path, metadata_path):
                    try:
                        artifact_path.unlink(missing_ok=True)
                    except OSError:
                        logger.exception(
                            "Could not remove incomplete export file: %s",
                            artifact_path,
                        )

                raise ValueError(
                    f"Could not write export for subscription {subscription_id}"
                ) from e

            logger.info(
                f"Generated CSV for subscription {subscription_id}: "
                f"{len(products)} products"
            )
            return str(csv_path)

        except (MySQLError, OSError, ValueError) as e:
            logger.error(
                f"Error generating CSV for subscription {subscription_id}: "
                f"{type(e).__name__}"
            )
            raise

    @staticmethod
    def upload_csv(csv_path: str, subscription_id: int) -> bool:
        """Upload an inventory CSV with its eligible store IDs."""
        try:
            metadata_path = Path(csv_path).with_suffix(".json")
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

            if not isinstance(metadata, dict):
                raise TypeError("CSV metadata must be a dictionary")

            if (
                type(metadata.get("subscription_id")) is not int
                or metadata["subscription_id"] != subscription_id
            ):
                raise ValueError("CSV metadata subscription mismatch")

            store_ids = metadata.get("store_ids")
            if (
                not isinstance(store_ids, list)
                or not store_ids
                or any(
                    type(store_id) is not int or store_id <= 0 for store_id in store_ids
                )
            ):
                raise ValueError("CSV metadata has invalid store IDs")

            with DatabaseManager.get_connection() as conn:
                cursor = conn.cursor(dictionary=True)
                try:
                    cursor.execute(
                        """SELECT w.consumer_key, w.consumer_secret, s.buyer_domain
                        FROM woocommerce_credentials w
                        JOIN api_subscriptions s ON s.id = w.subscription_id
                        WHERE w.subscription_id = %s""",
                        (subscription_id,),
                    )
                    row = cursor.fetchone()
                finally:
                    cursor.close()

                if not row:
                    raise ValueError(
                        f"No credentials found for subscription {subscription_id}"
                    )

            api_url = (
                f"{row['buyer_domain'].rstrip('/')}" "/wp-json/product-sync/v1/products"
            )

            with open(csv_path, "rb") as csv_file:
                response = requests.post(
                    api_url,
                    params={
                        "consumer_key": row["consumer_key"],
                        "consumer_secret": row["consumer_secret"],
                    },
                    files={"file": csv_file},
                    data={"store_ids": json.dumps(store_ids)},
                    timeout=60,
                )

            try:
                response.raise_for_status()
            finally:
                response.close()

            logger.info(
                f"CSV uploaded successfully for subscription {subscription_id}: "
                f"{len(store_ids)} stores"
            )
            return True

        except (
            MySQLError,
            OSError,
            ValueError,
            TypeError,
            requests.RequestException,
        ) as e:
            logger.error(
                f"Error uploading CSV for subscription {subscription_id}: "
                f"{type(e).__name__}"
            )
            return False

    @staticmethod
    def cleanup_old_csvs() -> None:
        """Delete all old CSV files before a new scraper run"""
        try:
            csv_base = Path(CSVService.BASE_CSV_DIR)
            if csv_base.exists():
                shutil.rmtree(csv_base)
                logger.info("Cleaned up old CSV files")
            csv_base.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.error(f"Error cleaning up CSVs: {e}")

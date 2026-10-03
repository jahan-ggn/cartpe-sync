"""CSV generation service for subscription data"""

import csv
import logging
import re
import shutil
from pathlib import Path

import requests
from mysql.connector import Error as MySQLError
from utils.timeutil import now

from config.database import DatabaseManager
from config.settings import settings

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
                    """SELECT store_id FROM subscription_permissions
                    WHERE subscription_id = %s""",
                    (subscription_id,),
                )
                if not cursor.fetchall():
                    logger.warning(
                        f"No stores selected for subscription {subscription_id}"
                    )
                    return None

                query = """
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
                    JOIN subscription_permissions sp
                        ON p.store_id = sp.store_id AND sp.subscription_id = %s
                    LEFT JOIN product_categories pc ON p.id = pc.product_id
                    LEFT JOIN categories c ON pc.category_id = c.category_id
                    WHERE p.image_url IS NOT NULL AND p.image_url != ''
                    AND p.is_active = 1
                    AND p.stock_status = 'in_stock'
                    GROUP BY p.id
                """
                cursor.execute(query, (subscription_id,))
                products = cursor.fetchall()

            if not products:
                logger.info(
                    f"No in-stock products for subscription {subscription_id}; "
                    "generating a header-only CSV"
                )

            domain_clean = re.sub(r"^https?://", "", buyer_domain)
            csv_dir = Path(CSVService.BASE_CSV_DIR) / domain_clean
            csv_dir.mkdir(parents=True, exist_ok=True)

            timestamp = now().strftime("%Y%m%d_%H%M%S")
            csv_path = csv_dir / f"subscription_{subscription_id}_{timestamp}.csv"

            with open(csv_path, "w", newline="", encoding="utf-8") as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=FIELD_NAMES)
                writer.writeheader()
                writer.writerows(products)

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
        """Upload a full CSV to WordPress in a single request"""
        try:
            with DatabaseManager.get_connection() as conn:
                cursor = conn.cursor(dictionary=True)
                cursor.execute(
                    """SELECT w.consumer_key, w.consumer_secret, s.buyer_domain
                    FROM woocommerce_credentials w
                    JOIN api_subscriptions s ON s.id = w.subscription_id
                    WHERE w.subscription_id = %s""",
                    (subscription_id,),
                )
                row = cursor.fetchone()
                if not row:
                    raise ValueError(
                        f"No credentials found for subscription {subscription_id}"
                    )

            api_url = (
                f"{row['buyer_domain']}/wp-json/product-sync/v1/products"
                f"?consumer_key={row['consumer_key']}"
                f"&consumer_secret={row['consumer_secret']}"
            )

            with open(csv_path, "rb") as f:
                response = requests.post(api_url, files={"file": f}, timeout=60)
            response.raise_for_status()

            logger.info(f"CSV uploaded successfully for subscription {subscription_id}")
            return True

        except (
            MySQLError,
            OSError,
            ValueError,
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

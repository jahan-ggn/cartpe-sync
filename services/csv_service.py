"""CSV generation service for subscription data"""

import csv
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
    "brand_id",
    "brand_name",
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
        """Export current active, in-stock inventory for selected stores."""
        try:
            with DatabaseManager.get_connection() as conn:
                cursor = conn.cursor(dictionary=True)
                try:
                    cursor.execute(
                        """SELECT id
                        FROM api_subscriptions
                        WHERE id = %s AND expires_at > NOW()""",
                        (subscription_id,),
                    )
                    if cursor.fetchone() is None:
                        logger.warning(
                            "No active subscription found: %s",
                            subscription_id,
                        )
                        return None

                    cursor.execute(
                        """SELECT sp.store_id
                        FROM subscription_permissions sp
                        JOIN stores s ON s.store_id = sp.store_id
                        WHERE sp.subscription_id = %s
                        ORDER BY sp.store_id""",
                        (subscription_id,),
                    )
                    store_ids = [row["store_id"] for row in cursor.fetchall()]

                    if not store_ids:
                        logger.warning(
                            "No selected stores available for subscription %s",
                            subscription_id,
                        )
                        return None

                    placeholders = ", ".join(["%s"] * len(store_ids))
                    cursor.execute(
                        f"""
                        SELECT
                            p.id,
                            p.store_id,
                            p.store_name,
                            p.external_product_id AS product_id,
                            p.product_name,
                            p.product_url,
                            p.image_url,
                            p.product_images,
                            p.current_price,
                            p.original_price,
                            p.stock_status,
                            p.is_active,
                            p.last_synced_at,
                            p.created_at,
                            p.updated_at,
                            p.has_variants,
                            p.variants,
                            p.brand_id,
                            b.brand_name,
                            p.short_description,
                            p.description,
                            p.attributes,
                            GROUP_CONCAT(
                                DISTINCT c.category_id
                                ORDER BY c.category_id
                                SEPARATOR ', '
                            ) AS categories
                        FROM products p
                        LEFT JOIN brands b ON p.brand_id = b.brand_id
                        LEFT JOIN product_categories pc ON p.id = pc.product_id
                        LEFT JOIN categories c ON pc.category_id = c.category_id
                        WHERE p.store_id IN ({placeholders})
                            AND p.is_active = 1
                            AND p.stock_status = 'in_stock'
                            AND p.image_url IS NOT NULL
                            AND p.image_url != ''
                            AND p.product_name IS NOT NULL
                            AND TRIM(p.product_name) != ''
                            AND p.product_url IS NOT NULL
                            AND TRIM(p.product_url) != ''
                        GROUP BY p.id
                        ORDER BY p.store_id, p.id
                        """,
                        tuple(store_ids),
                    )
                    products = cursor.fetchall()
                finally:
                    cursor.close()

            if not products:
                logger.info(
                    "Skipping CSV export for subscription %s: "
                    "no exportable products",
                    subscription_id,
                )
                return None

            csv_dir = Path(CSVService.BASE_CSV_DIR) / f"subscription_{subscription_id}"
            csv_dir.mkdir(parents=True, exist_ok=True)

            timestamp = now().strftime("%Y%m%d_%H%M%S")
            csv_path = csv_dir / (
                f"subscription_{subscription_id}_{timestamp}_{uuid4().hex}.csv"
            )

            try:
                with open(csv_path, "w", newline="", encoding="utf-8") as csvfile:
                    writer = csv.DictWriter(
                        csvfile,
                        fieldnames=FIELD_NAMES,
                    )
                    writer.writeheader()
                    writer.writerows(products)
            except (OSError, ValueError, TypeError, csv.Error) as e:
                try:
                    csv_path.unlink(missing_ok=True)
                except OSError:
                    logger.exception(
                        "Could not remove incomplete CSV: %s",
                        csv_path,
                    )
                raise ValueError(
                    f"Could not write export for subscription {subscription_id}"
                ) from e

            logger.info(
                "Generated CSV for subscription %s: %s products",
                subscription_id,
                len(products),
            )
            return str(csv_path)

        except (MySQLError, OSError, ValueError) as e:
            logger.error(
                "Error generating CSV for subscription %s: %s",
                subscription_id,
                type(e).__name__,
            )
            raise

    @staticmethod
    def upload_csv(csv_path: str, subscription_id: int) -> bool:
        """Upload only the inventory CSV."""
        try:
            # Do not upload an empty or header-only CSV.
            with open(csv_path, "r", newline="", encoding="utf-8") as csv_file:
                reader = csv.DictReader(csv_file)
                if next(reader, None) is None:
                    logger.info(
                        f"Skipping upload for subscription {subscription_id}: "
                        "CSV contains no product rows"
                    )
                    return False

            with DatabaseManager.get_connection() as conn:
                cursor = conn.cursor(dictionary=True)
                try:
                    cursor.execute(
                        """SELECT w.consumer_key, w.consumer_secret,
                                  s.buyer_domain
                        FROM woocommerce_credentials w
                        JOIN api_subscriptions s
                            ON s.id = w.subscription_id
                        WHERE w.subscription_id = %s
                            AND s.expires_at > NOW()""",
                        (subscription_id,),
                    )
                    row = cursor.fetchone()
                finally:
                    cursor.close()

            if not row:
                raise ValueError(
                    f"No active subscription with credentials found: "
                    f"{subscription_id}"
                )

            api_url = (
                f"{row['buyer_domain'].rstrip('/')}" "/wp-json/product-sync/v1/products"
            )

            with open(csv_path, "rb") as csv_file, requests.post(
                api_url,
                params={
                    "consumer_key": row["consumer_key"],
                    "consumer_secret": row["consumer_secret"],
                },
                files={
                    "file": (
                        Path(csv_path).name,
                        csv_file,
                        "text/csv",
                    )
                },
                timeout=60,
            ) as response:
                response.raise_for_status()

            logger.info(f"CSV uploaded successfully for subscription {subscription_id}")
            return True

        except (
            MySQLError,
            OSError,
            ValueError,
            TypeError,
            csv.Error,
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

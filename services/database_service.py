"""Database service layer for CRUD operations"""

import json
import logging
import time
from functools import wraps

from mysql.connector import Error as MySQLError

from config.database import DatabaseManager
from config.settings import settings
from utils.timeutil import now

logger = logging.getLogger(__name__)


def retry_on_deadlock(max_retries: int = 3, retry_delay: int = 1):
    """Retry a function when MySQL reports a deadlock (errno 1213)"""

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except MySQLError as e:
                    if e.errno != 1213 or attempt == max_retries - 1:
                        raise
                    wait_time = retry_delay * (2**attempt)
                    logger.warning(
                        f"Deadlock (attempt {attempt + 1}), retrying in {wait_time}s"
                    )
                    time.sleep(wait_time)

        return wrapper

    return decorator


class StoreService:
    """Store-related database operations"""

    @staticmethod
    def get_all_stores(store_type: str | None = None) -> list[dict]:
        """Fetch stores, optionally filtered by type (None returns all)"""
        if store_type:
            query = "SELECT * FROM stores WHERE store_type = %s"
            params = (store_type,)
        else:
            query = "SELECT * FROM stores"
            params = None

        try:
            stores = DatabaseManager.execute_query(query, params, fetch=True)
            logger.info(f"Found {len(stores)} stores in database")
            return stores
        except MySQLError as e:
            logger.error(f"Error fetching stores: {e}")
            return []

    @staticmethod
    def create_store(store_data: dict) -> dict:
        """Create a new store row"""
        query = """
            INSERT INTO stores (store_type, store_name, base_url)
            VALUES (%s, %s, %s)
        """
        params = (
            store_data.get("store_type", "cartpe"),
            store_data["store_name"],
            store_data["base_url"].rstrip("/"),
        )

        with DatabaseManager.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            store_id = cursor.lastrowid

        logger.info(f"Created store: {store_data['store_name']}")
        return {"success": True, "store_id": store_id}


class CategoryService:
    """Category-related database operations"""

    @staticmethod
    def bulk_insert_categories(categories: list[dict]) -> int:
        """Insert categories or refresh existing category details."""
        if not categories:
            return 0

        query = """
            INSERT INTO categories
            (store_id, external_category_id, category_name, category_slug,
            category_url, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                category_name = VALUES(category_name),
                category_slug = VALUES(category_slug),
                category_url = VALUES(category_url)
        """

        data = [
            (
                cat["store_id"],
                cat.get("external_category_id"),
                cat["category_name"],
                cat["category_slug"],
                cat.get("category_url"),
                now(),
            )
            for cat in categories
        ]

        try:
            rows_affected = DatabaseManager.execute_many(query, data)
            logger.info(f"Inserted {rows_affected} categories into database")
            return rows_affected
        except MySQLError as e:
            logger.error(f"Error saving categories: {e}")
            return 0

    @staticmethod
    def get_categories_by_store(store_id: int) -> list[dict]:
        """Get all categories for a specific store"""
        query = "SELECT * FROM categories WHERE store_id = %s"
        try:
            categories = DatabaseManager.execute_query(query, (store_id,), fetch=True)
            logger.info(f"Found {len(categories)} categories for store ID: {store_id}")
            return categories
        except MySQLError as e:
            logger.error(f"Error fetching categories for store {store_id}: {e}")
            return []

    @staticmethod
    def get_all_categories() -> list[dict]:
        """Fetch categories for all stores in one query."""
        return DatabaseManager.execute_query(
            "SELECT * FROM categories",
            fetch=True,
        )


class ProductService:
    """Product-related database operations"""

    @staticmethod
    @retry_on_deadlock(max_retries=3, retry_delay=1)
    def bulk_upsert_products(
        products: list[dict], mark_inactive: tuple[int, int] | None = None
    ) -> dict:
        """Insert or update products, returning change metrics.

        If `mark_inactive` is (store_id, category_id), that category's products
        are deactivated first, within the SAME transaction as the upsert, so
        both commit or both roll back together.
        """
        metrics = {"new": 0, "price_changed": 0, "stock_changed": 0, "total": 0}
        if not products and mark_inactive is None:
            return metrics

        r2_prefix = (
            settings.R2_PUBLIC_URL.rstrip("/") + "/" if settings.R2_PUBLIC_URL else ""
        )

        query = """
            INSERT INTO products
            (store_id, store_name, external_product_id, product_name, product_url,
            image_url, source_image_url, product_images,
            current_price, original_price, has_variants, variants,
            stock_status, is_active, short_description, description, attributes,
            last_synced_at, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
            updated_at = IF(
                product_name <=> VALUES(product_name) AND
                product_url <=> VALUES(product_url) AND
                current_price <=> VALUES(current_price) AND
                original_price <=> VALUES(original_price) AND
                stock_status <=> VALUES(stock_status) AND
                source_image_url <=> VALUES(source_image_url) AND
                has_variants <=> VALUES(has_variants) AND
                variants <=> VALUES(variants) AND
                short_description <=> VALUES(short_description) AND
                description <=> VALUES(description) AND
                attributes <=> VALUES(attributes),
                updated_at,
                VALUES(updated_at)
            ),
            product_name = VALUES(product_name),
            product_url = VALUES(product_url),
            image_url = IF(
                %s != ''
                AND LEFT(image_url, CHAR_LENGTH(%s)) = %s
                AND source_image_url <=> VALUES(source_image_url),
                image_url,
                VALUES(image_url)
            ),
            source_image_url = VALUES(source_image_url),
            product_images = NULL,
            current_price = VALUES(current_price),
            original_price = VALUES(original_price),
            has_variants = VALUES(has_variants),
            variants = VALUES(variants),
            stock_status = VALUES(stock_status),
            is_active = VALUES(is_active),
            short_description = VALUES(short_description),
            description = VALUES(description),
            attributes = VALUES(attributes),
            last_synced_at = VALUES(last_synced_at)
        """

        try:
            with DatabaseManager.get_connection() as conn:
                cursor = conn.cursor(dictionary=True)

                if mark_inactive is not None:
                    ProductService.mark_category_products_inactive(
                        mark_inactive[0], mark_inactive[1], cursor=cursor
                    )
                ids_by_store = {}
                for prod in products:
                    ids_by_store.setdefault(prod["store_id"], set()).add(
                        prod["external_product_id"]
                    )

                existing_by_key = {}
                for store_id, external_ids in ids_by_store.items():
                    external_ids = list(external_ids)

                    for offset in range(0, len(external_ids), 500):
                        batch = external_ids[offset : offset + 500]
                        placeholders = ", ".join(["%s"] * len(batch))

                        cursor.execute(
                            f"""SELECT id, external_product_id, current_price,
                                original_price, stock_status, source_image_url
                            FROM products
                            WHERE store_id = %s
                            AND external_product_id IN ({placeholders})""",
                            (store_id, *batch),
                        )

                        for row in cursor.fetchall():
                            existing_by_key[
                                (store_id, str(row["external_product_id"]))
                            ] = row

                for prod in products:
                    key = (prod["store_id"], str(prod["external_product_id"]))
                    existing = existing_by_key.get(key)
                    attributes = prod.get("attributes")
                    if isinstance(attributes, list):
                        attributes = json.dumps(attributes) if attributes else None

                    if existing is None:
                        metrics["new"] += 1
                    else:
                        if existing["current_price"] != prod.get(
                            "current_price"
                        ) or existing["original_price"] != prod.get("original_price"):
                            metrics["price_changed"] += 1
                        if existing["stock_status"] != prod.get(
                            "stock_status", "in_stock"
                        ):
                            metrics["stock_changed"] += 1

                    metrics["total"] += 1

                    data = (
                        prod["store_id"],
                        prod["store_name"],
                        prod["external_product_id"],
                        prod["product_name"],
                        prod.get("product_url"),
                        prod.get("image_url"),
                        prod.get("source_image_url"),
                        None,
                        prod.get("current_price"),
                        prod.get("original_price"),
                        prod.get("has_variants", False),
                        prod.get("variants"),
                        prod.get("stock_status", "in_stock"),
                        True,
                        prod.get("short_description"),
                        prod.get("description"),
                        attributes,
                        now(),
                        now(),
                        now(),
                    )

                    cursor.execute(
                        query,
                        data + (r2_prefix, r2_prefix, r2_prefix),
                    )

                    # On update, lastrowid is unreliable — reuse the row we fetched
                    product_id = existing["id"] if existing else cursor.lastrowid
                    existing_by_key[key] = {
                        "id": product_id,
                        "current_price": prod.get("current_price"),
                        "original_price": prod.get("original_price"),
                        "stock_status": prod.get("stock_status", "in_stock"),
                        "source_image_url": prod.get("source_image_url"),
                    }

                    if product_id and prod.get("category_id"):
                        cursor.execute(
                            """DELETE FROM product_categories
                            WHERE product_id = %s""",
                            (product_id,),
                        )
                        cursor.execute(
                            """INSERT IGNORE INTO product_categories (product_id, category_id)
                            VALUES (%s, %s)""",
                            (product_id, prod["category_id"]),
                        )

            logger.info(
                f"Upserted {metrics['total']} products "
                f"(new: {metrics['new']}, price: {metrics['price_changed']}, "
                f"stock: {metrics['stock_changed']})"
            )
            return metrics

        except MySQLError as e:
            logger.error(f"Error bulk upserting products: {e}")
            raise

    @staticmethod
    def mark_category_products_inactive(
        store_id: int, category_id: int, cursor=None
    ) -> int:
        """Mark all products in a category as inactive, without bumping updated_at.

        When `cursor` is given the UPDATE runs on it (joining the caller's
        transaction); otherwise it opens its own connection.
        """
        query = """
            UPDATE products p
            JOIN product_categories pc ON p.id = pc.product_id
            SET p.is_active = FALSE, p.updated_at = p.updated_at
            WHERE p.store_id = %s AND pc.category_id = %s
        """
        if cursor is not None:
            cursor.execute(query, (store_id, category_id))
            logger.info(
                f"Marked {cursor.rowcount} products as inactive for category {category_id}"
            )
            return cursor.rowcount
        try:
            rows_affected = DatabaseManager.execute_query(
                query, (store_id, category_id)
            )
            logger.info(
                f"Marked {rows_affected} products as inactive for category {category_id}"
            )
            return rows_affected
        except MySQLError as e:
            logger.error(f"Error marking products inactive: {e}")
            return 0

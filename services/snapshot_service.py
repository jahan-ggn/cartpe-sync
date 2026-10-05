"""Store complete CSV-ready snapshots for each store."""

import json

from config.database import DatabaseManager
from utils.timeutil import now


class SnapshotService:
    @staticmethod
    def save_store_snapshot(store_id: int, products: list[dict]) -> None:
        """Replace a store's snapshot atomically."""

        rows = []
        for product in products:
            if product["store_id"] != store_id:
                raise ValueError("Snapshot contains a different store's product")

            rows.append(
                (
                    store_id,
                    product["id"],
                    json.dumps(product, default=str, ensure_ascii=False),
                )
            )

        with DatabaseManager.get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    """DELETE FROM store_export_snapshot_products
                    WHERE store_id = %s""",
                    (store_id,),
                )

                if rows:
                    cursor.executemany(
                        """INSERT INTO store_export_snapshot_products
                        (store_id, product_id, row_data)
                        VALUES (%s, %s, %s)""",
                        rows,
                    )

                cursor.execute(
                    """INSERT INTO store_export_snapshots
                    (store_id, completed_at, product_count)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        completed_at = VALUES(completed_at),
                        product_count = VALUES(product_count)""",
                    (store_id, now(), len(rows)),
                )
            finally:
                cursor.close()

    @staticmethod
    def get_store_export_products(store_id: int) -> list[dict]:
        """Fetch the current CSV-ready rows for one store."""
        with DatabaseManager.get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            try:
                cursor.execute(
                    """
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
                    LEFT JOIN brands b
                        ON p.brand_id = b.brand_id
                    LEFT JOIN product_categories pc
                        ON p.id = pc.product_id
                    LEFT JOIN categories c
                        ON pc.category_id = c.category_id
                    WHERE p.store_id = %s
                        AND p.image_url IS NOT NULL
                        AND p.image_url != ''
                        AND p.is_active = 1
                        AND p.stock_status = 'in_stock'
                    GROUP BY p.id
                    ORDER BY p.id
                    """,
                    (store_id,),
                )
                return cursor.fetchall()
            finally:
                cursor.close()

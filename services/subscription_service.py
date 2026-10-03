"""Subscription service for managing API subscriptions"""

import logging
import secrets
import uuid
from datetime import timedelta

from config.database import DatabaseManager
from config.settings import settings
from utils.timeutil import now

logger = logging.getLogger(__name__)


class SubscriptionService:
    """Manages subscriptions, store permissions, and storefront credentials"""

    @staticmethod
    def _get_active_subscription(cursor, token: str, buyer_domain: str) -> dict:
        """Resolve the newest active subscription for a domain, validating the token"""
        cursor.execute(
            """SELECT id, token, expires_at FROM api_subscriptions
            WHERE buyer_domain = %s AND expires_at > NOW()
            ORDER BY created_at DESC LIMIT 1""",
            (buyer_domain,),
        )
        subscription = cursor.fetchone()
        if not subscription:
            raise ValueError(f"No active subscription found for domain: {buyer_domain}")
        if not secrets.compare_digest(subscription["token"], token):
            raise ValueError("Invalid token for this domain")
        return subscription

    @staticmethod
    def create_subscription(
        buyer_name: str, buyer_domain: str, phone_number: str
    ) -> dict:
        """Create a new subscription and generate its token"""
        buyer_domain = buyer_domain.rstrip("/")
        token = str(uuid.uuid4())
        created_at = now()
        expires_at = created_at + timedelta(days=settings.SUBSCRIPTION_DAYS)

        with DatabaseManager.get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                """INSERT INTO api_subscriptions
                (token, buyer_name, buyer_domain, phone_number, created_at, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s)""",
                (
                    token,
                    buyer_name,
                    buyer_domain,
                    phone_number,
                    created_at,
                    expires_at,
                ),
            )
            subscription_id = cursor.lastrowid

        logger.info(f"Created subscription {subscription_id} for {buyer_name}")

        return {
            "subscription_id": subscription_id,
            "token": token,
            "buyer_name": buyer_name,
            "buyer_domain": buyer_domain,
            "phone_number": phone_number,
            "created_at": created_at.isoformat(),
            "expires_at": expires_at.isoformat(),
        }

    @staticmethod
    def add_subscription_permissions(
        token: str, buyer_domain: str, store_ids: list[int]
    ) -> dict:
        """Set store permissions, diffing against the current set"""
        buyer_domain = buyer_domain.rstrip("/")

        if not store_ids:
            raise ValueError("store_ids cannot be empty")

        with DatabaseManager.get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            subscription = SubscriptionService._get_active_subscription(
                cursor, token, buyer_domain
            )
            subscription_id = subscription["id"]

            placeholders = ",".join(["%s"] * len(store_ids))
            cursor.execute(
                f"SELECT store_id FROM stores WHERE store_id IN ({placeholders})",
                store_ids,
            )
            valid_stores = {row["store_id"] for row in cursor.fetchall()}
            invalid_stores = set(store_ids) - valid_stores
            if invalid_stores:
                raise ValueError(f"Invalid store IDs: {invalid_stores}")

            cursor.execute(
                """SELECT store_id FROM subscription_permissions
                WHERE subscription_id = %s""",
                (subscription_id,),
            )
            existing = {row["store_id"] for row in cursor.fetchall()}
            requested = set(store_ids)

            to_add = requested - existing
            to_remove = existing - requested

            if to_remove:
                rm_placeholders = ",".join(["%s"] * len(to_remove))
                cursor.execute(
                    f"""DELETE FROM subscription_permissions
                    WHERE subscription_id = %s AND store_id IN ({rm_placeholders})""",
                    [subscription_id] + list(to_remove),
                )

            for store_id in to_add:
                cursor.execute(
                    """INSERT INTO subscription_permissions
                    (subscription_id, store_id) VALUES (%s, %s)""",
                    (subscription_id, store_id),
                )

        logger.info(
            f"Updated permissions for subscription {subscription_id}: "
            f"+{len(to_add)} added, -{len(to_remove)} removed, "
            f"{len(requested & existing)} preserved"
        )

        return {
            "subscription_id": subscription_id,
            "buyer_domain": buyer_domain,
            "total_stores": len(requested),
            "added": len(to_add),
            "removed": len(to_remove),
            "preserved": len(requested & existing),
        }

    @staticmethod
    def store_woocommerce_credentials(
        token: str, buyer_domain: str, consumer_key: str, consumer_secret: str
    ) -> dict:
        """Store the buyer's WordPress REST credentials for a subscription"""
        buyer_domain = buyer_domain.rstrip("/")

        if not consumer_key or not consumer_key.startswith("ck_"):
            raise ValueError("Invalid consumer_key format. Must start with 'ck_'")
        if not consumer_secret or not consumer_secret.startswith("cs_"):
            raise ValueError("Invalid consumer_secret format. Must start with 'cs_'")

        with DatabaseManager.get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            subscription = SubscriptionService._get_active_subscription(
                cursor, token, buyer_domain
            )
            subscription_id = subscription["id"]

            cursor.execute(
                """INSERT INTO woocommerce_credentials
                (subscription_id, consumer_key, consumer_secret)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE
                consumer_key = VALUES(consumer_key),
                consumer_secret = VALUES(consumer_secret)""",
                (subscription_id, consumer_key, consumer_secret),
            )

        logger.info(
            f"Stored WooCommerce credentials for subscription {subscription_id}"
        )

        return {
            "subscription_id": subscription_id,
            "buyer_domain": buyer_domain,
            "message": "WooCommerce credentials stored successfully",
        }

    @staticmethod
    def get_subscription_status(token: str, buyer_domain: str) -> dict:
        """Get a subscription's expiry and selected stores"""
        buyer_domain = buyer_domain.rstrip("/")

        with DatabaseManager.get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            subscription = SubscriptionService._get_active_subscription(
                cursor, token, buyer_domain
            )
            subscription_id = subscription["id"]

            cursor.execute(
                """SELECT sp.store_id, s.store_name, s.store_type
                FROM subscription_permissions sp
                JOIN stores s ON sp.store_id = s.store_id
                WHERE sp.subscription_id = %s""",
                (subscription_id,),
            )
            stores = cursor.fetchall()

        return {
            "expires_at": subscription["expires_at"].isoformat(),
            "selected_stores": [
                {
                    "store_id": s["store_id"],
                    "store_name": s["store_name"],
                    "store_type": s["store_type"],
                }
                for s in stores
            ],
        }

"""Push orchestrator for sending data to all active subscriptions"""

import logging

from mysql.connector import Error as MySQLError

from config.database import DatabaseManager
from services.csv_service import CSVService

logger = logging.getLogger(__name__)


class PushOrchestrator:
    """Orchestrates data push to all active subscriptions"""

    @staticmethod
    def push_to_all_subscriptions() -> dict:
        """Push data to all active subscriptions"""
        with DatabaseManager.get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("""SELECT id, buyer_domain FROM api_subscriptions
                WHERE expires_at > NOW()""")
            subscriptions = cursor.fetchall()

        logger.info(f"Found {len(subscriptions)} active subscriptions")

        results = {
            "total": len(subscriptions),
            "success": 0,
            "failed": 0,
            "no_data": 0,
        }

        for sub in subscriptions:
            subscription_id = sub["id"]
            buyer_domain = sub["buyer_domain"]

            try:
                csv_path = CSVService.generate_csv_for_subscription(subscription_id)
            except (MySQLError, OSError, ValueError) as e:
                results["failed"] += 1
                logger.error(
                    f"CSV generation failed for {buyer_domain}: {type(e).__name__}"
                )
                continue

            if csv_path is None:
                results["no_data"] += 1
                logger.info(f"No data to push for {buyer_domain}")
                continue

            if CSVService.upload_csv(csv_path, subscription_id):
                results["success"] += 1
                logger.info(f"Uploaded CSV for {buyer_domain}")
            else:
                results["failed"] += 1
                logger.error(f"Upload failed for {buyer_domain}")

        logger.info(
            f"Push completed: {results['success']} success, "
            f"{results['failed']} failed, {results['no_data']} no data"
        )
        return results

    @staticmethod
    def update_last_push_at(token: str, buyer_domain: str) -> bool:
        """Mark a subscription's permissions as pushed, after a successful upload"""
        try:
            with DatabaseManager.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """UPDATE subscription_permissions sp
                    JOIN api_subscriptions s ON s.id = sp.subscription_id
                    SET sp.last_push_at = NOW()
                    WHERE s.token = %s AND s.buyer_domain = %s""",
                    (token, buyer_domain.rstrip("/")),
                )
            return True
        except MySQLError as e:
            logger.error(f"Error updating last_push_at: {type(e).__name__}")
            return False

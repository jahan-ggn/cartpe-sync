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
            "skipped": 0,
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
                results["skipped"] += 1
                logger.warning(
                    f"Push skipped for {buyer_domain}: "
                    "no active subscription, no selected stores, "
                    "or no stores with a recorded complete scrape"
                )
                continue

            if CSVService.upload_csv(csv_path, subscription_id):
                results["success"] += 1
                logger.info(f"Uploaded CSV for {buyer_domain}")
            else:
                results["failed"] += 1
                logger.error(f"Upload failed for {buyer_domain}")

        logger.info(
            f"Push completed: {results['success']} success, "
            f"{results['failed']} failed, {results['skipped']} skipped"
        )
        return results

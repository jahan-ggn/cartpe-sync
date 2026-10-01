"""Category scraper for CartPE stores"""

import logging
from urllib.parse import urlsplit

import requests

from config.settings import settings
from scrapers.crypto import decrypt_json, encrypt_json

logger = logging.getLogger(__name__)


class CategoryScraper:
    """Scrapes categories from the CartPE encrypted API"""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": settings.USER_AGENT})

    def extract_categories(self, store_data: dict) -> tuple[list[dict], bool]:
        """Extract all categories from the store's all-category endpoint.

        Returns (categories, success). `success` is False on any API/parsing
        failure, so the caller can distinguish failure from an empty result.
        """
        store_id = store_data["store_id"]
        store_name = store_data["store_name"]
        base_url = store_data["base_url"].rstrip("/")
        host = urlsplit(base_url).netloc

        logger.info(f"Fetching categories for: {store_name} (ID: {store_id})")

        try:
            response = self.session.post(
                f"{base_url}/api/all-category",
                json=encrypt_json({}),
                headers={"X-Tenant-Host": host, "Accept": "application/json"},
                timeout=settings.REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            payload = decrypt_json(response.json())
            if not payload.get("success"):
                logger.warning(
                    f"Category API reported failure for {store_name}: {payload.get('message')}"
                )
                return [], False
        except Exception as e:
            logger.error(f"Error fetching categories for {store_name}: {e}")
            return [], False

        categories = [
            {
                "store_id": store_id,
                "external_category_id": str(cat["category_id"]),
                "category_name": cat["category_name"],
                "category_slug": cat["slug"],
                "category_url": f"{base_url}/{cat['slug']}",
            }
            for cat in payload.get("data", {}).get("categories", [])
        ]

        logger.info(f"Extracted {len(categories)} categories from {store_name}")
        return categories, True

    def close(self):
        """Close the requests session"""
        self.session.close()

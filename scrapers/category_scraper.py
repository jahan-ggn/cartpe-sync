"""Category scraper for CartPE stores"""

import logging
from urllib.parse import urlsplit

import requests
from cryptography.exceptions import InvalidTag

from config.settings import settings
from scrapers.crypto import decrypt_json, encrypt_json

logger = logging.getLogger(__name__)


class CategoryScraper:
    """Scrapes categories from the CartPE encrypted API"""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": settings.USER_AGENT})

    def extract_categories(self, store_data: dict) -> tuple[list[dict], bool]:
        """Return (categories, success); API/parsing failures return ([], False)."""
        store_id = store_data["store_id"]
        store_name = store_data["store_name"]
        base_url = store_data["base_url"].rstrip("/")
        host = urlsplit(base_url).netloc

        logger.info(f"Fetching categories for: {store_name} (ID: {store_id})")

        try:
            response = self.session.post(
                f"{base_url}/api/all-category",
                json=encrypt_json({}),
                headers={
                    "X-Tenant-Host": host,
                    "Accept": "application/json",
                },
                timeout=settings.REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            payload = decrypt_json(response.json())

            if not isinstance(payload, dict):
                raise ValueError("Category payload must be a dictionary")

            if not payload.get("success"):
                logger.warning(
                    f"Category API reported failure for {store_name}: "
                    f"{payload.get('message')}"
                )
                return [], False

            data = payload.get("data")
            if not isinstance(data, dict):
                raise ValueError("Category payload data must be a dictionary")

            items = data.get("categories")
            if not isinstance(items, list):
                raise ValueError("Category payload categories must be a list")

            categories = []

            for cat in items:
                if not isinstance(cat, dict):
                    raise ValueError("Category entry must be a dictionary")

                external_id = cat.get("category_id")
                name = cat.get("category_name")
                slug = cat.get("slug")

                if (
                    isinstance(external_id, bool)
                    or not isinstance(external_id, (str, int))
                    or not str(external_id).strip()
                ):
                    raise ValueError("Category entry has an invalid category_id")

                if not isinstance(name, str) or not name.strip():
                    raise ValueError(
                        f"Category {external_id} has an invalid category_name"
                    )

                if not isinstance(slug, str) or not slug.strip():
                    raise ValueError(f"Category {external_id} has an invalid slug")

                categories.append(
                    {
                        "store_id": store_id,
                        "external_category_id": str(external_id),
                        "category_name": name,
                        "category_slug": slug,
                        "category_url": f"{base_url}/{slug}",
                    }
                )

        except (
            requests.RequestException,
            InvalidTag,
            ValueError,
            TypeError,
            KeyError,
        ) as e:
            logger.error(f"Error fetching categories for {store_name}: {e}")
            return [], False

        logger.info(f"Extracted {len(categories)} categories from {store_name}")
        return categories, True

    def close(self):
        """Close the requests session"""
        self.session.close()

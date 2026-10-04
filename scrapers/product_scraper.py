"""Product scraper for CartPE stores"""

import json
import logging
import time
from urllib.parse import urlsplit

import requests
from cryptography.exceptions import InvalidTag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config.settings import settings
from scrapers.crypto import decrypt_json, encrypt_json
from utils.brand_detector import BrandDetector

logger = logging.getLogger(__name__)

IMAGE_CDN = "https://cdn.cartpe.in/images"
GALLERY_MD = "gallery_md"

API_ERRORS = (requests.RequestException, ValueError, InvalidTag)


class ProductScraper:
    """Scrapes products from the CartPE encrypted API"""

    def __init__(self, known_brands: dict[str, int] | None = None):
        self.session = requests.Session()

        retry_strategy = Retry(
            total=3,
            backoff_factor=3,
            status_forcelist=[429, 502, 503, 504],
            allowed_methods=["GET", "POST"],
        )
        adapter = HTTPAdapter(
            pool_connections=30, pool_maxsize=30, max_retries=retry_strategy
        )
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.session.headers.update({"User-Agent": settings.USER_AGENT})

        self.known_brands = known_brands or {}
        self.brand_detector = BrandDetector(
            list(self.known_brands),
            min_similarity=settings.BRAND_MIN_SIMILARITY,
            min_margin=settings.BRAND_MIN_MARGIN,
        )

        logger.info(
            "Loaded %s brands for matching",
            len(self.known_brands),
        )

    def _post_encrypted(self, url: str, body: dict, host: str) -> dict:
        """POST an encrypted body and decrypt the response envelope"""
        response = self.session.post(
            url,
            json=encrypt_json(body),
            headers={"X-Tenant-Host": host, "Accept": "application/json"},
            timeout=settings.REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        if "application/json" not in response.headers.get("content-type", ""):
            raise ValueError(
                f"{url} returned {response.status_code} "
                f"({response.headers.get('content-type')}): {response.text[:120]!r}"
            )

        return decrypt_json(response.json())

    def _image_url(self, filename: str) -> str | None:
        """Build a `gallery_md` CDN URL from the bare filename the API returns"""
        if not filename:
            return None
        return f"{IMAGE_CDN}/{GALLERY_MD}/{filename}"

    def _build_variants(self, sizes: list) -> tuple[bool, str | None]:
        """Turn the `sizes` array into the stored variants field"""
        entries = [
            {
                "name": "Size",
                "value": str(size["sizeName"]),
            }
            for size in sizes or []
            if isinstance(size, dict)
            and size.get("sizeName")
            and size.get("qty", 0) > 0
        ]
        if not entries:
            return False, None
        return True, json.dumps(entries)

    def _extract_brand_from_name(self, product_name: str) -> str | None:
        """Resolve a product title to a canonical brand when the match is clear."""
        result = self.brand_detector.detect(product_name)

        if result["status"] == "ambiguous":
            logger.warning(
                "Ambiguous brand for product %r: %s",
                product_name,
                result["candidates"],
            )
        elif result["method"] == "fuzzy":
            logger.debug(
                "Corrected brand for %r to %s (similarity %.1f%%)",
                product_name,
                result["brand_name"],
                result["similarity"],
            )

        return result["brand_name"]

    def _parse_product(
        self,
        item: dict,
        store_id: int,
        store_name: str,
        category_id: int,
        base_url: str,
    ) -> tuple[dict | None, str | None]:
        """Map one API product onto the row shape the upsert expects"""
        try:
            if not isinstance(item, dict):
                logger.warning(
                    f"Skipping malformed product entry: {type(item).__name__}"
                )
                return None, "malformed_data"
            product_name = item.get("productName")
            filename = item.get("image")
            if not product_name or not filename:
                return None, "missing_fields"

            site_slug = item.get("siteSlug")
            if not site_slug:
                logger.warning(
                    f"Skipping {item.get('id')} ({product_name}) - siteSlug not yet generated"
                )
                return None, "no_slug"

            has_variants, variants = self._build_variants(item.get("sizes"))
            image_url = self._image_url(filename)
            brand_name = self._extract_brand_from_name(product_name)
            brand_id = self.known_brands.get(brand_name) if brand_name else None

            product = {
                "store_id": store_id,
                "store_name": store_name,
                "category_id": category_id,
                "external_product_id": str(item["id"]),
                "product_name": product_name,
                "product_url": f"{base_url}/product-detail/{item['siteSlug']}",
                "site_slug": item.get("siteSlug"),
                "image_url": image_url,
                "source_image_url": image_url,
                "product_images": None,
                "current_price": item.get("basicPrice"),
                "original_price": item.get("oldPrice"),
                "has_variants": has_variants,
                "variants": variants,
                "stock_status": (
                    "in_stock" if item.get("stock") == 1 else "out_of_stock"
                ),
                "brand_name": brand_name,
                "brand_id": brand_id,
            }
            return product, None
        except (AttributeError, KeyError, TypeError, ValueError) as e:
            logger.warning(f"Skipping malformed product {item.get('id')}: {e}")
            return None, "malformed_data"

    def extract_products(
        self, store_data: dict, category_data: dict
    ) -> tuple[list[dict], bool]:
        """Extract products for a category, paging until exhausted.

        Returns (products, complete). Completeness requires valid pagination
        and a unique-ID count matching the reported total, including products
        deliberately skipped because their slug is missing.
        """

        store_id = store_data["store_id"]
        store_name = store_data["store_name"]
        base_url = store_data["base_url"].rstrip("/")
        host = urlsplit(base_url).netloc

        category_id = category_data["category_id"]
        category_name = category_data["category_name"]
        category_slug = category_data["category_slug"]

        api_url = f"{base_url}/api/category-products"
        all_products: list[dict] = []
        seen_ids: set = set()
        page = 1
        complete = True
        reported_total: int | None = None
        skipped_slug_ids: set[str] = set()

        logger.info(f"Fetching products for {store_name} - {category_name}")

        while True:
            request_data = {
                "slug": [category_slug],
                "category_type": ["category"],
                "search_key": "",
                "orderby": "",
                "min_price": "",
                "max_price": "",
                "size_id": [],
                "page": page,
                "per_page": settings.CARTPE_PER_PAGE,
                "with_count": 1,
                "total_product": 0,
                "in_stock_count": 0,
                "token": None,
            }

            try:
                payload = self._post_encrypted(api_url, request_data, host)
            except API_ERRORS as e:
                logger.error(
                    f"Error fetching products for {store_name} - {category_name} "
                    f"(page {page}): {e}"
                )
                complete = False
                break

            if not isinstance(payload, dict):
                logger.warning(f"Malformed payload for {category_name} (page {page})")
                complete = False
                break

            status = payload.get("status")
            if not isinstance(status, bool) or not status:
                logger.warning(
                    f"API reported failure for {category_name} (page {page}): "
                    f"{payload.get('message')}"
                )
                complete = False
                break

            items = payload.get("data")
            if not isinstance(items, list):
                logger.warning(
                    f"Malformed data for {category_name} (page {page}): "
                    f"expected list, got {type(items).__name__}"
                )
                complete = False
                break

            has_more = payload.get("has_more_pages")
            total = payload.get("total")
            if (
                not isinstance(has_more, bool)
                or not isinstance(total, int)
                or isinstance(total, bool)
                or total < 0
            ):
                logger.warning(
                    f"Malformed pagination fields for {category_name} (page {page})"
                )
                complete = False
                break

            if reported_total is None:
                reported_total = total
            elif total != reported_total:
                logger.warning(
                    f"Reported total changed for {category_name}: "
                    f"{reported_total} -> {total} (page {page})"
                )
                complete = False
                break

            if not items:
                if total == 0 and not has_more:
                    logger.info(f"Category {category_name} is empty")
                else:
                    logger.warning(
                        f"Empty page but total={total}, has_more={has_more} "
                        f"for {category_name} (page {page})"
                    )
                    complete = False
                break

            for item in items:
                product, reason = self._parse_product(
                    item, store_id, store_name, category_id, base_url
                )
                if product:
                    all_products.append(product)
                    seen_ids.add(product["external_product_id"])
                elif reason == "no_slug":
                    external_id = item.get("id")
                    if (
                        isinstance(external_id, bool)
                        or not isinstance(external_id, (str, int))
                        or not str(external_id).strip()
                    ):
                        logger.warning(
                            f"Slug-less product has an invalid ID in {category_name}"
                        )
                        complete = False
                    else:
                        skipped_slug_ids.add(str(external_id))
                else:
                    complete = False

            logger.info(f"Page {page}: parsed {len(items)} products")

            if not has_more:
                break

            page += 1
            time.sleep(settings.REQUEST_DELAY)

        accounted_ids = seen_ids | skipped_slug_ids

        if (
            complete
            and reported_total is not None
            and len(accounted_ids) != reported_total
        ):
            logger.warning(
                f"Accounted for {len(accounted_ids)} unique products in "
                f"{category_name}, but API reported total={reported_total}"
            )
            complete = False

        logger.info(
            f"Total products extracted for {category_name}: "
            f"{len(all_products)} "
            f"(complete={complete}, skipped_no_slug={len(skipped_slug_ids)})"
        )

        return all_products, complete

    def close(self) -> None:
        """Close the scraper's HTTP session."""
        self.session.close()

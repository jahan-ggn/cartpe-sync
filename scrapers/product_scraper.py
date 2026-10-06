"""Product scraper for CartPE stores"""

import json
import logging
import threading
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

_WAF_CONDITION = threading.Condition()
_waf_resume_at = 0.0

WAF_COOLDOWNS = (60, 120, 240)


def _wait_for_waf_cooldown() -> None:
    """Pause new product requests until the shared cooldown expires."""
    with _WAF_CONDITION:
        while True:
            remaining = _waf_resume_at - time.monotonic()
            if remaining <= 0:
                return
            _WAF_CONDITION.wait(timeout=min(remaining, 60))


def _extend_waf_cooldown(seconds: int) -> None:
    """Extend the cooldown shared by all product scraper threads."""
    global _waf_resume_at

    with _WAF_CONDITION:
        _waf_resume_at = max(
            _waf_resume_at,
            time.monotonic() + seconds,
        )
        _WAF_CONDITION.notify_all()


class ProductScraper:
    """Scrapes products from the CartPE encrypted API"""

    def __init__(self, known_brands: dict[str, int] | None = None):
        self.session = requests.Session()

        retry_strategy = Retry(
            total=3,
            backoff_factor=3,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
            raise_on_status=False,
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
        """Request a page with shared cooldowns for WAF challenges."""
        for attempt in range(len(WAF_COOLDOWNS) + 1):
            _wait_for_waf_cooldown()

            response = self.session.post(
                url,
                json=encrypt_json(body),
                headers={
                    "X-Tenant-Host": host,
                    "Accept": "application/json",
                },
                timeout=settings.REQUEST_TIMEOUT,
            )

            try:
                waf_action = (
                    response.headers.get("x-amzn-waf-action", "").strip().lower()
                )

                if response.status_code == 202 and waf_action == "challenge":
                    if attempt == len(WAF_COOLDOWNS):
                        # Protect other workers even when this page gives up.
                        _extend_waf_cooldown(WAF_COOLDOWNS[-1])
                        raise ValueError(
                            f"{url}: WAF challenge persisted after "
                            f"{attempt + 1} attempts"
                        )

                    cooldown = WAF_COOLDOWNS[attempt]
                    _extend_waf_cooldown(cooldown)

                    logger.warning(
                        "WAF challenge: host=%s, page=%s; shared cooldown "
                        "at least %ss before retry %s/%s",
                        host,
                        body.get("page"),
                        cooldown,
                        attempt + 1,
                        len(WAF_COOLDOWNS),
                    )
                    continue

                if response.status_code != 200:
                    logger.warning(
                        "Unexpected API response: host=%s, page=%s, "
                        "status=%s, content_type=%r, waf_action=%r",
                        host,
                        body.get("page"),
                        response.status_code,
                        response.headers.get("Content-Type"),
                        waf_action,
                    )
                    response.raise_for_status()
                    raise ValueError(
                        f"{url} returned unexpected HTTP " f"{response.status_code}"
                    )

                content_type = response.headers.get("Content-Type", "")
                if "application/json" not in content_type.lower():
                    raise ValueError(
                        f"{url} returned a non-JSON response " f"({content_type})"
                    )

                payload = decrypt_json(response.json())

                if attempt:
                    logger.info(
                        "API request recovered after WAF cooldown: "
                        "host=%s, page=%s, attempts=%s",
                        host,
                        body.get("page"),
                        attempt + 1,
                    )

                return payload
            finally:
                response.close()

        raise ValueError(f"{url}: WAF retry attempts exhausted")

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
            if (
                not isinstance(product_name, str)
                or not product_name.strip()
                or not isinstance(filename, str)
                or not filename.strip()
            ):
                site_slug = item.get("siteSlug")
                product_url = (
                    f"{base_url.rstrip('/')}/product-detail/{site_slug}"
                    if isinstance(site_slug, str) and site_slug.strip()
                    else None
                )
                logger.warning(
                    "Skipping product with missing or invalid title/image: "
                    "id=%r, productName=%r, image=%r, url=%s",
                    item.get("id"),
                    product_name,
                    filename,
                    product_url or "unavailable",
                )
                return None, "missing_fields"

            site_slug = item.get("siteSlug")
            if not isinstance(site_slug, str) or not site_slug.strip():
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
        and a distinct-listing count matching the reported total, including
        deliberately skipped products. Products are deduplicated by source ID.
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
        products_by_id: dict[str, dict] = {}
        accounted_listings: set[tuple[str, str | None]] = set()
        first_page_by_id: dict[str, int] = {}
        page = 1
        consecutive_request_failures = 0
        complete = True
        reported_total: int | None = None
        skipped_product_ids: set[str] = set()
        received_entries = 0
        duplicate_counts: dict[str, int] = {}
        skipped_reasons: dict[str, str] = {}

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
                    "Page failed for %s / %s: page=%s, error=%s",
                    store_name,
                    category_name,
                    page,
                    e,
                )
                complete = False
                consecutive_request_failures += 1

                if consecutive_request_failures >= 3:
                    logger.warning(
                        "Stopping %s / %s after three consecutive failed pages",
                        store_name,
                        category_name,
                    )
                    break

                page += 1
                time.sleep(settings.REQUEST_DELAY)
                continue

            consecutive_request_failures = 0

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

            received_entries += len(items)

            for item in items:
                if isinstance(item, dict):
                    external_id = item.get("id")
                    if (
                        isinstance(external_id, (str, int))
                        and not isinstance(external_id, bool)
                        and str(external_id).strip()
                    ):
                        product_id = str(external_id)
                        if product_id in first_page_by_id:
                            duplicate_counts[product_id] = (
                                duplicate_counts.get(product_id, 0) + 1
                            )
                            logger.warning(
                                "Repeated product ID in %s / %s: "
                                "id=%s, first_page=%s, repeated_page=%s",
                                store_name,
                                category_name,
                                product_id,
                                first_page_by_id[product_id],
                                page,
                            )
                        else:
                            first_page_by_id[product_id] = page

                if isinstance(item, dict):
                    external_id = item.get("id")
                    if (
                        isinstance(external_id, (str, int))
                        and not isinstance(external_id, bool)
                        and str(external_id).strip()
                    ):
                        site_slug = item.get("siteSlug")
                        listing_slug = (
                            site_slug
                            if isinstance(site_slug, str) and site_slug.strip()
                            else None
                        )
                        accounted_listings.add((str(external_id), listing_slug))
                product, reason = self._parse_product(
                    item, store_id, store_name, category_id, base_url
                )

                if product:
                    product_id = product["external_product_id"]
                    existing = products_by_id.get(product_id)

                    if existing is None:
                        products_by_id[product_id] = product
                        all_products.append(product)
                    else:
                        # Different URLs may represent the same source product.
                        existing_fields = {
                            key: value
                            for key, value in existing.items()
                            if key not in {"product_url", "site_slug"}
                        }
                        incoming_fields = {
                            key: value
                            for key, value in product.items()
                            if key not in {"product_url", "site_slug"}
                        }

                        if existing_fields != incoming_fields:
                            complete = False
                            differences = {
                                key: {
                                    "first": existing_fields.get(key),
                                    "duplicate": incoming_fields.get(key),
                                }
                                for key in (
                                    existing_fields.keys() | incoming_fields.keys()
                                )
                                if existing_fields.get(key) != incoming_fields.get(key)
                            }
                            logger.warning(
                                "Conflicting duplicate product in %s / %s: "
                                "id=%s, differences=%r",
                                store_name,
                                category_name,
                                product_id,
                                differences,
                            )

                elif reason in {"no_slug", "missing_fields"}:
                    external_id = item.get("id")
                    if (
                        isinstance(external_id, bool)
                        or not isinstance(external_id, (str, int))
                        or not str(external_id).strip()
                    ):
                        logger.warning(
                            "Skipped product has an invalid ID in %s / %s: "
                            "reason=%s",
                            store_name,
                            category_name,
                            reason,
                        )
                        complete = False
                    else:
                        skipped_product_ids.add(str(external_id))
                        skipped_reasons[str(external_id)] = reason

                else:
                    complete = False
                    logger.warning(
                        "Product skipped in %s / %s, page %s: " "id=%r, reason=%s",
                        store_name,
                        category_name,
                        page,
                        item.get("id") if isinstance(item, dict) else None,
                        reason,
                    )

            logger.info(f"Page {page}: parsed {len(items)} products")

            if not has_more:
                break

            page += 1
            time.sleep(settings.REQUEST_DELAY)

        logger.info(
            "Scrape accounting for %s / %s: "
            "reported_total=%s, received_entries=%s, "
            "unique_product_ids=%s, distinct_id_slug_pairs=%s, "
            "duplicate_occurrences=%s, extracted_products=%s, "
            "intentionally_skipped_ids=%s",
            store_name,
            category_name,
            reported_total,
            received_entries,
            len(first_page_by_id),
            len(accounted_listings),
            sum(duplicate_counts.values()),
            len(all_products),
            len(skipped_product_ids),
        )

        if duplicate_counts:
            logger.warning(
                "Duplicate IDs for %s / %s " "(ID: extra occurrences): %s",
                store_name,
                category_name,
                duplicate_counts,
            )

        if skipped_reasons:
            logger.warning(
                "Intentionally skipped IDs for %s / %s " "(ID: reason): %s",
                store_name,
                category_name,
                skipped_reasons,
            )

        logger.info(
            "Listing accounting for %s / %s: "
            "distinct_listings=%s, reported_total=%s",
            store_name,
            category_name,
            len(accounted_listings),
            reported_total,
        )

        if (
            complete
            and reported_total is not None
            and len(accounted_listings) != reported_total
        ):
            logger.warning(
                "Accounted for %s distinct listings in %s, "
                "but API reported total=%s",
                len(accounted_listings),
                category_name,
                reported_total,
            )
            complete = False

        logger.info(
            f"Total products extracted for {category_name}: "
            f"{len(all_products)} "
            f"(complete={complete}, skipped_products={len(skipped_product_ids)})"
        )

        return all_products, complete

    def close(self) -> None:
        """Close the scraper's HTTP session."""
        self.session.close()

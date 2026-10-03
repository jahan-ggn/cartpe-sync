"""CartPE scraping pipeline"""

import logging
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

from mysql.connector import Error as MySQLError

from config.settings import settings
from scrapers import CategoryScraper, ProductScraper
from services.database_service import CategoryService, ProductService, StoreService
from services.image_service import ImageService
from services.push_orchestrator import PushOrchestrator

logger = logging.getLogger(__name__)


def merge_metrics(target: dict, source: dict) -> None:
    """Merge source metrics into target by summing values"""
    for key, value in source.items():
        target[key] = target.get(key, 0) + value


def scrape_and_save_categories(store_data: dict) -> tuple[str, int, bool]:
    """Scrape categories for one store and save them; returns (name, count, ok)"""
    store_name = store_data["store_name"]

    scraper = CategoryScraper()
    try:
        categories, success = scraper.extract_categories(store_data)
        if not success:
            logger.error(f"Failed to fetch categories for {store_name}")
            return (store_name, 0, False)
        if not categories:
            logger.warning(f"No categories found for {store_name}")
            return (store_name, 0, False)

        CategoryService.bulk_insert_categories(categories)
        logger.info(f"Saved/refreshed {len(categories)} categories for {store_name}")
        return (store_name, len(categories), True)

    except MySQLError as e:
        logger.error(f"Error saving categories for {store_name}: {e}")
        return (store_name, 0, False)
    finally:
        scraper.close()


def run_category_scraping() -> bool:
    """Scrape categories for every CartPE store"""
    logger.info("=" * 80)
    logger.info("STEP 1: Category Scraping")
    logger.info("=" * 80)

    stores = StoreService.get_all_stores("cartpe")
    if not stores:
        logger.warning("No stores found in database")
        return False

    logger.info(f"Scraping categories for {len(stores)} stores")

    successful, failed, total = 0, 0, 0

    with ThreadPoolExecutor(max_workers=settings.MAX_WORKERS) as executor:
        futures = [executor.submit(scrape_and_save_categories, s) for s in stores]

        for future in as_completed(futures):
            store_name, count, ok = future.result()
            if ok:
                successful += 1
                total += count
                logger.info(f"Categories scraped: {store_name} ({count})")
            else:
                failed += 1
                logger.error(f"Categories failed: {store_name}")

    logger.info(
        f"Category scraping complete: {total} categories from {successful} stores\n"
    )
    return failed == 0


def scrape_category(store_data: dict, category: dict) -> tuple[str, dict | None, bool]:
    """Scrape every product in one category; returns (name, metrics or None, complete)"""
    category_name = category["category_name"]
    store_id = store_data["store_id"]

    scraper = ProductScraper()
    try:
        products, complete = scraper.extract_products(store_data, category)

        if not products:
            if complete:
                # Genuinely empty category: deactivate stale rows via the
                # unified path so DB errors propagate instead of being swallowed
                ProductService.bulk_upsert_products(
                    [], mark_inactive=(store_id, category["category_id"])
                )
                logger.info(f"No products found for {category_name}")
                return (category_name, None, True)
            logger.warning(f"Incomplete scrape for {category_name}: no products saved")
            return (category_name, None, False)

        mark_inactive = (store_id, category["category_id"]) if complete else None
        if not complete:
            logger.warning(
                f"Incomplete scrape for {category_name}: upserting collected "
                f"products but skipping deactivation"
            )

        metrics = ProductService.bulk_upsert_products(
            products, mark_inactive=mark_inactive
        )

        logger.info(
            f"Saved {metrics['total']} products for {category_name} "
            f"(new: {metrics['new']}, price: {metrics['price_changed']}, "
            f"stock: {metrics['stock_changed']})"
        )
        return (category_name, metrics, complete)

    except MySQLError as e:
        logger.error(f"Error saving products for {category_name}: {e}")
        return (category_name, None, False)
    finally:
        scraper.close()


def scrape_store_products(
    store_data: dict,
) -> tuple[str, dict, bool, int, int]:
    """Return (store name, metrics, complete, failed categories, total categories)."""
    store_id = store_data["store_id"]
    store_name = store_data["store_name"]

    store_metrics = {
        "new": 0,
        "price_changed": 0,
        "stock_changed": 0,
        "total": 0,
    }

    categories = CategoryService.get_categories_by_store(store_id)
    if not categories:
        logger.warning(f"No categories available for {store_name}")
        return (store_name, store_metrics, False, 0, 0)

    logger.info(f"Processing {len(categories)} categories for {store_name} (parallel)")

    failed_categories = 0

    with ThreadPoolExecutor(max_workers=settings.CATEGORY_WORKERS) as executor:
        futures = [
            executor.submit(scrape_category, store_data, cat) for cat in categories
        ]

        for future in as_completed(futures):
            category_name, metrics, cat_complete = future.result()

            if metrics:
                merge_metrics(store_metrics, metrics)

            if not cat_complete:
                failed_categories += 1
                logger.warning(
                    f"Category incomplete or failed: " f"{store_name} / {category_name}"
                )

    return (
        store_name,
        store_metrics,
        failed_categories == 0,
        failed_categories,
        len(categories),
    )


def run_product_scraping() -> bool:
    """Scrape products for every CartPE store."""
    logger.info("=" * 80)
    logger.info("STEP 2: Product Scraping")
    logger.info("=" * 80)

    stores = StoreService.get_all_stores("cartpe")
    if not stores:
        logger.warning("No stores available for product scraping")
        return False

    logger.info(f"Scraping products for {len(stores)} stores")

    successful, failed, total_products = 0, 0, 0
    failed_categories, total_categories = 0, 0

    with ThreadPoolExecutor(max_workers=settings.MAX_WORKERS) as executor:
        futures = [executor.submit(scrape_store_products, store) for store in stores]

        for future in as_completed(futures):
            store_name, metrics, ok, n_failed, n_total = future.result()

            failed_categories += n_failed
            total_categories += n_total
            total_products += metrics["total"]

            if ok:
                successful += 1
                logger.info(
                    f"Products scraped: {store_name} "
                    f"(total: {metrics['total']}, new: {metrics['new']}, "
                    f"price: {metrics['price_changed']}, "
                    f"stock: {metrics['stock_changed']})"
                )
            else:
                failed += 1
                if n_total == 0:
                    logger.error(
                        f"Products failed: {store_name} " "— no categories available"
                    )
                else:
                    logger.error(
                        f"Products failed: {store_name} "
                        f"({n_failed}/{n_total} categories incomplete or failed; "
                        f"{metrics['total']} products upserted)"
                    )

    if failed:
        logger.warning(
            f"Product scraping incomplete: {failed}/{len(stores)} stores failed; "
            f"{failed_categories}/{total_categories} categories incomplete "
            "or failed. The upcoming push may include previously stored "
            "stock data."
        )

    logger.info(
        f"Product scraping finished: {total_products} products upserted; "
        f"{successful} stores complete, "
        f"{failed} stores incomplete or failed"
    )

    return failed == 0


def run_pipeline() -> None:
    """Run the full CartPE scraping pipeline"""
    logger.info("*" * 80)
    logger.info("CARTPE PRODUCT SCRAPER - PIPELINE")
    logger.info("*" * 80)

    try:
        if not run_category_scraping():
            raise RuntimeError(
                "Category scraping failed; aborting pipeline before product scraping and push"
            )

        if not run_product_scraping():
            logger.warning(
                "Product scraping had failures; continuing to image processing "
                "and push. Subscribers may receive previously stored stock data "
                "for incomplete or failed stores/categories."
            )

        logger.info("=" * 80)
        logger.info("STEP 3: Image Processing")
        logger.info("=" * 80)

        if settings.R2_UPLOAD_ENABLED:
            try:
                ImageService().process_images()
            except MySQLError as e:
                logger.error(f"Image processing failed: {e}\n{traceback.format_exc()}")
        else:
            logger.info("R2 upload disabled - keeping CartPE CDN image URLs")

        logger.info("=" * 80)
        logger.info("STEP 4: Pushing Data to Subscriptions")
        logger.info("=" * 80)

        try:
            results = PushOrchestrator.push_to_all_subscriptions()
            logger.info(
                f"Push complete: {results['success']} success, "
                f"{results['failed']} failed, {results['no_data']} no data"
            )
        except MySQLError as e:
            logger.error(f"Data push failed: {e}\n{traceback.format_exc()}")

        logger.info("*" * 80)
        logger.info("CARTPE PIPELINE COMPLETE")
        logger.info("*" * 80)

    except KeyboardInterrupt:
        logger.info("Process interrupted by user")
    except MySQLError as e:
        logger.error(f"Fatal error in pipeline: {e}\n{traceback.format_exc()}")
        raise

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

CATEGORY_WORKERS = 5


def merge_metrics(target: dict, source: dict) -> None:
    """Merge source metrics into target by summing values"""
    for key, value in source.items():
        target[key] = target.get(key, 0) + value


def scrape_and_save_categories(store_data: dict) -> tuple[str, int, bool]:
    """Scrape categories for one store and save them; returns (name, count, ok)"""
    store_name = store_data["store_name"]

    scraper = CategoryScraper()
    try:
        categories = scraper.extract_categories(store_data)
        if not categories:
            logger.warning(f"No categories found for {store_name}")
            return (store_name, 0, False)

        inserted = CategoryService.bulk_insert_categories(categories)
        logger.info(f"Saved {inserted} new categories for {store_name}")
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

    stores = StoreService.get_all_stores()
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


def scrape_category(store_data: dict, category: dict) -> tuple[str, dict | None]:
    """Scrape every product in one category; returns (name, metrics or None)"""
    category_name = category["category_name"]
    store_id = store_data["store_id"]

    scraper = ProductScraper(product_service=ProductService)
    try:
        products = scraper.extract_products(store_data, category)

        if not products:
            logger.info(f"No products found for {category_name}")
            return (category_name, None)

        ProductService.mark_category_products_inactive(
            store_id, category["category_id"]
        )
        metrics = ProductService.bulk_upsert_products(products)

        logger.info(
            f"Saved {metrics['total']} products for {category_name} "
            f"(new: {metrics['new']}, price: {metrics['price_changed']}, "
            f"stock: {metrics['stock_changed']})"
        )
        return (category_name, metrics)

    except MySQLError as e:
        logger.error(f"Error saving products for {category_name}: {e}")
        return (category_name, None)
    finally:
        scraper.close()


def scrape_store_products(store_data: dict) -> tuple[str, dict, bool]:
    """Scrape all products for one store, its categories in parallel"""
    store_id = store_data["store_id"]
    store_name = store_data["store_name"]

    store_metrics = {"new": 0, "price_changed": 0, "stock_changed": 0, "total": 0}

    categories = CategoryService.get_categories_by_store(store_id)
    if not categories:
        logger.warning(f"No categories found for {store_name}")
        return (store_name, store_metrics, False)

    logger.info(f"Processing {len(categories)} categories for {store_name} (parallel)")

    failed_categories = 0

    with ThreadPoolExecutor(max_workers=CATEGORY_WORKERS) as executor:
        futures = [
            executor.submit(scrape_category, store_data, cat) for cat in categories
        ]

        for future in as_completed(futures):
            category_name, metrics = future.result()
            if metrics:
                merge_metrics(store_metrics, metrics)
            else:
                failed_categories += 1
                logger.warning(f"Category produced no data: {category_name}")

    return (store_name, store_metrics, failed_categories == 0)


def run_product_scraping() -> bool:
    """Scrape products for every CartPE store"""
    logger.info("=" * 80)
    logger.info("STEP 2: Product Scraping")
    logger.info("=" * 80)

    stores = StoreService.get_all_stores()
    if not stores:
        logger.warning("No stores found in database")
        return False

    logger.info(f"Scraping products for {len(stores)} stores")

    successful, failed, total_products = 0, 0, 0

    with ThreadPoolExecutor(max_workers=settings.MAX_WORKERS) as executor:
        futures = [executor.submit(scrape_store_products, s) for s in stores]

        for future in as_completed(futures):
            store_name, metrics, ok = future.result()
            if ok:
                successful += 1
                total_products += metrics["total"]
                logger.info(
                    f"Products scraped: {store_name} "
                    f"(total: {metrics['total']}, new: {metrics['new']}, "
                    f"price: {metrics['price_changed']}, "
                    f"stock: {metrics['stock_changed']})"
                )
            else:
                failed += 1
                logger.error(f"Products failed: {store_name}")

    logger.info(
        f"Product scraping complete: {total_products} products "
        f"from {successful} stores\n"
    )
    return failed == 0


def run_pipeline() -> None:
    """Run the full CartPE scraping pipeline"""
    logger.info("*" * 80)
    logger.info("CARTPE PRODUCT SCRAPER - PIPELINE")
    logger.info("*" * 80)

    try:
        if not run_category_scraping():
            logger.warning("Category scraping had failures, continuing...")

        if not run_product_scraping():
            logger.warning("Product scraping had failures, continuing...")

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

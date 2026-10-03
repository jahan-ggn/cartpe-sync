"""Store routes"""

import logging
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from mysql.connector import Error as MySQLError
from pydantic import BaseModel

from api.deps import require_admin
from scrapers import CategoryScraper
from services.database_service import CategoryService, StoreService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["stores"])


class StoreCreateRequest(BaseModel):
    store_type: Literal["cartpe"] = "cartpe"
    store_name: str
    base_url: str


def fetch_and_store_categories(store_data: dict) -> None:
    """Scrape and store categories for a newly created store."""
    scraper = CategoryScraper()
    try:
        categories, success = scraper.extract_categories(store_data)
        if not success:
            logger.error(f"Category fetch failed for {store_data['store_name']}")
            return

        if categories:
            CategoryService.bulk_insert_categories(categories)
    except MySQLError:
        logger.exception(f"Category save failed for {store_data['store_name']}")
    finally:
        scraper.close()


@router.get("/stores")
def get_stores() -> list[dict]:
    """List stores with their categories."""
    try:
        stores = StoreService.get_all_stores()
        categories = CategoryService.get_all_categories()
    except MySQLError as e:
        logger.error(f"Error fetching stores/categories: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

    categories_by_store = {}
    for cat in categories:
        categories_by_store.setdefault(cat["store_id"], []).append(
            {
                "category_id": cat["category_id"],
                "category_name": cat["category_name"],
                "category_slug": cat["category_slug"],
            }
        )

    return [
        {
            "store_id": store["store_id"],
            "store_type": store["store_type"],
            "store_name": store["store_name"],
            "base_url": store["base_url"],
            "categories": categories_by_store.get(store["store_id"], []),
        }
        for store in stores
    ]


@router.post("/stores", dependencies=[Depends(require_admin)])
async def create_store(
    request: StoreCreateRequest, background_tasks: BackgroundTasks
) -> dict:
    """Create a store and scrape its categories in the background"""
    store_data = {"store_name": request.store_name, "base_url": request.base_url}

    try:
        result = StoreService.create_store(store_data)
    except MySQLError as e:
        logger.error(f"Error creating store: {e}")
        raise HTTPException(status_code=500, detail="Database error")

    background_tasks.add_task(
        fetch_and_store_categories, {**store_data, "store_id": result["store_id"]}
    )
    return {"success": True, "data": result}

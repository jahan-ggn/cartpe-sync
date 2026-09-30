"""Store routes"""

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from mysql.connector import Error as MySQLError
from pydantic import BaseModel

from api.deps import require_admin
from scrapers import CategoryScraper
from services.database_service import CategoryService, StoreService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["stores"])


class StoreCreateRequest(BaseModel):
    store_name: str
    base_url: str


def fetch_and_store_categories(store_data: dict) -> None:
    """Scrape and store categories for a newly created store"""
    scraper = CategoryScraper()
    try:
        categories = scraper.extract_categories(store_data)
        if categories:
            CategoryService.bulk_insert_categories(categories)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error fetching categories for {store_data['store_name']}: {e}")
    finally:
        scraper.close()


@router.get("/stores")
def get_stores() -> list[dict]:
    """List stores with their categories"""
    try:
        stores = StoreService.get_all_stores()
    except MySQLError as e:
        logger.error(f"Error fetching stores: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

    return [
        {
            "store_id": store["store_id"],
            "store_type": store["store_type"],
            "store_name": store["store_name"],
            "base_url": store["base_url"],
            "categories": [
                {
                    "category_id": cat["category_id"],
                    "category_name": cat["category_name"],
                    "category_slug": cat["category_slug"],
                }
                for cat in CategoryService.get_categories_by_store(store["store_id"])
            ],
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
    except (MySQLError, KeyError) as e:
        logger.error(f"Error creating store: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    background_tasks.add_task(
        fetch_and_store_categories, {**store_data, "store_id": result["store_id"]}
    )
    return {"success": True, "data": result}

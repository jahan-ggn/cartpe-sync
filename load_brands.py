"""One-time script to load brands from brands.txt into the database"""

import logging
from pathlib import Path

from config.database import DatabaseManager
from config.settings import settings
from utils.logger import setup_logger

setup_logger("brands")
logger = logging.getLogger(__name__)


def load_brands_from_file(file_path: str | None = None) -> int:
    """Load brand names from `file_path`, defaulting to the project-root brands.txt"""
    path = Path(file_path) if file_path else settings.BASE_DIR / "brands.txt"

    try:
        with open(path, encoding="utf-8") as f:
            brands = [line.strip().strip('"') for line in f if line.strip()]
    except FileNotFoundError:
        logger.error(f"File not found: {path}")
        return 0

    logger.info(f"Found {len(brands)} brands in {path.name}")

    query = "INSERT IGNORE INTO brands (brand_name) VALUES (%s)"
    data = [(brand,) for brand in brands if brand]
    inserted = DatabaseManager.execute_many(query, data)

    logger.info(f"Loaded {inserted} new brands ({len(data)} names processed)")
    return inserted


if __name__ == "__main__":
    try:
        load_brands_from_file()
    except Exception:
        logging.getLogger(__name__).exception("Brand load failed")
        raise SystemExit(1)

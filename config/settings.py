"""Application configuration settings"""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


class Settings:
    """Centralized configuration for the application"""

    BASE_DIR = BASE_DIR

    # Database
    DB_HOST = os.getenv("DB_HOST", "localhost")
    DB_PORT = int(os.getenv("DB_PORT", "3306"))
    DB_USER = os.getenv("DB_USER", "root")
    DB_PASSWORD = os.getenv("DB_PASSWORD", "")
    DB_NAME = os.getenv("DB_NAME", "cartpe_sync")

    # API auth
    ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")
    WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

    # Scraping
    REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))
    REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "0.5"))
    MAX_WORKERS = int(os.getenv("MAX_WORKERS", "5"))
    CATEGORY_WORKERS = int(os.getenv("CATEGORY_WORKERS", "5"))
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    # Connection pool
    POOL_NAME = "cartpe_sync_pool"
    POOL_SIZE = int(os.getenv("POOL_SIZE", str(MAX_WORKERS * CATEGORY_WORKERS + 10)))

    # CartPE encrypted API
    CARTPE_KEY_SOURCE = os.getenv("CARTPE_KEY_SOURCE", "")
    CARTPE_PER_PAGE = int(os.getenv("CARTPE_PER_PAGE", "16"))

    # Subscription (single plan)
    SUBSCRIPTION_DAYS = int(os.getenv("SUBSCRIPTION_DAYS", "30"))

    # Logging
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    LOG_DIR = BASE_DIR / "logs"

    # Cloudflare R2
    R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "")
    R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "")
    R2_ENDPOINT_URL = os.getenv("R2_ENDPOINT_URL", "")
    R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME", "")
    R2_PUBLIC_URL = os.getenv("R2_PUBLIC_URL", "")
    R2_UPLOAD_ENABLED = os.getenv("R2_UPLOAD_ENABLED", "false").lower() == "true"


settings = Settings()

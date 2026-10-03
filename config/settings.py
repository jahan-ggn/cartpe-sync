"""Application configuration settings"""

import os
from pathlib import Path

from dotenv import load_dotenv
from mysql.connector.pooling import CNX_POOL_MAXSIZE

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
    POOL_SIZE = int(
        os.getenv(
            "POOL_SIZE",
            str(min(MAX_WORKERS * CATEGORY_WORKERS + 10, CNX_POOL_MAXSIZE)),
        )
    )

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


def _validate_settings() -> None:
    """Fail fast on missing required environment variables"""
    errors = []
    warnings = []

    if not 1 <= settings.POOL_SIZE <= CNX_POOL_MAXSIZE:
        errors.append(f"POOL_SIZE must be between 1 and {CNX_POOL_MAXSIZE}")

    # Required for core functionality
    if not settings.ADMIN_API_KEY:
        errors.append("ADMIN_API_KEY is required for admin endpoint authentication")
    if not settings.CARTPE_KEY_SOURCE:
        errors.append("CARTPE_KEY_SOURCE is required for encrypted API access")

    # Conditionally required for R2
    if settings.R2_UPLOAD_ENABLED:
        if not settings.R2_ACCESS_KEY_ID:
            errors.append("R2_ACCESS_KEY_ID is required when R2_UPLOAD_ENABLED=true")
        if not settings.R2_SECRET_ACCESS_KEY:
            errors.append(
                "R2_SECRET_ACCESS_KEY is required when R2_UPLOAD_ENABLED=true"
            )
        if not settings.R2_ENDPOINT_URL:
            errors.append("R2_ENDPOINT_URL is required when R2_UPLOAD_ENABLED=true")
        if not settings.R2_BUCKET_NAME:
            errors.append("R2_BUCKET_NAME is required when R2_UPLOAD_ENABLED=true")
        if not settings.R2_PUBLIC_URL:
            errors.append("R2_PUBLIC_URL is required when R2_UPLOAD_ENABLED=true")

    # Warn on DB defaults (may be intentional)
    if settings.DB_HOST == "localhost":
        warnings.append("DB_HOST is default (localhost) — ensure this is intentional")
    if settings.DB_USER == "root":
        warnings.append("DB_USER is default (root) — ensure this is intentional")
    if not settings.DB_PASSWORD:
        warnings.append("DB_PASSWORD is empty — ensure this is intentional")

    for w in warnings:
        print(f"WARNING: {w}")

    if errors:
        for e in errors:
            print(f"ERROR: {e}")
        raise SystemExit(
            f"Missing required environment variables: {len(errors)} errors"
        )


_validate_settings()

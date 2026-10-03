"""Logging configuration for the application"""

import logging
import os
from config.settings import settings


def setup_logger(source: str = "scraper"):
    """
    Configure and return application logger

    Args:
        source: 'cartpe', 'api', or 'scraper'
    """

    os.makedirs(settings.LOG_DIR, exist_ok=True)

    logger = logging.getLogger()
    level = logging.getLevelNamesMapping().get(settings.LOG_LEVEL.upper())
    logger.setLevel(level if level is not None else logging.INFO)

    # Clear existing handlers
    logger.handlers.clear()

    # File handler - source specific
    log_file = os.path.join(settings.LOG_DIR, f"{source}.log")
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)

    # Formatter
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger

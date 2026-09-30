"""Entry point for the CartPE scraping pipeline"""

import logging
import sys

from pipeline import run_pipeline
from services.csv_service import CSVService
from utils.logger import setup_logger


def main() -> None:
    setup_logger("cartpe")
    logger = logging.getLogger(__name__)

    logger.info("Starting CartPE sync run")
    CSVService.cleanup_old_csvs()
    run_pipeline()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.getLogger(__name__).exception("Pipeline crashed")
        sys.exit(1)

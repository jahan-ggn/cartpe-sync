"""Entry point for the CartPE scraping pipeline"""

import fcntl
import logging
import sys

from config.settings import settings, validate_settings
from pipeline import run_pipeline
from services.csv_service import CSVService
from utils.logger import setup_logger

LOCK_FILE = settings.BASE_DIR / "logs" / "pipeline.lock"


def main() -> None:
    setup_logger("cartpe")
    logger = logging.getLogger(__name__)
    validate_settings()

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)

    with open(LOCK_FILE, "w") as lock_fd:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.warning("Another pipeline run is in progress — exiting")
            sys.exit(0)

        try:
            logger.info("Starting CartPE sync run")
            CSVService.cleanup_old_csvs()
            run_pipeline()
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.getLogger(__name__).exception("Pipeline crashed")
        sys.exit(1)

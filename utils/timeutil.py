"""Application timestamps use Asia/Kolkata."""

from datetime import datetime
from zoneinfo import ZoneInfo

APP_TIMEZONE = ZoneInfo("Asia/Kolkata")


def now() -> datetime:
    """Return naive IST for MySQL DATETIME columns."""
    return datetime.now(APP_TIMEZONE).replace(tzinfo=None)


def parse_date(value: str, fmt: str = "%Y-%m-%d") -> datetime:
    """Parse an IST date and return naive IST for MySQL DATETIME."""
    parsed = datetime.strptime(value, fmt).replace(tzinfo=APP_TIMEZONE)
    return parsed.replace(tzinfo=None)

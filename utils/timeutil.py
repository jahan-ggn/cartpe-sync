"""Timestamp helpers — app times must share the MySQL server's clock"""

from datetime import datetime


def now() -> datetime:
    """Current local time, matching MySQL's NOW()/CURRENT_TIMESTAMP()"""
    return datetime.now()  # noqa: DTZ005


def parse_date(value: str, fmt: str = "%Y-%m-%d") -> datetime:
    """Parse a date string into naive local time, matching MySQL's clock"""
    return datetime.strptime(value, fmt)  # noqa: DTZ007

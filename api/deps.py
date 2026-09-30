"""Shared FastAPI dependencies"""

import secrets

from fastapi import Header, HTTPException

from config.settings import settings


def require_admin(api_key: str = Header(None)) -> None:
    """Reject requests without the admin API key"""
    if not api_key or not secrets.compare_digest(api_key, settings.ADMIN_API_KEY):
        raise HTTPException(status_code=403, detail="Invalid API key")

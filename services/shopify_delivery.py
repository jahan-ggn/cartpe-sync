"""Signed, retryable Shopify app CSV delivery. No Shopify access tokens are used."""

import hashlib
import hmac
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

import requests

logger = logging.getLogger(__name__)


def deliver_shopify_csv(csv_path: str, subscription_id: int, buyer_domain: str) -> bool:
    endpoint = os.getenv("SHOPIFY_SYNC_URL", "").strip()
    secret_hex = os.getenv("SOURCE_WEBHOOK_SECRET", "").strip()
    target = urlsplit(endpoint)
    if (
        not target.hostname
        or target.path != "/api/source/imports"
        or target.query
        or target.fragment
        or target.username
        or target.password
        or (
            target.scheme != "https"
            and not (
                target.scheme == "http"
                and target.hostname in {"localhost", "127.0.0.1", "::1"}
            )
        )
    ):
        raise ValueError(
            "Configure SHOPIFY_SYNC_URL as the app's HTTPS /api/source/imports endpoint"
        )
    if len(secret_hex) != 64 or any(c not in "0123456789abcdef" for c in secret_hex):
        raise ValueError(
            "SOURCE_WEBHOOK_SECRET must contain 64 lowercase hexadecimal characters"
        )
    domain = urlsplit(buyer_domain)
    shop = domain.hostname or ""
    if (
        domain.scheme != "https"
        or not shop.endswith(".myshopify.com")
        or domain.path not in {"", "/"}
        or domain.query
        or domain.fragment
        or domain.port
        or domain.username
        or domain.password
    ):
        raise ValueError(
            "Shopify buyer_domain must be its canonical HTTPS myshopify.com URL"
        )
    path = Path(csv_path)
    # The filename includes the export UUID. Reusing this file reuses its delivery ID.
    delivery_id = str(uuid5(NAMESPACE_URL, f"cartpe:{subscription_id}:{path.name}"))
    generated = str(path.stat().st_mtime_ns // 1_000_000)
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    content_hash = digest.hexdigest()
    for attempt in range(3):
        timestamp = str(int(time.time()))
        message = "\n".join(
            [
                "v1",
                "POST",
                "/api/source/imports",
                shop,
                delivery_id,
                timestamp,
                generated,
                content_hash,
            ]
        )
        signature = hmac.new(
            bytes.fromhex(secret_hex), message.encode(), hashlib.sha256
        ).hexdigest()
        headers = {
            "Content-Type": "text/csv",
            "X-Source-Shop": shop,
            "X-Source-Delivery-Id": delivery_id,
            "X-Source-Timestamp": timestamp,
            "X-Source-Generated-At": generated,
            "X-Source-Content-SHA256": content_hash,
            "X-Source-Signature": signature,
        }
        try:
            with path.open("rb") as source, requests.post(
                endpoint,
                data=source,
                headers=headers,
                timeout=(10, 90),
                allow_redirects=False,
            ) as response:
                if response.status_code in {200, 202}:
                    result = response.json()
                    return (
                        isinstance(result, dict)
                        and result.get("success") is True
                        and isinstance(result.get("jobId"), str)
                    )
                logger.warning(
                    "Shopify delivery for subscription %s returned HTTP %s",
                    subscription_id,
                    response.status_code,
                )
                if response.status_code not in {409, 429, 500, 502, 503, 504}:
                    return False
        except (requests.RequestException, ValueError):
            # A lost response is retried with the same delivery ID and CSV hash.
            pass
        if attempt < 2:
            time.sleep(2 ** (attempt + 1))
    return False

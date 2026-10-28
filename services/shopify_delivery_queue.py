"""Durable Shopify retry spool, processed by the existing API process."""

import fcntl
import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from config.database import DatabaseManager
from config.settings import settings
from services.shopify_delivery import DeliveryDeferred, _send

logger = logging.getLogger(__name__)
QUEUE = settings.BASE_DIR / "logs" / "shopify-deliveries"


@contextmanager
def _lock(subscription_id, blocking=False):
    QUEUE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (QUEUE / f"{subscription_id}.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _save(path, data):
    fd, name = tempfile.mkstemp(dir=QUEUE, prefix=".metadata-")
    try:
        with os.fdopen(fd, "w") as target:
            json.dump(data, target)
            target.flush()
            os.fsync(target.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _remove(path, data):
    with _lock("enqueue", blocking=True):
        path.unlink(missing_ok=True)
        (QUEUE / f"{data['id']}.csv").unlink(missing_ok=True)


def _attempt(path, data):
    try:
        accepted = _send(
            str(QUEUE / f"{data['id']}.csv"), data['subscription_id'],
            data['buyer_domain'], data['id'], data['generated'], data['hash'],
        )
    except DeliveryDeferred as error:
        data['next_attempt'] = time.time() + error.retry_after
        _save(path, data)
        logger.warning(
            "Shopify CSV for subscription %s retained; retry in %.0f seconds",
            data['subscription_id'], error.retry_after,
        )
        return False
    if accepted:
        logger.info("Shopify CSV accepted for subscription %s", data['subscription_id'])
    else:
        logger.error("Shopify CSV for subscription %s permanently rejected", data['subscription_id'])
    _remove(path, data)
    return accepted


def submit_delivery(csv_path, subscription_id, buyer_domain):
    original = Path(csv_path)
    delivery_id = str(uuid5(NAMESPACE_URL, f"cartpe:{subscription_id}:{original.name}"))
    QUEUE.mkdir(parents=True, exist_ok=True, mode=0o700)
    record = QUEUE / f"{delivery_id}.json"
    # Serialize only the short disk write; network requests use a separate lock.
    with _lock("enqueue", blocking=True):
        if record.exists():
            data = json.loads(record.read_text())
            digest = hashlib.sha256(original.read_bytes()).hexdigest()
            if data['hash'] != digest or data['buyer_domain'] != buyer_domain:
                raise ValueError("Delivery ID already retained with different contents")
            if data['next_attempt'] > time.time():
                return False
        else:
            destination = QUEUE / f"{delivery_id}.csv"
            fd, temporary = tempfile.mkstemp(dir=QUEUE, prefix=".csv-")
            try:
                with os.fdopen(fd, 'wb') as target, original.open('rb') as source:
                    shutil.copyfileobj(source, target)
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(temporary, destination)
            finally:
                Path(temporary).unlink(missing_ok=True)
            data = {
                'id': delivery_id, 'subscription_id': subscription_id,
                'buyer_domain': buyer_domain,
                'generated': str(original.stat().st_mtime_ns // 1_000_000),
                'hash': hashlib.sha256(destination.read_bytes()).hexdigest(),
                'next_attempt': 0,
            }
            _save(record, data)
    with _lock(subscription_id) as acquired:
        if not acquired:
            return False  # The snapshot is saved; the API retry loop will send it.
        if not record.exists():
            # Another process handled it; do not claim a confirmed acceptance.
            return False
        data = json.loads(record.read_text())
        if data['next_attempt'] > time.time():
            return False
        return _attempt(record, data)


def delivery_is_pending(csv_path, subscription_id):
    delivery_id = str(uuid5(NAMESPACE_URL, f"cartpe:{subscription_id}:{Path(csv_path).name}"))
    return (QUEUE / f"{delivery_id}.json").exists()


def _still_active(data):
    with DatabaseManager.get_connection() as conn:
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute(
                """SELECT id FROM api_subscriptions
                   WHERE id = %s AND buyer_domain = %s AND expires_at > NOW()
                     AND platform = 'shopify'""",
                (data['subscription_id'], data['buyer_domain']),
            )
            return cursor.fetchone() is not None
        finally:
            cursor.close()


def retry_pending_deliveries():
    if not QUEUE.exists():
        return
    for path in QUEUE.glob('*.json'):
        try:
            # IDs in metadata are written by this module, never by a request.
            data = json.loads(path.read_text())
            with _lock(data['subscription_id']) as acquired:
                if not acquired or not path.exists():
                    continue
                data = json.loads(path.read_text())
                if data['next_attempt'] > time.time():
                    continue
                if not _still_active(data):
                    _remove(path, data)
                    logger.warning("Removed pending Shopify CSV for inactive subscription %s", data['subscription_id'])
                    continue
                # The app accepts generation times within seven days only.
                if time.time() * 1000 - int(data['generated']) > 7 * 86400_000:
                    _remove(path, data)
                    logger.error("Pending Shopify CSV expired for subscription %s; generate a fresh export", data['subscription_id'])
                    continue
                _attempt(path, data)
        except FileNotFoundError:
            continue
        except Exception as error:
            # Do not log exception text/tracebacks: they can contain credentials.
            logger.error("Shopify retry deferred: %s", type(error).__name__)

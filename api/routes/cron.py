"""Cron control routes"""

import logging
import os
import re
import shlex
import subprocess

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import require_admin
from config.settings import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["cron"])

CRONTAB_BIN = "/usr/bin/crontab"
CRON_COMMAND = (
    f"cd {shlex.quote(str(settings.BASE_DIR))} && "
    f"{shlex.quote(str(settings.BASE_DIR / 'venv/bin/python'))} main.py "
    f">> {shlex.quote(str(settings.BASE_DIR / 'logs/cron.log'))} 2>&1"
)
CRON_FIELD = r"[\d*,/-]+"
CRON_PATTERN = re.compile(
    rf"^{CRON_FIELD}\s+{CRON_FIELD}\s+{CRON_FIELD}\s+{CRON_FIELD}\s+{CRON_FIELD}$"
)
CRONTAB_BACKUP = settings.BASE_DIR / "logs" / "crontab.backup"


def _get_crontab() -> str:
    """Return crontab text or empty if absent; raise HTTPException on failure."""

    try:
        result = subprocess.run(
            [CRONTAB_BIN, "-l"],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
            timeout=10,
            encoding="utf-8",
        )
    except subprocess.TimeoutExpired:
        logger.exception("Crontab command timed out; completion is unknown")
        raise HTTPException(
            status_code=504,
            detail="Cron operation timed out; check cron status before retrying",
        ) from None
    except UnicodeError:
        logger.exception("Crontab command encountered a text encoding error")
        raise HTTPException(
            status_code=500,
            detail="Cron configuration could not be processed as UTF-8",
        ) from None
    except OSError:
        logger.exception("Could not execute crontab")
        raise HTTPException(
            status_code=500,
            detail="Could not read scraper cron configuration",
        ) from None

    if result.returncode == 0:
        return result.stdout

    error = result.stderr.strip()

    if result.returncode == 1 and "no crontab for" in error.lower():
        return ""

    logger.error(f"Crontab read failed with exit code {result.returncode}: {error}")
    raise HTTPException(
        status_code=500,
        detail="Could not read scraper cron configuration",
    )


def _set_crontab(content: str) -> None:
    """Replace crontab, returning a clear API error on failure."""
    try:
        subprocess.run(
            [CRONTAB_BIN, "-"],
            input=content,
            text=True,
            capture_output=True,
            check=True,
            env={**os.environ, "LC_ALL": "C"},
            timeout=10,
            encoding="utf-8",
        )
    except subprocess.TimeoutExpired:
        logger.exception("Crontab command timed out; completion is unknown")
        raise HTTPException(
            status_code=504,
            detail="Cron operation timed out; check cron status before retrying",
        ) from None
    except UnicodeError:
        logger.exception("Crontab command encountered a text encoding error")
        raise HTTPException(
            status_code=500,
            detail="Cron configuration could not be processed as UTF-8",
        ) from None
    except subprocess.CalledProcessError as e:
        logger.error(f"Crontab update failed: {e.stderr}")
        raise HTTPException(
            status_code=500,
            detail="Could not update scraper cron configuration",
        ) from None
    except OSError:
        logger.exception("Could not execute crontab")
        raise HTTPException(
            status_code=500,
            detail="Could not update scraper cron configuration",
        ) from None


def _parse_cron_line(crontab: str) -> str | None:
    for line in crontab.splitlines():
        if CRON_COMMAND in line:
            return line
    return None


def _validate_cron_schedule(schedule: str) -> bool:
    """Check that a cron schedule has 5 valid fields"""
    return bool(CRON_PATTERN.match(schedule.strip()))


def _backup_crontab(content: str) -> None:
    """Save current crontab before replacing it"""
    if not content.strip():
        return
    try:
        CRONTAB_BACKUP.parent.mkdir(parents=True, exist_ok=True)
        CRONTAB_BACKUP.write_text(content)
    except OSError as e:
        logger.warning(f"Could not backup crontab: {e}")


class CronUpdateRequest(BaseModel):
    schedule: str  # e.g. "0 23 * * *"


@router.get("/cron/status", dependencies=[Depends(require_admin)])
def get_cron_status() -> dict:
    """Report whether the scraper is scheduled, and when"""
    line = _parse_cron_line(_get_crontab())
    if not line:
        return {"enabled": False, "schedule": None}

    return {
        "enabled": not line.strip().startswith("#"),
        "schedule": " ".join(line.lstrip("#").strip().split()[:5]),
    }


@router.post("/cron/update", dependencies=[Depends(require_admin)])
def update_cron_schedule(request: CronUpdateRequest) -> dict:
    """Set the scraper's schedule"""
    if not _validate_cron_schedule(request.schedule):
        raise HTTPException(
            status_code=400,
            detail="Invalid cron schedule. Expected 5 fields: minute hour day month weekday",
        )

    current = _get_crontab()
    _backup_crontab(current)

    new_line = f"{request.schedule} {CRON_COMMAND}"
    new_lines = []
    updated = False

    for line in current.splitlines():
        if CRON_COMMAND in line:
            new_lines.append(new_line)
            updated = True
        else:
            new_lines.append(line)

    if not updated:
        new_lines.append(new_line)

    _set_crontab("\n".join(new_lines) + "\n")
    return {"success": True, "schedule": request.schedule}


@router.post("/cron/toggle", dependencies=[Depends(require_admin)])
def toggle_cron() -> dict:
    """Enable or disable the scraper's schedule"""
    current = _get_crontab()
    if _parse_cron_line(current) is None:
        raise HTTPException(
            status_code=404,
            detail="Scraper cron schedule not found",
        )
    _backup_crontab(current)

    new_lines = []
    toggled_to = None

    for line in current.splitlines():
        if CRON_COMMAND in line:
            if line.strip().startswith("#"):
                line = line.lstrip("#").strip()
                toggled_to = "enabled"
            else:
                line = f"# {line}"
                toggled_to = "disabled"
        new_lines.append(line)

    _set_crontab("\n".join(new_lines) + "\n")
    return {"success": True, "status": toggled_to}

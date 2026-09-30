"""Cron control routes"""

import logging
import subprocess

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api.deps import require_admin
from config.settings import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["cron"])

CRONTAB_BIN = "/usr/bin/crontab"
CRON_COMMAND = (
    f"cd {settings.BASE_DIR} && {settings.BASE_DIR}/venv/bin/python main.py "
    f">> {settings.BASE_DIR}/logs/cron.log 2>&1"
)


def _get_crontab() -> str:
    """Return the current crontab, or an empty string if the user has none"""
    result = subprocess.run(
        [CRONTAB_BIN, "-l"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else ""


def _set_crontab(content: str) -> None:
    """Replace the crontab, raising if `crontab` rejects it"""
    try:
        subprocess.run(
            [CRONTAB_BIN, "-"],
            input=content,
            text=True,
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to set crontab: {e.stderr}")
        raise RuntimeError(f"crontab update failed: {e.stderr}") from e


def _parse_cron_line(crontab: str) -> str | None:
    for line in crontab.splitlines():
        if CRON_COMMAND in line:
            return line
    return None


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
    new_line = f"{request.schedule} {CRON_COMMAND}"
    new_lines = []
    updated = False

    for line in _get_crontab().splitlines():
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
    new_lines = []
    toggled_to = None

    for line in _get_crontab().splitlines():
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

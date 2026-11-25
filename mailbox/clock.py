"""The household clock (ruling S117).

Both stacks run on one zone, named by TZ. The C library resolves TZ through
/usr/share/zoneinfo and falls back to UTC in silence when the zone file is
missing; resolving it here through zoneinfo instead, and printing what was
resolved with its offset, makes a UTC fallback visible in the startup line.
"""
import os
from datetime import datetime, tzinfo
from typing import Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def household_zone(environ: Mapping[str, str] = os.environ) -> tzinfo:
    """ZoneInfo(TZ) when TZ names a zone the box knows; otherwise the zone the
    C library resolved (UTC on a box without tzdata), so the log says so."""
    name = (environ.get("TZ") or "").strip()
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            pass
    return datetime.now().astimezone().tzinfo


def describe_zone(zone: tzinfo, now: Optional[datetime] = None) -> str:
    """`Etc/UTC +0000 UTC`: the zone, its offset now, its abbreviation."""
    local = (now or datetime.now(zone)).astimezone(zone)
    name = getattr(zone, "key", None) or local.tzname() or "unknown"
    return f"{name} {local.strftime('%z')} {local.tzname()}"

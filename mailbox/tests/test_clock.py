"""The household clock (ruling S117, final-review C1): TZ resolved through
zoneinfo, the zone and its offset printed, the capture clock zone-aware."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from mailbox.clock import describe_zone, household_zone
from mailbox.service import Service
from mailbox.store import Store
from mailbox.telegram import FakeTelegramClient


def test_household_zone_resolves_tz_through_zoneinfo():
    assert household_zone({"TZ": "Europe/Amsterdam"}) == ZoneInfo("Europe/Amsterdam")


def test_household_zone_falls_back_to_the_process_zone_when_tz_is_unset_or_unknown():
    process = datetime.now().astimezone().tzinfo
    assert household_zone({}) == process
    assert household_zone({"TZ": "Nowhere/Nothing"}) == process
    assert household_zone({"TZ": "../etc/passwd"}) == process


def test_describe_zone_prints_the_zone_and_its_offset():
    summer = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    winter = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
    assert describe_zone(ZoneInfo("Europe/Amsterdam"), summer) == "Europe/Amsterdam +0200 CEST"
    assert describe_zone(ZoneInfo("Europe/Amsterdam"), winter) == "Europe/Amsterdam +0100 CET"
    # No tzdata (or no TZ): the fallback names what the C library resolved, so a UTC box shows as one.
    assert describe_zone(timezone.utc, summer) == "UTC +0000 UTC"


def test_service_clock_is_aware_in_the_household_zone(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Amsterdam")
    store = Store(":memory:")
    store.open()
    try:
        service = Service(store, FakeTelegramClient(), "personal")
        now = service.now()
        assert now.tzinfo == ZoneInfo("Europe/Amsterdam")
        assert abs((now - datetime.now(timezone.utc)).total_seconds()) < 5
    finally:
        store.close()


def test_service_takes_an_explicit_zone():
    store = Store(":memory:")
    store.open()
    try:
        service = Service(store, FakeTelegramClient(), "personal", zone=ZoneInfo("Pacific/Auckland"))
        assert service.now().tzinfo == ZoneInfo("Pacific/Auckland")
    finally:
        store.close()

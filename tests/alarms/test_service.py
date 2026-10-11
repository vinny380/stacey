import asyncio
import math
from datetime import UTC, datetime, timedelta

import pytest

from stacy.alarms.service import AlarmService, resolve_alarm


def test_next_local_alarm_and_rollover():
    before = datetime(2026, 10, 9, 10, 0, tzinfo=UTC)
    after = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    assert resolve_alarm("07:00", "America/Toronto", before).hour == 11
    assert resolve_alarm("07:00", "America/Toronto", after).day == 10


def test_alarm_result_explicitly_reports_today_or_tomorrow(tmp_path, monkeypatch):
    import stacy.alarms.service as module

    class Clock(datetime):
        instant = datetime(2026, 10, 10, 4, 30, tzinfo=UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.instant.astimezone(tz) if tz else cls.instant.replace(tzinfo=None)

    monkeypatch.setattr(module, "datetime", Clock)

    async def noop(value):
        pass

    service = AlarmService(tmp_path / "state.sqlite3", "America/Toronto", noop, noop)
    try:
        today = service.alarm("07:00")
        assert today["relative_day"] == "today"
        assert today["local_deadline"] == "2026-10-10T07:00:00-04:00"
        Clock.instant = datetime(2026, 10, 10, 12, tzinfo=UTC)
        tomorrow = service.alarm("07:00")
        assert tomorrow["relative_day"] == "tomorrow"
        assert tomorrow["local_deadline"] == "2026-10-11T07:00:00-04:00"
    finally:
        service.close()


@pytest.mark.parametrize("when", ["2027-03-14T02:30:00", "2027-11-07T01:30:00"])
def test_dst_requires_unambiguous_time(when):
    with pytest.raises(ValueError):
        resolve_alarm(when, "America/Toronto", datetime(2026, 1, 1, tzinfo=UTC))


def test_explicit_offset_resolves_ambiguous_alarm():
    result = resolve_alarm(
        "2027-11-07T01:30:00-05:00", "America/Toronto", datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert result == datetime(2027, 11, 7, 6, 30, tzinfo=UTC)


def test_timer_fires_without_model_and_can_be_stopped(tmp_path):
    async def scenario():
        fired = asyncio.Event()
        silenced = []

        async def ring(alarm):
            fired.set()

        async def silence(alarm_id):
            silenced.append(alarm_id)

        service = AlarmService(tmp_path / "state.sqlite3", "America/Toronto", ring, silence)
        await service.start()
        timer = service.timer(0.05)
        await asyncio.wait_for(fired.wait(), 3)
        assert service.get(timer["id"])["status"] == "ringing"
        await service.stop()
        assert silenced == [timer["id"]]
        assert service.get(timer["id"])["status"] == "completed"
        service.close()
        await asyncio.sleep(0)

    asyncio.run(scenario())


def test_restart_restores_pending_events_and_reports_missed(tmp_path):
    async def noop(value):
        pass

    async def scenario():
        path = tmp_path / "state.sqlite3"
        first = AlarmService(path, "America/Toronto", noop, noop)
        pending = first.timer(60)
        cancelled = first.timer(60)
        missed = first.timer(60)
        with first.connection:
            first.connection.execute(
                "UPDATE alarms SET deadline=? WHERE id=?",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), missed["id"]),
            )
        await first.cancel(cancelled["id"])
        first.close()
        second = AlarmService(path, "America/Toronto", noop, noop)
        await second.start()
        assert second.scheduler.get_job(pending["id"]) is not None
        assert second.scheduler.get_job(cancelled["id"]) is None
        assert second.get(missed["id"])["status"] == "missed"
        second.close()
        await asyncio.sleep(0)

    asyncio.run(scenario())


def test_snooze_reschedules_same_event(tmp_path):
    async def noop(value):
        pass

    async def scenario():
        service = AlarmService(tmp_path / "state.sqlite3", "America/Toronto", noop, noop)
        await service.start()
        timer = service.timer(60)
        await service._fire(timer["id"])
        snoozed = await service.snooze(timer["id"], 300)
        assert snoozed["status"] == "pending"
        assert snoozed["id"] == timer["id"]
        assert service.scheduler.get_job(timer["id"]) is not None
        service.close()
        await asyncio.sleep(0)

    asyncio.run(scenario())


@pytest.mark.parametrize("duration", [0, -1, math.inf, math.nan])
def test_invalid_timer_duration(tmp_path, duration):
    async def noop(value):
        pass

    service = AlarmService(tmp_path / "state.sqlite3", "America/Toronto", noop, noop)
    with pytest.raises(ValueError):
        service.timer(duration)
    service.close()

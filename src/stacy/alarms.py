from __future__ import annotations

import logging
import math
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)
Alarm = dict[str, str]


def resolve_alarm(when: str, timezone: str, now: datetime | None = None) -> datetime:
    """Resolve HH:MM (next occurrence) or a local/offset ISO datetime into UTC.

    Ambiguous and nonexistent DST wall times require an explicit UTC offset.
    """
    zone = ZoneInfo(timezone)
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must include a timezone")
    local_now = now.astimezone(zone)
    clock_only = len(when) == 5 and when[2] == ":"
    if clock_only:
        try:
            hour, minute = map(int, when.split(":"))
            naive = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except ValueError as error:
            raise ValueError("Use a valid 24-hour time, such as 07:00") from error
        naive = naive.replace(tzinfo=None)
        if naive <= local_now.replace(tzinfo=None):
            naive += timedelta(days=1)
    else:
        try:
            naive = datetime.fromisoformat(when)
        except ValueError as error:
            raise ValueError("Use HH:MM or an ISO datetime") from error
        if naive.tzinfo is not None:
            result = naive.astimezone(UTC)
            if result <= now:
                raise ValueError("An alarm must be in the future")
            return result

    first = naive.replace(tzinfo=zone, fold=0)
    second = naive.replace(tzinfo=zone, fold=1)
    if first.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != naive:
        raise ValueError("This local time does not exist because of daylight saving time")
    if first.utcoffset() != second.utcoffset():
        raise ValueError("This local time is ambiguous; provide an ISO datetime with a UTC offset")
    result = first.astimezone(UTC)
    if result <= now:
        raise ValueError("An alarm must be in the future")
    return result


class AlarmService:
    """SQLite owns alarm state; APScheduler dispatches persisted future deadlines."""

    def __init__(
        self,
        database: Path,
        timezone: str,
        on_ring: Callable[[Alarm], Awaitable[None]],
        on_silence: Callable[[str], Awaitable[None]],
    ):
        database.parent.mkdir(parents=True, exist_ok=True)
        self.database = database
        self.connection = sqlite3.connect(database)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS alarms (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                label TEXT NOT NULL,
                deadline TEXT NOT NULL,
                timezone TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        self.connection.commit()
        self.timezone = timezone
        ZoneInfo(timezone)
        self.on_ring = on_ring
        self.on_silence = on_silence
        self.scheduler = AsyncIOScheduler(timezone=UTC)
        self.started = False

    async def start(self) -> None:
        if self.started:
            return
        self.scheduler.start()
        self.started = True
        now = datetime.now(UTC)
        for alarm in self.list():
            if alarm["status"] == "ringing":
                await self.on_ring(alarm)
            elif datetime.fromisoformat(alarm["deadline"]) <= now:
                self._status(alarm["id"], "missed")
                logger.warning("Missed %s: %s", alarm["kind"], alarm["id"])
            else:
                self._schedule(alarm)

    def close(self) -> None:
        if self.started:
            self.scheduler.shutdown(wait=False)
            self.started = False
        self.connection.close()

    def _status(self, alarm_id: str, status: str) -> None:
        with self.connection:
            self.connection.execute("UPDATE alarms SET status=? WHERE id=?", (status, alarm_id))

    def _schedule(self, alarm: Alarm) -> None:
        self.scheduler.add_job(
            self._fire,
            "date",
            run_date=datetime.fromisoformat(alarm["deadline"]),
            args=[alarm["id"]],
            id=alarm["id"],
            replace_existing=True,
            misfire_grace_time=None,
        )

    async def _fire(self, alarm_id: str) -> None:
        alarm = self.get(alarm_id)
        if alarm["status"] != "pending":
            return
        self._status(alarm_id, "ringing")
        await self.on_ring(self.get(alarm_id))

    def get(self, alarm_id: str) -> Alarm:
        row = self.connection.execute("SELECT * FROM alarms WHERE id=?", (alarm_id,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown alarm or timer: {alarm_id}")
        return self._describe(row)

    def _describe(self, row: sqlite3.Row) -> Alarm:
        alarm = dict(row)
        zone = ZoneInfo(alarm["timezone"])
        local = datetime.fromisoformat(alarm["deadline"]).astimezone(zone)
        days = (local.date() - datetime.now(zone).date()).days
        alarm["local_deadline"] = local.isoformat()
        alarm["relative_day"] = {0: "today", 1: "tomorrow"}.get(days, local.date().isoformat())
        return alarm

    def list(self, include_finished: bool = False) -> list[Alarm]:
        query = "SELECT * FROM alarms"
        if not include_finished:
            query += " WHERE status IN ('pending', 'ringing')"
        query += " ORDER BY deadline"
        return [self._describe(row) for row in self.connection.execute(query)]

    def _create(self, kind: str, deadline: datetime, label: str) -> Alarm:
        alarm_id = uuid4().hex[:12]
        with self.connection:
            self.connection.execute(
                "INSERT INTO alarms VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    alarm_id,
                    kind,
                    label,
                    deadline.astimezone(UTC).isoformat(),
                    self.timezone,
                    "pending",
                    datetime.now(UTC).isoformat(),
                ),
            )
        alarm = self.get(alarm_id)
        if self.started:
            self._schedule(alarm)
        local_deadline = deadline.astimezone(ZoneInfo(self.timezone)).isoformat()
        logger.info(
            "Saved %s (%s): %s — %s [%s]",
            kind,
            alarm_id,
            label,
            local_deadline,
            self.timezone,
        )
        return alarm

    def timer(self, seconds: float, label: str = "Timer") -> Alarm:
        if not math.isfinite(seconds) or seconds <= 0 or seconds > 365 * 86400:
            raise ValueError("Timer duration must be positive and no longer than one year")
        return self._create("timer", datetime.now(UTC) + timedelta(seconds=seconds), label)

    def alarm(self, when: str, label: str = "Wake-up alarm") -> Alarm:
        return self._create("alarm", resolve_alarm(when, self.timezone), label)

    async def cancel(self, alarm_id: str) -> Alarm:
        alarm = self.get(alarm_id)
        if alarm["status"] in {"pending", "ringing"}:
            self._status(alarm_id, "cancelled")
            if self.scheduler.get_job(alarm_id):
                self.scheduler.remove_job(alarm_id)
            await self.on_silence(alarm_id)
        return self.get(alarm_id)

    async def stop(self, alarm_id: str | None = None) -> list[Alarm]:
        selected = [self.get(alarm_id)] if alarm_id else self.list()
        stopped = []
        for alarm in selected:
            if alarm["status"] == "ringing":
                self._status(alarm["id"], "completed")
                await self.on_silence(alarm["id"])
                stopped.append(self.get(alarm["id"]))
        return stopped

    async def snooze(self, alarm_id: str, seconds: float = 540) -> Alarm:
        if not math.isfinite(seconds) or seconds <= 0 or seconds > 86400:
            raise ValueError("Snooze must be positive and no longer than one day")
        alarm = self.get(alarm_id)
        if alarm["status"] != "ringing":
            raise ValueError("Only a ringing alarm or timer can be snoozed")
        with self.connection:
            self.connection.execute(
                "UPDATE alarms SET deadline=?, status='pending' WHERE id=?",
                ((datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(), alarm_id),
            )
        await self.on_silence(alarm_id)
        updated = self.get(alarm_id)
        if self.started:
            self._schedule(updated)
        return updated

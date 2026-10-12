"""In-process prayer scheduler. Replaces the cron-rewriting approach:
computes today's times with the praytimes engine and schedules playback,
reminder, and suhoor jobs directly. Recomputes just after midnight and
whenever settings change (via reschedule()).
"""
import asyncio
import logging
import time as time_mod
from datetime import date, datetime, timedelta
from pathlib import Path

from apscheduler.events import EVENT_JOB_SUBMITTED
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

import db
import hijri
from config import (CHIME_FILE, DEFAULT_ASR_METHOD, DEFAULT_HIGH_LATS,
                    DEFAULT_METHOD, PRAYER_NAMES)
from events import emit
from hooks import run_hooks
from player import PlaybackStopped, player
from praytimes import PrayTimes

scheduler = AsyncIOScheduler()
TIMESYNC_DIR = Path("/run/systemd/timesync")
_clock_sample = None
_clock_task = None
_log = logging.getLogger(__name__)
_dispatched_jobs = set()

_JOB_PREFIXES = ("prayer-", "reminder-", "suhoor", "hook-")


def _engine() -> PrayTimes:
    pt = PrayTimes()
    pt.setMethod(db.get_setting("method", DEFAULT_METHOD))
    pt.adjust({
        "asr": db.get_setting("asr_method", DEFAULT_ASR_METHOD),
        "highLats": db.get_setting("high_lats", DEFAULT_HIGH_LATS),
    })
    pt.tune({p["name"]: p["offset_minutes"] or 0 for p in db.get_prayers()})
    return pt


def compute_times(day: date | None = None) -> dict | None:
    """{'times': {fajr..isha: HH:MM}, 'extras': {imsak, sunrise, midnight}} or None if location unset."""
    lat, lng = db.get_setting("lat"), db.get_setting("lng")
    if lat is None or lng is None:
        return None
    day = day or date.today()
    utc_offset = -(time_mod.timezone / 3600)
    is_dst = time_mod.localtime().tm_isdst
    t = _engine().getTimes((day.year, day.month, day.day), (lat, lng), utc_offset, is_dst)
    return {
        "times": {name: t[name] for name in PRAYER_NAMES},
        "extras": {name: t[name] for name in ("imsak", "sunrise", "midnight")},
    }


# ---------- mute / skip ----------

def is_muted() -> bool:
    until = db.get_setting("mute_until")
    return bool(until and datetime.fromisoformat(until) > datetime.now())


def consume_skip(name: str) -> bool:
    skip = db.get_setting("skip_next")
    if skip and skip.get("name") == name and skip.get("date") == date.today().isoformat():
        db.set_setting("skip_next", None)
        return True
    return False


# ---------- playback jobs ----------

async def play_prayer(name: str, volume_override: int | None = None) -> None:
    prayer = db.get_prayer(name)
    if not prayer or not prayer["enabled"]:
        return
    volume = prayer["volume"] if volume_override is None else volume_override
    if is_muted():
        await emit("skipped", f"{name} adhan not played (muted)")
        return
    if consume_skip(name):
        await emit("skipped", f"{name} adhan skipped (one-time skip)")
        return

    mp3, fade = prayer["mp3"], 0
    if name == "dhuhr" and datetime.now().weekday() == 4:  # Friday
        action = db.get_setting("jumuah_action", "normal")
        if action == "skip":
            await emit("skipped", "dhuhr adhan skipped (jumu'ah)")
            return
        if action == "mp3":
            mp3 = db.get_setting("jumuah_mp3") or mp3
    if name == "fajr":
        fade = int(db.get_setting("fajr_fade_seconds", 0) or 0)

    try:
        detail = f"Playing {name} adhan ({mp3}, volume {volume}"
        detail += f", {fade}s fade-in)" if fade else ")"
        await run_hooks("before", name)
        playback = await player.play(mp3, volume, prayer["device"], label=name, fade=fade)
        await emit("adhan", detail)
        await player.wait_until_done(playback)
        if prayer.get("dua_mp3"):
            playback = await player.play(prayer["dua_mp3"], volume, prayer["device"], label=f"{name} dua")
            await player.wait_until_done(playback)
        await run_hooks("after", name)
        await emit("adhan", f"Finished {name} adhan")
    except PlaybackStopped as exc:
        await emit("skipped", f"{name} adhan interrupted: {exc}")
    except Exception as exc:
        await emit("error", f"{name} adhan failed: {exc}")


async def play_reminder(name: str, volume_override: int | None = None) -> None:
    prayer = db.get_prayer(name)
    if not prayer or not prayer["enabled"] or (is_muted() and volume_override is None):
        return
    volume = prayer["volume"] if volume_override is None else volume_override
    try:
        await player.play(CHIME_FILE, volume, prayer["device"], label=f"{name} reminder")
        await emit("reminder", f"{name} in {prayer['reminder_minutes']} minutes")
    except PlaybackStopped as exc:
        await emit("skipped", f"{name} reminder interrupted: {exc}")
    except Exception as exc:
        await emit("error", f"{name} reminder failed: {exc}")


async def play_suhoor(force: bool = False, volume_override: int | None = None) -> None:
    if not force and (not hijri.ramadan_active() or is_muted()):
        return
    fajr = db.get_prayer("fajr")
    mp3 = db.get_setting("suhoor_mp3") or CHIME_FILE
    volume = fajr["volume"] if volume_override is None else volume_override
    try:
        await emit("ramadan", f"Suhoor alarm ({mp3})")
        await player.play(mp3, volume, fajr["device"], label="suhoor")
    except PlaybackStopped as exc:
        await emit("skipped", f"Suhoor alarm interrupted: {exc}")
    except Exception as exc:
        await emit("error", f"Suhoor alarm failed: {exc}")


# ---------- scheduling ----------

def record_dispatch(event) -> None:
    if event.job_id.startswith(_JOB_PREFIXES):
        for run_time in event.scheduled_run_times:
            _dispatched_jobs.add((event.job_id, run_time.astimezone().date()))


def _add_playback_job(func, trigger, **options) -> None:
    key = (options["id"], trigger.run_date.astimezone().date())
    if key not in _dispatched_jobs:
        scheduler.add_job(func, trigger, **options)


scheduler.add_listener(record_dispatch, EVENT_JOB_SUBMITTED)

def schedule_today() -> dict | None:
    """(Re)create today's jobs for prayers, reminders, and suhoor still in the future."""
    for job in scheduler.get_jobs():
        if job.id.startswith(_JOB_PREFIXES):
            job.remove()
    data = compute_times()
    if data is None:
        return None
    now = datetime.now()

    def today_at(hhmm: str) -> datetime:
        hour, minute = map(int, hhmm.split(":"))
        return now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    for name, hhmm in data["times"].items():
        prayer = db.get_prayer(name)
        if not prayer or not prayer["enabled"]:
            continue
        run_at = today_at(hhmm)
        if run_at > now:
            _add_playback_job(play_prayer, DateTrigger(run_date=run_at),
                              args=[name], id=f"prayer-{name}", replace_existing=True)
        reminder = prayer["reminder_minutes"] or 0
        if reminder > 0:
            remind_at = run_at - timedelta(minutes=reminder)
            if remind_at > now:
                _add_playback_job(play_reminder, DateTrigger(run_date=remind_at),
                                  args=[name], id=f"reminder-{name}", replace_existing=True)

    if hijri.ramadan_active() and db.get_setting("suhoor_enabled"):
        minutes = int(db.get_setting("suhoor_minutes", 45) or 45)
        suhoor_at = today_at(data["times"]["fajr"]) - timedelta(minutes=minutes)
        if suhoor_at > now:
            _add_playback_job(play_suhoor, DateTrigger(run_date=suhoor_at),
                              id="suhoor", replace_existing=True)

    # offset hooks: independently scheduled at prayer time +/- offset
    from hooks import run_scheduled_hook  # late import to avoid a cycle
    weekday = now.weekday()
    for hook in db.get_hooks():
        offset = hook.get("offset_minutes") or 0
        if not hook["enabled"] or offset == 0 or weekday not in hook["days"]:
            continue
        for prayer in hook["prayers"]:
            run_at = today_at(data["times"][prayer]) + timedelta(minutes=offset)
            if run_at > now:
                _add_playback_job(run_scheduled_hook, DateTrigger(run_date=run_at),
                                  args=[hook["id"], prayer],
                                  id=f"hook-{hook['id']}-{prayer}", replace_existing=True)
    return data


async def reschedule(reason: str = "settings changed") -> None:
    data = schedule_today()
    if data:
        times = ", ".join(f"{k} {v}" for k, v in data["times"].items())
        await emit("schedule", f"Rescheduled ({reason}): {times}")
    else:
        await emit("schedule", f"Location not set — nothing scheduled ({reason})")


async def wait_for_clock_sync() -> None:
    """On timesyncd hosts, never build prayer timers from the boot clock."""
    if TIMESYNC_DIR.is_dir():
        while not (TIMESYNC_DIR / "synchronized").exists():
            _log.warning("Waiting for initial system clock synchronization")
            await asyncio.sleep(5)


async def check_clock() -> None:
    """Compare wall time with monotonic time, independent of APScheduler timers."""
    global _clock_sample
    sample = (time_mod.time(), time_mod.monotonic(), date.today())
    previous = _clock_sample
    if previous is not None:
        shift = (sample[0] - previous[0]) - (sample[1] - previous[1])
        if abs(shift) > 2 or sample[2] != previous[2]:
            # Replace the cron job too: a backward jump can leave its next run
            # on the wrong day. Only future playback jobs are rebuilt.
            _add_daily_job()
            await reschedule(f"system clock changed ({shift:+.1f}s)")
    _clock_sample = sample


async def _monitor_clock() -> None:
    while True:
        await asyncio.sleep(5)
        try:
            await check_clock()
        except Exception:
            _log.exception("Clock recovery failed; will retry")


def _add_daily_job() -> None:
    scheduler.add_job(
        reschedule, CronTrigger(hour=0, minute=5),
        kwargs={"reason": "daily recompute"}, id="daily-recompute",
        replace_existing=True,
    )


def start() -> None:
    global _clock_sample, _clock_task
    _dispatched_jobs.clear()
    _add_daily_job()
    scheduler.start()
    schedule_today()
    _clock_sample = (time_mod.time(), time_mod.monotonic(), date.today())
    _clock_task = asyncio.create_task(_monitor_clock())


def shutdown() -> None:
    global _clock_task, _clock_sample
    if _clock_task is not None:
        _clock_task.cancel()
        _clock_task = None
    _clock_sample = None
    if scheduler.running:
        scheduler.shutdown(wait=False)


def next_prayer() -> dict | None:
    """The soonest scheduled prayer job, for the status endpoint."""
    upcoming = [
        {"name": j.id.removeprefix("prayer-"), "at": j.next_run_time.isoformat()}
        for j in scheduler.get_jobs()
        if j.id.startswith("prayer-") and j.next_run_time
    ]
    return min(upcoming, key=lambda p: p["at"]) if upcoming else None


def scheduled_jobs() -> list[dict]:
    """Everything queued for today, for the status endpoint / debugging."""
    jobs = [
        {"id": j.id, "at": j.next_run_time.isoformat()}
        for j in scheduler.get_jobs()
        if j.id.startswith(_JOB_PREFIXES) and j.next_run_time
    ]
    return sorted(jobs, key=lambda j: j["at"])

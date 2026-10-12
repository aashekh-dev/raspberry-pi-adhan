import asyncio
from datetime import datetime

import pytest
from freezegun import freeze_time

import scheduler
import db
from apscheduler.events import EVENT_JOB_SUBMITTED, JobSubmissionEvent
from test_scheduler import FIXED_TIMES


@pytest.mark.parametrize("jump", [13800, -3600, 5])
def test_clock_jump_rebuilds_future_jobs(temp_db, monkeypatch, jump):
    monkeypatch.setattr(scheduler, "compute_times", lambda: FIXED_TIMES)
    monotonic = [100.0]

    async def scenario():
        with freeze_time("2026-10-12 12:00:00", real_asyncio=True) as clock:
            monkeypatch.setattr(scheduler.time_mod, "monotonic", lambda: monotonic[0])
            scheduler.start()
            try:
                assert scheduler.scheduler.get_job("prayer-dhuhr") is not None
                clock.tick(jump)
                monotonic[0] += 5
                await scheduler.check_clock()
                jobs = scheduler.scheduled_jobs()
                assert all(datetime.fromisoformat(j["at"]).replace(tzinfo=None)
                           > datetime.now() for j in jobs)
                assert scheduler.scheduler.get_job("prayer-asr") is not None
                if jump > 5:
                    assert scheduler.scheduler.get_job("prayer-dhuhr") is None
                else:
                    assert scheduler.scheduler.get_job("prayer-dhuhr") is not None
                assert any("clock" in e["detail"] for e in db.get_events()) == (jump != 5)
            finally:
                scheduler.shutdown()

    asyncio.run(scenario())


def test_startup_waits_for_timesync_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "TIMESYNC_DIR", tmp_path, raising=False)

    async def scenario():
        task = asyncio.create_task(scheduler.wait_for_clock_sync())
        await asyncio.sleep(0)
        assert not task.done()
        (tmp_path / "synchronized").touch()
        await asyncio.wait_for(task, timeout=6)

    asyncio.run(scenario())


def test_startup_without_timesyncd_does_not_wait(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "TIMESYNC_DIR", tmp_path / "missing", raising=False)
    asyncio.run(scheduler.wait_for_clock_sync())


def test_backward_jump_does_not_requeue_dispatched_prayer(temp_db, monkeypatch):
    monkeypatch.setattr(scheduler, "compute_times", lambda: FIXED_TIMES)

    async def scenario():
        with freeze_time("2026-10-12 12:00:00", real_asyncio=True) as clock:
            monotonic = [100.0]
            monkeypatch.setattr(scheduler.time_mod, "monotonic", lambda: monotonic[0])
            scheduler.start()
            try:
                job = scheduler.scheduler.get_job("prayer-dhuhr")
                scheduler.record_dispatch(JobSubmissionEvent(
                    EVENT_JOB_SUBMITTED, job.id, "default", [job.next_run_time]))
                job.remove()
                clock.tick(-3600)
                monotonic[0] += 5
                await scheduler.check_clock()
                assert scheduler.scheduler.get_job("prayer-dhuhr") is None
                assert scheduler.scheduler.get_job("prayer-asr") is not None
            finally:
                scheduler.shutdown()

    asyncio.run(scenario())

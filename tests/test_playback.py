"""Exercise playback lifecycle with real child processes instead of a speaker."""
import asyncio
import sys

import pytest

import audio
import db
import player
import scheduler


@pytest.fixture
def playback(monkeypatch, tmp_path, temp_db):
    media = tmp_path / "test.mp3"
    media.write_bytes(b"test")
    monkeypatch.setattr(player, "MEDIA_DIR", tmp_path)
    monkeypatch.setattr(player, "IPC_SOCKET", tmp_path / "mpv.sock")
    async def ready(device):
        pass
    monkeypatch.setattr(audio, "ensure_ready", ready)
    return player.Player()


def child(monkeypatch, code):
    monkeypatch.setattr(player, "_build_cmd", lambda *args, **kwargs:
                        ([sys.executable, "-c", code], False))


def test_failure_exit_and_stderr_survive_reaping(playback, monkeypatch):
    child(monkeypatch, "import sys; print('A2DP output failed', file=sys.stderr); sys.exit(7)")
    async def run():
        # An immediate startup failure may be raised by play() or its waiter.
        with pytest.raises(RuntimeError, match="exit 7.*A2DP output failed"):
            await playback.play("test.mp3", 80)
            await asyncio.sleep(0.1)
            await playback.wait_until_done()
        assert playback.current is None
    asyncio.run(run())
    errors = [e["detail"] for e in db.get_events() if e["type"] == "error"]
    assert any("exit 7" in error and "A2DP output failed" in error for error in errors)


def test_stderr_is_drained_and_bounded(playback, monkeypatch):
    child(monkeypatch, "import sys; sys.stderr.write('x' * 200000 + 'last diagnostic'); sys.exit(2)")
    async def run():
        with pytest.raises(RuntimeError, match="last diagnostic") as caught:
            await playback.play("test.mp3", 80)
            await asyncio.wait_for(playback.wait_until_done(), 3)
        assert len(str(caught.value)) < 5000
    asyncio.run(run())


def test_successful_completion(playback, monkeypatch):
    child(monkeypatch, "pass")
    async def run():
        await playback.play("test.mp3", 80)
        await playback.wait_until_done()
        assert not playback.playing
        assert playback.current is None
    asyncio.run(run())
    assert not any(e["type"] == "error" for e in db.get_events())


def test_stop_does_not_report_a_player_failure(playback, monkeypatch):
    child(monkeypatch, "import time; time.sleep(30)")
    async def run():
        await playback.play("test.mp3", 80)
        await playback.set_pause(True)
        await playback.stop()
        assert not playback.playing
        assert playback.current is None
    asyncio.run(run())
    assert any(e["type"] == "stopped" for e in db.get_events())
    assert not any(e["type"] == "error" for e in db.get_events())


def test_readiness_failure_prevents_spawning(playback, monkeypatch):
    child(monkeypatch, "pass")
    async def ready(device):
        raise RuntimeError("JBL unavailable")
    monkeypatch.setattr(audio, "ensure_ready", ready)
    async def run():
        with pytest.raises(RuntimeError, match="JBL unavailable"):
            await playback.play("test.mp3", 80, "pulse/jbl")
        assert not playback.playing
    asyncio.run(run())


def test_stop_cancels_pending_recovery(playback, monkeypatch):
    child(monkeypatch, "import time; time.sleep(30)")
    async def run():
        entered = asyncio.Event()
        async def ready(device):
            entered.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(audio, "ensure_ready", ready)
        pending = asyncio.create_task(playback.play("test.mp3", 80, "pulse/jbl"))
        await asyncio.wait_for(entered.wait(), 1)
        await playback.stop()
        with pytest.raises(RuntimeError, match="interrupted"):
            await asyncio.wait_for(pending, 1)
        assert not playback.playing
    asyncio.run(run())


def test_new_play_replaces_pending_recovery(playback, monkeypatch):
    child(monkeypatch, "pass")
    async def run():
        entered = asyncio.Event()
        async def ready(device):
            if device == "pulse/jbl":
                entered.set()
                await asyncio.Event().wait()
        monkeypatch.setattr(audio, "ensure_ready", ready)
        first = asyncio.create_task(playback.play("test.mp3", 80, "pulse/jbl"))
        await entered.wait()
        await playback.play("test.mp3", 80)
        with pytest.raises(RuntimeError, match="interrupted"):
            await first
        await playback.wait_until_done()
    asyncio.run(run())


def test_waiter_observes_its_own_replaced_process(playback, monkeypatch):
    child(monkeypatch, "import time; time.sleep(30)")
    async def run():
        first = await playback.play("test.mp3", 80)
        child(monkeypatch, "pass")
        await playback.play("test.mp3", 80)
        with pytest.raises(RuntimeError, match="interrupted"):
            await playback.wait_until_done(first)
        await playback.wait_until_done()
    asyncio.run(run())


def test_stop_invalidates_completion_during_final_broadcast(playback, monkeypatch):
    child(monkeypatch, "pass")
    async def run():
        cleared, release = asyncio.Event(), asyncio.Event()
        async def broadcast():
            if playback.current is None:
                cleared.set()
                await release.wait()
        monkeypatch.setattr(playback, "_broadcast", broadcast)
        first = await playback.play("test.mp3", 80)
        await cleared.wait()
        await playback.stop()
        release.set()
        with pytest.raises(RuntimeError, match="interrupted"):
            await playback.wait_until_done(first)
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["reminder", "suhoor"])
def test_stopped_alarm_recovery_is_not_a_failure(playback, monkeypatch, kind):
    child(monkeypatch, "pass")
    monkeypatch.setattr(scheduler, "player", playback)
    monkeypatch.setattr(scheduler, "CHIME_FILE", "test.mp3")
    db.set_setting("suhoor_mp3", "test.mp3")
    async def run():
        entered = asyncio.Event()
        async def ready(device):
            entered.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(audio, "ensure_ready", ready)
        job = scheduler.play_reminder("fajr") if kind == "reminder" else scheduler.play_suhoor(force=True)
        pending = asyncio.create_task(job)
        await entered.wait()
        await playback.stop()
        await pending
    asyncio.run(run())
    events = db.get_events()
    assert not any(e["type"] == "error" for e in events)
    assert any(e["type"] == "skipped" and "interrupted" in e["detail"] for e in events)


def test_failed_prayer_does_not_finish_or_run_after_hooks(playback, monkeypatch):
    child(monkeypatch, "import sys; print('no speaker', file=sys.stderr); sys.exit(3)")
    monkeypatch.setattr(scheduler, "player", playback)
    db.update_prayer("fajr", {"mp3": "test.mp3", "dua_mp3": "test.mp3"})
    hooks = []
    async def run_hooks(position, prayer):
        hooks.append(position)
    monkeypatch.setattr(scheduler, "run_hooks", run_hooks)
    asyncio.run(scheduler.play_prayer("fajr"))
    assert hooks == ["before"]
    details = [e["detail"] for e in db.get_events()]
    assert not any("Finished fajr" in detail for detail in details)
    assert any("fajr adhan failed" in detail for detail in details)

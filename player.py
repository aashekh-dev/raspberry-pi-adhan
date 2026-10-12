"""Audio playback. Prefers mpv (live volume/pause via its JSON IPC socket, fade
filter); falls back to cvlc (as on the original Pi setup) or afplay (dev Macs).
With non-mpv players, pause/resume works via SIGSTOP/SIGCONT but live volume
changes are unsupported.
"""
import asyncio
import json
import shutil
import signal
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import audio
from config import AUDIO_EXTS, MEDIA_DIR
from events import emit, ws_manager

IPC_SOCKET = Path("/tmp/adhand-mpv.sock")


class PlaybackStopped(RuntimeError):
    """A user stopped playback or another request replaced it."""


@dataclass(frozen=True)
class Playback:
    """A result and interruption flag that outlive the underlying process."""
    task: asyncio.Task
    stopped: asyncio.Event
    label: str


def list_media() -> list[str]:
    return sorted(p.name for p in MEDIA_DIR.iterdir()
                  if p.is_file() and p.suffix.lower() in AUDIO_EXTS)


def resolve_media(name: str) -> Path:
    path = (MEDIA_DIR / name).resolve()
    if path.parent != MEDIA_DIR.resolve() or path.suffix.lower() not in AUDIO_EXTS or not path.is_file():
        raise FileNotFoundError(f"Unknown audio file: {name}")
    return path


def _build_cmd(path: Path, volume: int, device: str | None,
               fade: int = 0, duration: int | None = None) -> tuple[list[str], bool]:
    """Returns (command, has_ipc). volume is 0-100; 50 is unity gain for vlc."""
    backend, output = audio.split_device(device)
    if shutil.which("mpv"):
        cmd = ["mpv", "--no-video", "--input-terminal=no", "--msg-level=all=warn", f"--volume={volume}",
               f"--input-ipc-server={IPC_SOCKET}"]
        if fade > 0:
            cmd.append(f"--af=lavfi=[afade=t=in:d={fade}]")
        if duration:
            cmd.append(f"--length={duration}")
        if device:
            cmd.append(f"--audio-device={backend}/{output}")
        return cmd + [str(path)], True
    if shutil.which("cvlc"):
        cmd = ["cvlc", "--no-dbus", "--play-and-exit", "--gain", f"{volume / 50:.2f}"]
        if duration:
            cmd += ["--stop-time", str(duration)]
        if device:
            if backend == "pipewire":
                raise RuntimeError("Native PipeWire output requires mpv; select a pulse/ output for VLC")
            if backend == "pulse":
                # VLC 3 has no --pulse-audio-device option. libpulse accepts
                # PULSE_SINK per process, leaving the server default untouched.
                cmd = ["env", f"PULSE_SINK={output}", *cmd, "-A", "pulse"]
            else:
                cmd += ["-A", "alsa", "--alsa-audio-device", output]
        return cmd + [str(path)], False
    if shutil.which("afplay"):  # macOS dev fallback; no device routing
        if device:
            raise RuntimeError("Selecting an audio output requires mpv or cvlc")
        cmd = ["afplay", "-v", f"{volume / 100:.2f}"]
        if duration:
            cmd += ["-t", str(duration)]
        return cmd + [str(path)], False
    raise RuntimeError("No supported audio player found (need mpv, cvlc, or afplay)")


class Player:
    def __init__(self):
        self._proc: asyncio.subprocess.Process | None = None
        self._has_ipc = False
        self._playback: Playback | None = None
        self._preparing: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._generation = 0
        self.current: dict | None = None

    @property
    def playing(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def _broadcast(self) -> None:
        await ws_manager.broadcast({"kind": "playing", "playing": self.current})

    async def _ipc(self, command: list) -> None:
        # mpv creates the socket shortly after spawn; retry briefly
        for attempt in range(6):
            try:
                reader, writer = await asyncio.open_unix_connection(str(IPC_SOCKET))
                break
            except OSError:
                if attempt == 5:
                    raise RuntimeError("player IPC socket unavailable")
                await asyncio.sleep(0.3)
        writer.write(json.dumps({"command": command}).encode() + b"\n")
        await writer.drain()
        writer.close()

    async def play(self, mp3: str, volume: int, device: str | None = None, label: str = "test",
                   fade: int = 0, duration: int | None = None) -> Playback:
        path = resolve_media(mp3)
        cmd, has_ipc = _build_cmd(path, volume, device, fade=fade, duration=duration)
        async with self._lock:
            self._generation += 1
            generation = self._generation
            await self._stop_locked()
            preparation = asyncio.create_task(audio.ensure_ready(device))
            self._preparing = preparation
        try:
            await preparation
        except asyncio.CancelledError:
            if generation != self._generation:
                raise PlaybackStopped("Playback interrupted during audio recovery") from None
            raise
        except Exception as exc:
            await emit("error", f"Audio not ready ({label}): {exc}")
            raise
        finally:
            if self._preparing is preparation:
                self._preparing = None
        async with self._lock:
            if generation != self._generation:
                raise PlaybackStopped("Playback interrupted before starting")
            IPC_SOCKET.unlink(missing_ok=True)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
                )
            except OSError as exc:
                raise RuntimeError(f"Cannot start {cmd[0]}: {exc}") from exc
            self._proc, self._has_ipc = proc, has_ipc
            stopped = asyncio.Event()
            self.current = {
                "label": label, "mp3": mp3, "volume": volume, "device": device,
                "paused": False, "live_volume": has_ipc,
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
            playback = Playback(asyncio.create_task(self._reap(proc, label, stopped)), stopped, label)
            self._playback = playback
            await self._broadcast()
        # Catch common immediate startup failures before the API reports playback.
        try:
            error = await asyncio.wait_for(asyncio.shield(playback.task), timeout=0.2)
        except asyncio.TimeoutError:
            pass
        else:
            if error:
                raise error
        if playback.stopped.is_set():
            raise PlaybackStopped(f"Playback interrupted ({label})")
        return playback

    async def _reap(self, proc: asyncio.subprocess.Process, label: str,
                    stopped: asyncio.Event) -> Exception | None:
        # Drain stdout + stderr continuously so a noisy player cannot deadlock;
        # only retain a small diagnostic tail in memory and in the event log.
        tail = b""
        while chunk := await proc.stdout.read(4096):
            tail = (tail + chunk)[-4096:]
        code = await proc.wait()
        if self._proc is proc:
            self.current = None
            self._proc = None
            await self._broadcast()
        if stopped.is_set():
            return PlaybackStopped(f"Playback interrupted ({label})")
        if code != 0:
            detail = tail.decode(errors="replace").strip() or "no diagnostics"
            error = RuntimeError(f"Playback failed ({label}, exit {code}): {detail}")
            await emit("error", str(error))
            return error
        return None

    async def wait_until_done(self, playback: Playback | None = None) -> None:
        # Retain the result after reaping, and allow sequencers to wait on their
        # own playback even when a preview replaces the shared player.
        playback = playback or self._playback
        if playback is not None:
            error = await asyncio.shield(playback.task)
            if playback.stopped.is_set():
                raise PlaybackStopped(f"Playback interrupted ({playback.label})")
            if error:
                raise error

    async def set_pause(self, paused: bool) -> None:
        if not self.playing:
            return
        if self._has_ipc:
            try:
                await self._ipc(["set_property", "pause", paused])
            except (OSError, RuntimeError):
                if self.playing:
                    self._proc.send_signal(signal.SIGSTOP if paused else signal.SIGCONT)
        else:
            self._proc.send_signal(signal.SIGSTOP if paused else signal.SIGCONT)
        if self.current:
            self.current["paused"] = paused
            await self._broadcast()

    async def set_volume(self, volume: int) -> None:
        if not self.playing:
            return
        if not self._has_ipc:
            raise RuntimeError("Live volume needs mpv; current player doesn't support it")
        await self._ipc(["set_property", "volume", volume])
        if self.current:
            self.current["volume"] = volume
            await self._broadcast()

    async def stop(self) -> None:
        async with self._lock:
            self._generation += 1
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        # The process may have exited while its completion notification or
        # prayer sequence is still pending. Invalidate that result as well.
        playback = self._playback
        if playback and not playback.stopped.is_set():
            playback.stopped.set()
            if not self.playing and not playback.task.done():
                await emit("stopped", f"Playback stopped ({playback.label})")
        if self._preparing:
            preparation = self._preparing
            self._preparing = None
            preparation.cancel()
            with suppress(asyncio.CancelledError):
                await preparation
        if self.playing:
            proc = self._proc
            label = self.current["label"] if self.current else "?"
            with suppress(ProcessLookupError):
                if self.current and self.current.get("paused"):
                    proc.send_signal(signal.SIGCONT)
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                with suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
            await playback.task
            await emit("stopped", f"Playback stopped ({label})")
        self.current = None
        self._proc = None


player = Player()

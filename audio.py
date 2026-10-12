"""Audio routing and readiness. Bluetooth pairing lives in bluetooth.py."""
import asyncio
import json
import re
import shutil

import bluetooth


def split_device(device: str | None) -> tuple[str | None, str | None]:
    """Explicit server/device IDs, with compatibility for stored ALSA names."""
    if not device:
        return None, None
    for backend in ("pulse", "pipewire", "alsa"):
        if device.startswith(backend + "/"):
            name = device[len(backend) + 1:]
            if not name:
                raise ValueError(f"Missing {backend} output name")
            return backend, name
    return "alsa", device


async def _output(*command: str) -> str | None:
    if not shutil.which(command[0]):
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
    except OSError:
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        proc.kill()
        await proc.communicate()
        if isinstance(exc, asyncio.CancelledError):
            raise
        return None
    return out.decode(errors="replace") if proc.returncode == 0 else None


def _json_list(output: str | None) -> list | None:
    try:
        items = json.loads(output) if output is not None else None
        return items if isinstance(items, list) else None
    except ValueError:
        return None


def bluetooth_mac(device: str | None) -> str | None:
    """Extract a speaker address from BlueZ sink names, including legacy ones."""
    match = re.search(r"bluez_(?:output|sink)[./]([0-9a-f]{2}(?:[_:][0-9a-f]{2}){5})(?:[./]|$)",
                      device or "", re.IGNORECASE)
    if not match:
        match = re.search(r"(?:^|,)DEV=([0-9a-f]{2}(?::[0-9a-f]{2}){5})(?:,|$)",
                          device or "", re.IGNORECASE)
    return match.group(1).replace("_", ":").upper() if match else None


async def _pulse_sinks() -> list[dict] | None:
    # PipeWire's Pulse server exposes Bluetooth sinks through the same API.
    items = _json_list(await _output("pactl", "--format=json", "list", "sinks"))
    if items is not None:
        return [{"id": f"pulse/{item['name']}",
                 "label": f"{item.get('description') or item['name']} (PulseAudio / PipeWire)"}
                for item in items if isinstance(item, dict) and item.get("name")]
    output = await _output("pactl", "list", "short", "sinks")
    if output is None:
        return None
    return [{"id": f"pulse/{fields[1]}", "label": f"{fields[1]} (PulseAudio / PipeWire)"}
            for line in output.splitlines() if len(fields := line.split()) >= 2]


async def _pipewire_sinks() -> list[dict] | None:
    items = _json_list(await _output("pw-dump"))
    if items is None:
        return None
    devices = []
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "PipeWire:Interface:Node":
            continue
        props = (item.get("info") or {}).get("props") or {}
        if props.get("media.class") == "Audio/Sink" and props.get("node.name"):
            devices.append({"id": f"pipewire/{props['node.name']}",
                            "label": f"{props.get('node.description') or props['node.name']} (PipeWire)"})
    return devices


async def _alsa_devices() -> list[dict]:
    output = await _output("aplay", "-L")
    lines = (output or "").splitlines()
    devices = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line and not line[0].isspace():
            dev_id = line.strip()
            desc = lines[i + 1].strip() if i + 1 < len(lines) and lines[i + 1][:1].isspace() else dev_id
            # keep the useful ones; raw ALSA output is very noisy
            if dev_id.split(":")[0] in ("default", "sysdefault", "plughw", "bluealsa"):
                devices.append({"id": dev_id, "label": f"{dev_id} — {desc}"})
        i += 1
    return devices


async def list_devices() -> list[dict]:
    """Selectable server sinks and legacy ALSA outputs, plus the default."""
    sinks = await _pulse_sinks()
    if sinks is None:
        sinks = await _pipewire_sinks()
    return [{"id": None, "label": "System default"}] + (sinks or []) + await _alsa_devices()


async def ensure_ready(device: str | None) -> None:
    """Check the selected server output; reconnect a saved Bluetooth speaker.

    A suspended sink is usable (the server wakes it on playback). Missing
    sinks are retried with backoff, never replaced by a different speaker.
    Raw ALSA outputs are opened and checked by the player itself.
    """
    backend, _ = split_device(device)
    if backend == "alsa":
        return
    if backend is None and not (shutil.which("pactl") or shutil.which("pw-dump")):
        return  # macOS and legacy ALSA-only installations

    async def ready() -> bool:
        if backend == "pulse":
            sinks = await _pulse_sinks()
        elif backend == "pipewire":
            sinks = await _pipewire_sinks()
        else:
            sinks = await _pulse_sinks()
            if sinks is None:
                sinks = await _pipewire_sinks()
            if sinks is None:
                return True  # no server: let the default player open ALSA/CoreAudio
        return bool(sinks) if device is None else any(
            sink["id"] == device for sink in (sinks or []))

    if await ready():
        return
    mac = bluetooth_mac(device)
    if mac and bluetooth.available():
        await bluetooth.reconnect(mac)
    # BlueZ connection completion precedes A2DP node creation by WirePlumber.
    for delay in (1, 2, 4):
        await asyncio.sleep(delay)
        if await ready():
            return
    raise RuntimeError(f"Audio output {device or 'system default'} unavailable; "
                       "check the user's PipeWire/PulseAudio session and speaker connection")

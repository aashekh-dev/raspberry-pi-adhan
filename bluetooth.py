"""Bluetooth speaker management via bluetoothctl. Absent (e.g. on dev Macs) -> available=False."""
import asyncio
import re
import shutil

_DEVICE_RE = re.compile(r"^Device ([0-9A-F:]{17}) (.+)$", re.MULTILINE | re.IGNORECASE)
_MAC_RE = re.compile(r"^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$", re.IGNORECASE)
_FAILURE_RE = re.compile(r"^(?:Failed\b|Invalid command\b|No default controller\b|Device .* not available)",
                         re.MULTILINE | re.IGNORECASE)


def available() -> bool:
    return shutil.which("bluetoothctl") is not None


async def _run(*args: str, timeout: int = 20) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "bluetoothctl", *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        raise RuntimeError(f"Cannot run bluetoothctl: {exc}") from exc
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        proc.kill()
        await proc.communicate()
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise RuntimeError(f"bluetoothctl {' '.join(args)} timed out")
    output = re.sub(r"\x1b\[[0-9;]*m", "", out.decode(errors="replace"))
    if proc.returncode != 0 or _FAILURE_RE.search(output):
        raise RuntimeError(f"bluetoothctl {' '.join(args)} failed (exit {proc.returncode}): {output.strip()[-1000:]}")
    return output


def _parse_devices(output: str) -> list[dict]:
    return [{"mac": m.group(1), "name": m.group(2).strip()} for m in _DEVICE_RE.finditer(output)]


async def _connected_macs() -> set[str]:
    try:
        out = await _run("devices", "Connected", timeout=10)
        return {d["mac"] for d in _parse_devices(out)}
    except RuntimeError:
        return set()


async def status() -> dict:
    if not available():
        return {"available": False, "devices": []}
    # "devices Paired" on bluez >= 5.65, "paired-devices" before
    try:
        out = await _run("devices", "Paired", timeout=10)
    except RuntimeError:
        try:
            out = await _run("paired-devices", timeout=10)
        except RuntimeError as exc:
            return {"available": True, "devices": [], "error": str(exc)}
    devices = _parse_devices(out)
    connected = await _connected_macs()
    for d in devices:
        d["connected"] = d["mac"] in connected
    return {"available": True, "devices": devices}


async def scan(seconds: int = 8) -> list[dict]:
    """Scan for nearby devices; returns unpaired discoveries."""
    await _run("power", "on", timeout=10)
    await _run("--timeout", str(seconds), "scan", "on", timeout=seconds + 10)
    found = _parse_devices(await _run("devices", timeout=10))
    paired = {d["mac"] for d in (await status())["devices"]}
    return [d for d in found if d["mac"] not in paired]


def _check_mac(mac: str) -> str:
    if not _MAC_RE.match(mac):
        raise ValueError(f"Invalid MAC address: {mac}")
    return mac


async def pair(mac: str) -> str:
    mac = _check_mac(mac)
    out = await _run("pair", mac, timeout=30)
    await _run("trust", mac, timeout=10)
    out += await _run("connect", mac, timeout=20)
    return out


async def connect(mac: str) -> str:
    return await _run("connect", _check_mac(mac), timeout=20)


async def reconnect(mac: str) -> str:
    """Reconnect an already paired speaker; do not pair or change routing."""
    mac = _check_mac(mac)
    for attempt in range(3):
        try:
            return await connect(mac)
        except RuntimeError as exc:
            if attempt == 2:
                raise RuntimeError(f"Bluetooth {mac} unavailable after 3 attempts: {exc}") from exc
            await asyncio.sleep(2 ** attempt)


async def disconnect(mac: str) -> str:
    return await _run("disconnect", _check_mac(mac), timeout=20)


async def forget(mac: str) -> str:
    return await _run("remove", _check_mac(mac), timeout=10)

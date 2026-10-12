import asyncio
from types import SimpleNamespace

import pytest

import bluetooth


@pytest.mark.parametrize("code,output", [(1, "Connection refused"), (0, "Failed to connect: org.bluez.Error.NotReady")])
def test_bluetooth_command_failure_is_reported(monkeypatch, code, output):
    async def spawn(*args, **kwargs):
        async def communicate():
            return output.encode(), None
        return SimpleNamespace(returncode=code, communicate=communicate)
    monkeypatch.setattr(bluetooth.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(RuntimeError, match=output):
        asyncio.run(bluetooth.connect("AA:BB:CC:DD:EE:FF"))


def test_reconnect_has_finite_exponential_backoff(monkeypatch):
    attempts, delays = [], []
    async def connect(mac):
        attempts.append(mac)
        if len(attempts) < 3:
            raise RuntimeError("speaker off")
        return "Connection successful"
    async def sleep(delay):
        delays.append(delay)
    monkeypatch.setattr(bluetooth, "connect", connect)
    monkeypatch.setattr(bluetooth.asyncio, "sleep", sleep)
    assert asyncio.run(bluetooth.reconnect("AA:BB:CC:DD:EE:FF")) == "Connection successful"
    assert attempts == ["AA:BB:CC:DD:EE:FF"] * 3
    assert delays == [1, 2]


def test_reconnect_reports_exhaustion(monkeypatch):
    async def connect(mac):
        raise RuntimeError("speaker off")
    async def sleep(delay):
        pass
    monkeypatch.setattr(bluetooth, "connect", connect)
    monkeypatch.setattr(bluetooth.asyncio, "sleep", sleep)
    with pytest.raises(RuntimeError, match="3 attempts.*speaker off"):
        asyncio.run(bluetooth.reconnect("AA:BB:CC:DD:EE:FF"))


def test_no_adapter_is_reported_without_breaking_status(monkeypatch):
    monkeypatch.setattr(bluetooth, "available", lambda: True)
    async def run(*args, **kwargs):
        raise RuntimeError("No default controller available")
    monkeypatch.setattr(bluetooth, "_run", run)
    status = asyncio.run(bluetooth.status())
    assert status["devices"] == []
    assert "No default controller" in status["error"]


def test_older_bluez_paired_command_fallback(monkeypatch):
    monkeypatch.setattr(bluetooth, "available", lambda: True)
    async def run(*args, **kwargs):
        if args == ("devices", "Paired"):
            raise RuntimeError("Invalid command")
        return "Device AA:BB:CC:DD:EE:FF JBL\n"
    monkeypatch.setattr(bluetooth, "_run", run)
    status = asyncio.run(bluetooth.status())
    assert status["devices"] == [{"mac": "AA:BB:CC:DD:EE:FF", "name": "JBL", "connected": True}]


def test_empty_modern_paired_list_does_not_use_deprecated_command(monkeypatch):
    monkeypatch.setattr(bluetooth, "available", lambda: True)
    async def run(*args, **kwargs):
        if args == ("paired-devices",):
            raise RuntimeError("Invalid command paired-devices")
        return ""
    monkeypatch.setattr(bluetooth, "_run", run)
    assert asyncio.run(bluetooth.status()) == {"available": True, "devices": []}


def test_scan_powers_on_adapter_and_excludes_paired_devices(monkeypatch):
    monkeypatch.setattr(bluetooth, "available", lambda: True)
    powered = False

    async def spawn(*args, **kwargs):
        nonlocal powered
        command = args[1:]
        output = ""
        if command == ("power", "on"):
            powered = True
            output = "Changing power on succeeded"
        elif command == ("--timeout", "8", "scan", "on"):
            if not powered:
                output = "Failed to start discovery: org.bluez.Error.NotReady"
        elif command == ("devices",):
            output = "Device AA:BB:CC:DD:EE:FF Saved speaker\nDevice 11:22:33:44:55:66 New speaker\n"
        elif command == ("devices", "Paired"):
            output = "Device AA:BB:CC:DD:EE:FF Saved speaker\n"
        elif command != ("devices", "Connected"):
            raise AssertionError(f"Unexpected command: {command}")

        async def communicate():
            return output.encode(), None
        return SimpleNamespace(returncode=0, communicate=communicate)

    monkeypatch.setattr(bluetooth.asyncio, "create_subprocess_exec", spawn)
    assert asyncio.run(bluetooth.scan()) == [{"mac": "11:22:33:44:55:66", "name": "New speaker"}]

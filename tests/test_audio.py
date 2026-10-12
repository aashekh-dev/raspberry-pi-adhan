"""Audio server discovery without depending on the runner's audio hardware."""
import asyncio
import json
from types import SimpleNamespace

import pytest

import audio
import bluetooth


def fake_tools(monkeypatch, responses):
    """Fake only the OS command boundary; exercise the real discovery code."""
    monkeypatch.setattr(audio.shutil, "which", lambda name: name if any(
        command[0] == name for command in responses) else None)

    async def spawn(*command, **kwargs):
        code, output = responses.get(command, (1, ""))

        async def communicate():
            return output.encode(), b""

        return SimpleNamespace(returncode=code, communicate=communicate)

    monkeypatch.setattr(audio.asyncio, "create_subprocess_exec", spawn)


def test_pulse_sinks_include_bluetooth_without_alsa(monkeypatch):
    fake_tools(monkeypatch, {
        ("pactl", "--format=json", "list", "sinks"): (0, json.dumps([
            {"name": "bluez_output.AA_BB_CC_DD_EE_FF.1", "description": "JBL Flip",
             "properties": {"api.bluez5.address": "AA:BB:CC:DD:EE:FF"}},
            {"name": "alsa_output.usb.analog-stereo", "description": "USB speaker"},
        ])),
    })
    devices = asyncio.run(audio.list_devices())
    assert [d["id"] for d in devices] == [None,
        "pulse/bluez_output.AA_BB_CC_DD_EE_FF.1", "pulse/alsa_output.usb.analog-stereo"]
    assert "JBL Flip" in devices[1]["label"]


def test_old_pactl_short_output(monkeypatch):
    fake_tools(monkeypatch, {
        ("pactl", "--format=json", "list", "sinks"): (1, ""),
        ("pactl", "list", "short", "sinks"): (0,
            "47\tbluez_sink.AA_BB_CC_DD_EE_FF.a2dp_sink\tmodule-bluez5-device.c\ts16le\tSUSPENDED\n"),
    })
    devices = asyncio.run(audio.list_devices())
    assert devices[1]["id"] == "pulse/bluez_sink.AA_BB_CC_DD_EE_FF.a2dp_sink"


def test_native_pipewire_fallback_uses_stable_sink_names(monkeypatch):
    fake_tools(monkeypatch, {
        ("pactl", "--format=json", "list", "sinks"): (1, ""),
        ("pw-dump",): (0, json.dumps([
            {"id": 52, "type": "PipeWire:Interface:Node", "info": {"props": {
                "media.class": "Audio/Sink", "node.name": "bluez_output.AA_BB_CC_DD_EE_FF.1",
                "node.description": "JBL speaker"}}},
            {"id": 53, "type": "PipeWire:Interface:Node", "info": {"props": {
                "media.class": "Audio/Source", "node.name": "microphone"}}},
        ])),
    })
    devices = asyncio.run(audio.list_devices())
    assert [d["id"] for d in devices] == [None, "pipewire/bluez_output.AA_BB_CC_DD_EE_FF.1"]


def test_malformed_server_output_keeps_legacy_alsa(monkeypatch):
    fake_tools(monkeypatch, {
        ("pactl", "--format=json", "list", "sinks"): (0, "invalid json"),
        ("aplay", "-L"): (0, "default\n    Default output\nplughw:CARD=USB,DEV=0\n    USB speaker\n"),
    })
    devices = asyncio.run(audio.list_devices())
    assert [d["id"] for d in devices] == [None, "default", "plughw:CARD=USB,DEV=0"]


def test_no_audio_tools_returns_system_default(monkeypatch):
    fake_tools(monkeypatch, {})
    assert asyncio.run(audio.list_devices()) == [{"id": None, "label": "System default"}]


def test_ready_bluetooth_sink_does_not_reconnect(monkeypatch):
    sink = "pulse/bluez_output.AA_BB_CC_DD_EE_FF.1"
    fake_tools(monkeypatch, {("pactl", "--format=json", "list", "sinks"): (0,
        '[{"name": "bluez_output.AA_BB_CC_DD_EE_FF.1"}]')})
    async def reconnect(mac):
        pytest.fail("a ready audio sink should not reconnect")
    monkeypatch.setattr(bluetooth, "reconnect", reconnect)
    asyncio.run(audio.ensure_ready(sink))


def test_missing_bluetooth_sink_reconnects_and_waits_for_profile(monkeypatch):
    sink = "pulse/bluez_output.AA_BB_CC_DD_EE_FF.1"
    connected, checks = [], []
    async def sinks():
        checks.append(True)
        return [{"id": sink}] if len(checks) >= 3 else []
    async def reconnect(mac):
        connected.append(mac)
    async def sleep(delay):
        pass
    monkeypatch.setattr(audio, "_pulse_sinks", sinks, raising=False)
    monkeypatch.setattr(bluetooth, "available", lambda: True)
    monkeypatch.setattr(bluetooth, "reconnect", reconnect, raising=False)
    monkeypatch.setattr(audio.asyncio, "sleep", sleep)
    asyncio.run(audio.ensure_ready(sink))
    assert connected == ["AA:BB:CC:DD:EE:FF"]
    assert len(checks) == 3


def test_missing_sink_fails_instead_of_playing_elsewhere(monkeypatch):
    fake_tools(monkeypatch, {("pactl", "--format=json", "list", "sinks"): (0, "[]")})
    async def sleep(delay):
        pass
    monkeypatch.setattr(audio.asyncio, "sleep", sleep)
    with pytest.raises(RuntimeError, match="pulse/missing.*unavailable"):
        asyncio.run(audio.ensure_ready("pulse/missing"))


def test_default_checks_server_has_an_output(monkeypatch):
    fake_tools(monkeypatch, {("pactl", "--format=json", "list", "sinks"): (0, "[]")})
    async def sleep(delay):
        pass
    monkeypatch.setattr(audio.asyncio, "sleep", sleep)
    with pytest.raises(RuntimeError, match="default.*unavailable"):
        asyncio.run(audio.ensure_ready(None))


@pytest.mark.parametrize("device", [None, "plughw:1,0", "alsa/plughw:1,0"])
def test_legacy_and_dev_default_do_not_require_an_audio_server(monkeypatch, device):
    fake_tools(monkeypatch, {})
    asyncio.run(audio.ensure_ready(device))


def test_default_allows_alsa_fallback_when_server_tools_are_installed_but_offline(monkeypatch):
    fake_tools(monkeypatch, {("pactl", "--format=json", "list", "sinks"): (1, "")})
    asyncio.run(audio.ensure_ready(None))

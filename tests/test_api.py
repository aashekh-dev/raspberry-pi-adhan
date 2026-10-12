"""FastAPI endpoint tests via TestClient, backed by the isolated temp DB.

The ``client`` fixture (see conftest.py) repoints DB_PATH before the app's
lifespan runs, so these tests hit a fresh temp database and start/stop the
real scheduler through the TestClient context manager.
"""

import pytest


@pytest.mark.parametrize("error", [
    "Failed to set power on: org.bluez.Error.Failed",
    "Failed to start discovery: org.bluez.Error.NotReady",
    "bluetoothctl --timeout 8 scan on timed out",
])
def test_bluetooth_scan_failure_returns_useful_error(client, monkeypatch, error):
    import bluetooth

    monkeypatch.setattr(bluetooth, "available", lambda: True)

    async def scan():
        raise RuntimeError(error)

    monkeypatch.setattr(bluetooth, "scan", scan)
    response = client.post("/api/bluetooth/scan")
    assert response.status_code == 400
    assert error in response.json()["detail"]


def test_status_ok_and_keys(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    for key in ("now", "times", "hijri", "next", "scheduled", "ramadan_active"):
        assert key in body
    # No location seeded -> times is null.
    assert body["times"] is None


def test_get_settings_ok(client):
    r = client.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["method"] == "ISNA"  # DEFAULT_METHOD
    assert "ISNA" in body["methods"]
    assert "Hanafi" in body["asr_methods"]


def test_put_settings_valid(client):
    payload = {"lat": 47.6, "lng": -122.3, "method": "ISNA",
               "asr_method": "Hanafi", "high_lats": "NightMiddle"}
    r = client.put("/api/settings", json=payload)
    assert r.status_code == 200
    assert r.json()["lat"] == 47.6
    # Now that location is set, status should surface computed prayer times.
    status = client.get("/api/status").json()
    assert status["times"] is not None
    assert set(status["times"]) == {"fajr", "dhuhr", "asr", "maghrib", "isha"}


def test_put_settings_invalid_method(client):
    payload = {"lat": 47.6, "lng": -122.3, "method": "BOGUS"}
    r = client.put("/api/settings", json=payload)
    assert r.status_code == 400


def test_put_settings_invalid_asr(client):
    payload = {"lat": 47.6, "lng": -122.3, "method": "ISNA", "asr_method": "Nope"}
    r = client.put("/api/settings", json=payload)
    assert r.status_code == 400


def test_get_prayers_returns_five(client):
    r = client.get("/api/prayers")
    assert r.status_code == 200
    prayers = r.json()
    assert len(prayers) == 5
    assert {p["name"] for p in prayers} == {"fajr", "dhuhr", "asr", "maghrib", "isha"}


def test_put_prayer_valid(client):
    r = client.put("/api/prayers/dhuhr", json={"volume": 60, "reminder_minutes": 5})
    assert r.status_code == 200
    assert r.json()["volume"] == 60


def test_put_prayer_unknown_name(client):
    r = client.put("/api/prayers/nope", json={"volume": 60})
    assert r.status_code == 404


def test_put_prayer_unknown_mp3(client):
    r = client.put("/api/prayers/dhuhr", json={"mp3": "not-a-real-file.mp3"})
    assert r.status_code == 400


def test_get_media_list(client):
    r = client.get("/api/media")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_get_preferences(client):
    r = client.get("/api/preferences")
    assert r.status_code == 200
    body = r.json()
    # Defaults from config.PREFERENCE_DEFAULTS.
    assert body["ramadan_mode"] == "auto"
    assert body["jumuah_action"] == "normal"
    assert "suhoor_enabled" in body


def test_put_preferences_valid(client):
    r = client.put("/api/preferences", json={"ramadan_mode": "on", "suhoor_minutes": 30})
    assert r.status_code == 200
    assert r.json()["ramadan_mode"] == "on"
    assert r.json()["suhoor_minutes"] == 30


def test_put_preferences_unknown_key(client):
    r = client.put("/api/preferences", json={"bogus_key": 1})
    assert r.status_code == 400


def test_put_preferences_invalid_ramadan_mode(client):
    r = client.put("/api/preferences", json={"ramadan_mode": "sometimes"})
    assert r.status_code == 400


def test_hooks_crud_cycle(client):
    # create
    payload = {
        "name": "Kahf",
        "position": "after",
        "prayers": ["dhuhr"],
        "days": [4],
        "script": "10-friday-kahf.sh",
        "offset_minutes": -45,
    }
    r = client.post("/api/hooks", json=payload)
    assert r.status_code == 200
    hook_id = r.json()["id"]

    # read (list)
    hooks = client.get("/api/hooks").json()
    assert any(h["id"] == hook_id and h["name"] == "Kahf" for h in hooks)

    # delete
    r = client.delete(f"/api/hooks/{hook_id}")
    assert r.status_code == 200
    hooks = client.get("/api/hooks").json()
    assert all(h["id"] != hook_id for h in hooks)


def test_create_hook_invalid_prayer(client):
    payload = {"name": "Bad", "position": "after", "prayers": ["nope"],
               "days": [4], "script": "10-friday-kahf.sh"}
    r = client.post("/api/hooks", json=payload)
    assert r.status_code == 400


def test_create_hook_unknown_script(client):
    payload = {"name": "Bad", "position": "after", "prayers": ["dhuhr"],
               "days": [4], "script": "no-such-script.sh"}
    r = client.post("/api/hooks", json=payload)
    assert r.status_code == 400


def test_events_endpoint(client):
    # The lifespan/scheduler emits schedule events on startup; endpoint returns a list.
    r = client.get("/api/events")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_playback_readiness_error_is_actionable(client, monkeypatch):
    import routes
    async def play(*args, **kwargs):
        raise RuntimeError("Audio output pulse/jbl unavailable")
    monkeypatch.setattr(routes.player, "play", play)
    response = client.post("/api/test", json={"mp3": "test.mp3", "device": "pulse/jbl"})
    assert response.status_code == 503
    assert "pulse/jbl unavailable" in response.json()["detail"]


def test_saved_output_can_be_reset_to_system_default(client):
    selected = client.put("/api/prayers/fajr", json={"device": "pulse/bluez_output.AA_BB_CC_DD_EE_FF.1"})
    assert selected.json()["device"] == "pulse/bluez_output.AA_BB_CC_DD_EE_FF.1"
    cleared = client.put("/api/prayers/fajr", json={"device": None})
    assert cleared.status_code == 200
    assert cleared.json()["device"] is None


def test_other_prayer_edits_preserve_saved_output(client):
    client.put("/api/prayers/fajr", json={"device": "pulse/jbl"})
    updated = client.put("/api/prayers/fajr", json={"volume": 60})
    assert updated.json()["device"] == "pulse/jbl"


def test_stopped_test_playback_is_not_a_server_failure(client, monkeypatch):
    import routes
    from player import PlaybackStopped
    async def play(*args, **kwargs):
        raise PlaybackStopped("Playback interrupted during audio recovery")
    monkeypatch.setattr(routes.player, "play", play)
    response = client.post("/api/test", json={"mp3": "test.mp3"})
    assert response.status_code == 200
    assert response.json() == {"playing": None, "interrupted": True}

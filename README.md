# Raspberry Pi Adhan Clock

Turns a Raspberry Pi into a self-hosted adhan clock that your whole family can
control from their phones. A single daemon (`adhand`) calculates prayer times
locally every day, plays the adhan at each prayer time, and serves a
phone-friendly web app on your home Wi-Fi — no cloud, no accounts, no ports
opened to the internet.

## Features

- **Web app for the whole family** — open `http://<pi-hostname>.local:8000` on
  any phone on the home network and "Add to Home Screen" for an app-like icon.
- **Automatic prayer times** — calculated on-device with the
  [praytimes.org](http://praytimes.org) engine; recomputed nightly and whenever
  settings change. Pick your calculation method, asr school, and high-latitude
  rule.
- **Per-prayer control** — enable/disable, volume, adhan audio, output device,
  a minute offset, an optional pre-adhan reminder chime, and a dua to play
  after the adhan.
- **Test anything** — preview any file, or simulate a reminder, the suhoor
  alarm, or a full adhan sequence, all with a chosen volume.
- **Live playback controls** — pause/resume, stop, and adjust volume of
  whatever is currently playing, right from the app.
- **Hooks** — run your own scripts before or after any prayer, filtered by day
  of week, with an optional schedule offset (e.g. Surah Al-Kahf 45 minutes
  before Friday dhuhr) and per-hook volume.
- **Ramadan mode** — Hijri date display, suhoor alarm, iftar countdown, and a
  Friday-dhuhr (jumu'ah) mode. Auto-detected from the Hijri calendar.
- **Bluetooth speakers** — scan, pair, and connect speakers from the app.
- **Health & self-update** — see CPU temp, uptime, disk, and version; pull the
  latest version and restart from a button in the app (no SSH needed).

## Hardware

1. A Raspberry Pi running Raspberry Pi OS (avoid the Pi Zero — no built-in audio
   out).
2. Speakers — **powered** speakers via the 3.5 mm jack, an HDMI display/speaker,
   a USB audio device, or a Bluetooth speaker. (The Pi's headphone jack is
   line-level and can't drive passive/unpowered speakers.)

## Install

Use Raspberry Pi OS Bookworm or newer. Clone this repo into your normal
user's home directory and run the installer as that user:

```bash
cd ~
git clone <this-repo-url> raspberry-pi-adhan
cd raspberry-pi-adhan
./deploy/install.sh
```

The installer:

- installs `mpv`, VLC, ALSA tools, BlueZ, PipeWire, WirePlumber,
  `libspa-0.2-bluetooth` (A2DP support), and `pulseaudio-utils` (`pactl`),
- enables the user's audio services at boot with lingering, and configures
  WirePlumber for Bluetooth without an active desktop login (0.4 and 0.5+),
- creates a Python virtualenv and installs dependencies,
- fetches a couple of dua recitations,
- installs and starts the `adhand` systemd service (auto-starts on boot),
- removes any leftover cron jobs from the older version of this project.

When it finishes, open `http://<pi-hostname>.local:8000` from any phone on your
Wi-Fi, set your location (the 📍 button uses the phone's GPS), and you're done.

The service uses the installing account's actual home, UID, runtime directory,
PulseAudio socket, and session bus. Running the installer with `sudo` selects
`SUDO_USER`; a root login must specify `ADHAND_USER=<your-user>`. The repository
must be writable by that account. `deploy/adhand.service` is a template rendered
by the installer, so do not copy it to systemd unchanged. Installing switches
that account's audio session to PipeWire and disables/masks its old PulseAudio
service to avoid competing audio servers.

### Bluetooth audio

Pairing and routing are separate steps:

1. Turn on the speaker and use **Bluetooth speakers** to scan, pair, and connect.
2. Click **Refresh outputs** if the audio profile takes a moment to appear.
3. Select the speaker in **Test adhan** and play a test. Server outputs are
   labeled **PulseAudio / PipeWire** or **PipeWire**; Bluetooth does not need
   to be forced through ALSA.
4. Set **Output** in each prayer's ⚙ options. Selecting **System default**
   clears the saved output. Pairing or connecting does not change these choices.

Outputs use stable names such as `pulse/bluez_output.AA_BB_CC_DD_EE_FF.1`.
Native `pipewire/<node.name>` outputs are supported with mpv; VLC supports
`pulse/<sink>` and ALSA. Existing raw ALSA names still work. Discovery prefers
`pactl` (including older versions without JSON); `pw-dump` supplies native
PipeWire outputs when the Pulse server is unavailable. Offline saved outputs
stay visible, and playback fails explicitly instead of selecting another
speaker.

Before playback, adhand checks server output readiness. A missing selected
Bluetooth sink triggers up to three reconnect attempts (1s, then 2s backoff;
each connection attempt times out after 20s), followed by checks after 1s, 2s,
and 4s for WirePlumber to create the sink. Each discovery command has a 5s
timeout. **Stop** cancels pending recovery. Player failures include the exit
code and a bounded diagnostic tail in the app's event log; failed or stopped
prayers do not continue with the dua or after-hooks.

For an existing Pi, rerun `./deploy/install.sh` to install dependencies and
regenerate the service. To repair only user audio configuration after the
packages are installed, run `bash deploy/setup-audio.sh` as the service user.
Check the audio session as that same user:

```bash
systemctl --user status pipewire pipewire-pulse wireplumber
pactl info
pactl list short sinks
bluetoothctl devices Connected
journalctl --user -u wireplumber -u pipewire-pulse -n 50
journalctl -u adhand -n 50
```

For native PipeWire, `wpctl status` is also useful for diagnosis. A connected
Bluetooth device without a sink points to the audio profile/session setup;
a listed sink with failed playback appears in adhand's event log. The
headless configuration follows the upstream [Bluetooth configuration](https://pipewire.pages.freedesktop.org/wireplumber/daemon/configuration/bluetooth.html)
and [WirePlumber 0.4 migration guide](https://pipewire.pages.freedesktop.org/wireplumber/daemon/configuration/migration.html).

## Usage

- **Set location & method** in the *Location & method* section, then Save.
- **Pick a speaker and volumes** per prayer in the *Prayers* section.
- **Add hooks** in the *Hooks* section — choose an installed script, the
  prayers and days it runs on, an offset, and a volume. Scripts live in
  `before-hooks.d/` and `after-hooks.d/`; only files placed there can be
  selected (the app can never run arbitrary commands).
- **Update** from the *System* section — it pulls this repo and restarts.

Logs: `journalctl -u adhand -f`

## Development

Runs on macOS/Linux for development (falls back to `mpv`/`afplay`, no ALSA
needed):

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

Then open `http://127.0.0.1:8000`. See [CLAUDE.md](CLAUDE.md) for architecture
notes and design decisions.

Run verification with `pip install -r requirements-dev.txt && pytest -q`.
The suite uses fake audio-server commands and real short-lived child processes
to verify discovery, routing, retries, errors, and installation without Bluetooth
hardware. Actual sound, speaker availability, and reboot behavior still need a Pi.

## Credits

- Prayer time calculation: <http://praytimes.org/code/>
- Original adhan-clock concept:
  <http://randomconsultant.blogspot.co.uk/2013/07/turn-your-raspberry-pi-into-azaanprayer.html>
- Dua recitations: [hisnmuslim.com](https://www.hisnmuslim.com) (Hisn al-Muslim)

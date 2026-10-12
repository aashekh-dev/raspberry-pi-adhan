#!/usr/bin/env bash
# Install adhand on a Raspberry Pi. Run from the repo root: ./deploy/install.sh
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_DIR="$(pwd)"
# shellcheck source=deploy/setup-audio.sh
source "$REPO_DIR/deploy/setup-audio.sh"
init_install_user

echo "==> Installing system packages"
as_root apt-get update
as_root apt-get install -y vlc-bin vlc-plugin-base mpv alsa-utils bluez python3-venv \
    pipewire pipewire-bin pipewire-pulse pipewire-alsa wireplumber \
    libspa-0.2-bluetooth pulseaudio-utils dbus-user-session

configure_audio

echo "==> Creating virtualenv"
as_owner python3 -m venv .venv
as_owner .venv/bin/pip install --upgrade pip
as_owner .venv/bin/pip install -r requirements.txt

echo "==> Migrating legacy .settings (if present)"
as_owner .venv/bin/python scripts/migrate_settings.py || true

echo "==> Fetching dua recitations from hisnmuslim.com"
as_owner .venv/bin/python scripts/fetch_duas.py || true

echo "==> Installing systemd service"
python3 deploy/render_service.py "$INSTALL_USER" "$INSTALL_HOME" "$INSTALL_UID" "$REPO_DIR" \
    | as_root tee /etc/systemd/system/adhand.service > /dev/null
as_root systemctl daemon-reload
as_root systemctl enable adhand
as_root systemctl restart adhand

echo "==> Removing legacy cron jobs (rpiAdhanClockJob)"
as_owner crontab -l 2>/dev/null | grep -v rpiAdhanClockJob | as_owner crontab - || true

echo
echo "Done. Open http://$(hostname).local:8000 from any phone on your Wi-Fi."
echo "Logs: journalctl -u adhand -f"

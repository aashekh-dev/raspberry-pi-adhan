#!/usr/bin/env bash
# Can be sourced by install.sh, or run alone to repair headless audio setup.
set -euo pipefail

ADHAND_DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

init_install_user() {
    INSTALL_USER="${ADHAND_USER:-${SUDO_USER:-$(id -un)}}"
    local account
    account="$(getent passwd "$INSTALL_USER")" || {
        echo "Unknown installation user: $INSTALL_USER" >&2
        return 1
    }
    IFS=: read -r INSTALL_USER _ INSTALL_UID INSTALL_GID _ INSTALL_HOME _ <<< "$account"
    if [[ "$INSTALL_UID" == 0 ]]; then
        echo "Run as your normal Pi account, or set ADHAND_USER when installing as root." >&2
        return 1
    fi
    INSTALL_RUNTIME="/run/user/$INSTALL_UID"
}

as_root() {
    if [[ "$(id -u)" == 0 ]]; then "$@"; else sudo -- "$@"; fi
}

as_owner() {
    if [[ "$(id -u)" == "$INSTALL_UID" ]]; then
        env "HOME=$INSTALL_HOME" "$@"
    else
        sudo -u "$INSTALL_USER" -- env "HOME=$INSTALL_HOME" "$@"
    fi
}

user_audio_command() {
    as_owner env "XDG_RUNTIME_DIR=$INSTALL_RUNTIME" \
        "DBUS_SESSION_BUS_ADDRESS=unix:path=$INSTALL_RUNTIME/bus" \
        "PULSE_SERVER=unix:$INSTALL_RUNTIME/pulse/native" "$@"
}

configure_audio() {
    local version major minor config_dir fragment obsolete
    version="$(wireplumber --version)"
    if [[ "$version" =~ ([0-9]+)\.([0-9]+)\.[0-9]+ ]]; then
        major="${BASH_REMATCH[1]}"
        minor="${BASH_REMATCH[2]}"
    else
        echo "Cannot determine WirePlumber version: $version" >&2
        return 1
    fi
    if (( major == 0 && minor < 4 )); then
        echo "WirePlumber 0.4 or newer is required (Raspberry Pi OS Bookworm or newer)." >&2
        return 1
    fi
    if (( major == 0 && minor == 4 )); then
        config_dir="$INSTALL_HOME/.config/wireplumber/bluetooth.lua.d"
        fragment=90-adhand-headless.lua
        obsolete="$INSTALL_HOME/.config/wireplumber/wireplumber.conf.d/90-adhand-headless.conf"
    else
        config_dir="$INSTALL_HOME/.config/wireplumber/wireplumber.conf.d"
        fragment=90-adhand-headless.conf
        obsolete="$INSTALL_HOME/.config/wireplumber/bluetooth.lua.d/90-adhand-headless.lua"
    fi
    as_owner mkdir -p "$config_dir"
    as_owner rm -f "$obsolete"
    as_root install -o "$INSTALL_UID" -g "$INSTALL_GID" -m 0644 \
        "$ADHAND_DEPLOY_DIR/wireplumber/$fragment" "$config_dir/$fragment"

    echo "==> Enabling headless audio for $INSTALL_USER (UID $INSTALL_UID)"
    as_root systemctl enable --now bluetooth.service
    as_root loginctl enable-linger "$INSTALL_USER"
    as_root systemctl start "user@$INSTALL_UID.service"
    # PipeWire owns the Pulse socket; avoid an older PulseAudio daemon competing.
    user_audio_command systemctl --user disable --now pulseaudio.service pulseaudio.socket 2>/dev/null || true
    user_audio_command systemctl --user mask pulseaudio.service pulseaudio.socket 2>/dev/null || true
    user_audio_command systemctl --user enable --now pipewire.socket pipewire-pulse.socket wireplumber.service
    user_audio_command systemctl --user restart pipewire.service pipewire-pulse.service wireplumber.service
    if ! user_audio_command pactl info; then
        echo "Audio server is starting; check systemctl --user status pipewire-pulse wireplumber." >&2
    fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    init_install_user
    configure_audio
fi

#!/usr/bin/env bash
# Raspberry Pi OS Lite 64-bit: interactive, headless SD/USB provisioning from Ubuntu.
# CAUTION: Irreversibly overwrites the chosen entire block device.
set -Eeuo pipefail
IFS=$'\n\t'
umask 077

IMAGE_URL='https://downloads.raspberrypi.com/raspios_lite_arm64_latest'
readonly TIMEZONE='America/New_York'
WORKDIR="${HOME}/rpi-image-cache"
DEVICE_OPT='' HOST_OPT='' USER_OPT='' SSID_OPT='' COUNTRY_OPT='' SHA_OPT=''
ADHAN=yes DRY_RUN=no VERBOSE=no LOGFILE='' IMAGE=''
usage() { cat <<'HELP'
Usage: flash-rpi-headless.sh [OPTIONS]

Flash Raspberry Pi OS Lite (64-bit) and provision a headless Raspberry Pi.
Run as a normal Ubuntu user, NOT root. Secrets are requested interactively.

Options:
  -d, --device PATH       Target whole USB/removable disk, e.g. /dev/sdb
  -H, --hostname NAME     Hostname (default: rpi3)
  -u, --username NAME     Linux username (default: maker)
      --wifi-ssid SSID    Wi-Fi network name (password prompted securely)
      --country CODE      Wi-Fi regulatory country (default: US)
      --with-adhan        Install Adhan on first Pi boot (default)
      --no-adhan          OS provisioning only
      --image-url URL     Alternate Raspberry Pi OS .img.xz URL
      --sha256 HASH       Expected SHA256 for downloaded compressed image
      --cache-dir DIR     Cache and log directory
      --log-file PATH     Write installer log to PATH (sensitive content redacted)
      --dry-run           Inspect options and disks; do not download or write
  -v, --verbose           More operation detail, without printing secrets
  -h, --help              Show this help

Example:
  ./flash-rpi-headless.sh --device /dev/sda --hostname rpi3 --username maker --with-adhan

Adhan source: https://github.com/aashekh-dev/raspberry-pi-adhan
Branch: fix/headless-bluetooth-audio
First-boot Adhan log: /var/log/rpi-adhan-bootstrap.log
First-boot service: rpi-adhan-bootstrap.service
Application service: adhand.service; web UI: http://HOST.local:8000
CAUTION: Confirming the erase irrevocably destroys ALL DATA on target.
HELP
}
while (($#)); do
 case "$1" in
 -h|--help) usage; exit 0;;
 -d|--device) DEVICE_OPT="${2:?missing --device value}"; shift 2;;
 -H|--hostname) HOST_OPT="${2:?missing --hostname value}"; shift 2;;
 -u|--username) USER_OPT="${2:?missing --username value}"; shift 2;;
 --wifi-ssid) SSID_OPT="${2:?missing --wifi-ssid value}"; shift 2;;
 --country) COUNTRY_OPT="${2:?missing --country value}"; shift 2;;
 --with-adhan) ADHAN=yes; shift;;
 --no-adhan) ADHAN=no; shift;;
 --image-url) IMAGE_URL="${2:?missing URL}"; shift 2;;
 --sha256) SHA_OPT="${2:?missing SHA256}"; shift 2;;
 --cache-dir) WORKDIR="${2:?missing directory}"; shift 2;;
 --log-file) LOGFILE="${2:?missing logfile}"; shift 2;;
 --dry-run) DRY_RUN=yes; shift;;
 -v|--verbose) VERBOSE=yes; shift;;
 *) printf 'Unknown argument: %s\n' "$1" >&2; usage >&2; exit 2;;
 esac
done
IMAGE="${WORKDIR}/raspios-lite-arm64.img.xz"
mkdir -p "$WORKDIR"
LOGFILE="${LOGFILE:-${WORKDIR}/flash-$(date +%Y%m%d-%H%M%S).log}"
mkdir -p "$(dirname "$LOGFILE")"
chmod 700 "$WORKDIR" 2>/dev/null || true
exec 3>>"$LOGFILE"
printf '[%s] START version=2.0 args: adhan=%s dry-run=%s verbose=%s\n' "$(date -Is)" "$ADHAN" "$DRY_RUN" "$VERBOSE" >&3

DEVICE='' MOUNTDIR='' TEMP_DIR='' MOUNTED=0 PASSWORD='' WIFI_PASSWORD=''

log() { printf '\n[INFO] %s\n' "$*"; printf '[%s] INFO %s\n' "$(date -Is)" "$*" >&3; }
step() { log "STEP: $*"; }
run() { log "RUN: $(printf '%q ' "$@")"; "$@"; log "OK: ${1##*/}"; }

die() { printf '\n[ERROR] %s\n' "$*" >&2; printf '[%s] ERROR %s\n' "$(date -Is)" "$*" >&3; exit 1; }
cleanup() {
  local rc=$?
  if (( MOUNTED )) && [[ -n "$MOUNTDIR" ]]; then sudo umount "$MOUNTDIR" || true; fi
  [[ -z "$MOUNTDIR" ]] || rmdir "$MOUNTDIR" 2>/dev/null || true
  [[ -z "$TEMP_DIR" ]] || rm -rf -- "$TEMP_DIR"
  PASSWORD='' WIFI_PASSWORD=''
  printf '[%s] EXIT code=%s\n' "$(date -Is)" "$rc" >&3
  exit "$rc"
}
trap cleanup EXIT
trap 'die "Interrupted."' INT TERM

ask() { local __var="$1" prompt="$2" default="${3-}" value; read -r -p "$prompt${default:+ [$default]}: " value; printf -v "$__var" '%s' "${value:-$default}"; }
ask_secret() { local __var="$1" prompt="$2" value; read -r -s -p "$prompt: " value; printf '\n'; printf -v "$__var" '%s' "$value"; }
# Safe YAML double-quoted scalar. Reject control characters, then escape \ and ".
yaml_quote() {
  local value="$1"
  [[ "$value" != *$'\n'* && "$value" != *$'\r'* && "$value" != *$'\t'* ]] || die 'Newlines or tabs are not supported in these input fields.'
  value=${value//\\/\\\\}; value=${value//\"/\\\"}
  printf '"%s"' "$value"
}
valid_name() { [[ "$1" =~ ^[a-z_][a-z0-9_-]*$ && ${#1} -le 32 ]]; }
valid_host() { [[ "$1" =~ ^[a-z][a-z0-9-]*$ && ${#1} -le 63 && "$1" != *- ]]; }

[[ -t 0 ]] || die 'Run interactively in a terminal (not piped from curl).'
[[ "$(id -u)" -ne 0 ]] || die 'Run as your regular Ubuntu user; the script uses sudo only when needed.'
command -v sudo >/dev/null || die 'sudo is required.'
log "Log file: $LOGFILE"
if [[ "$DRY_RUN" == yes ]]; then
  log 'DRY RUN: no packages, image downloads, credentials or disks will be changed.'
  lsblk -e 7 -o NAME,PATH,TYPE,TRAN,RM,SIZE,MODEL,FSTYPE,MOUNTPOINTS
  [[ -z "$DEVICE_OPT" ]] || lsblk "$DEVICE_OPT" -o NAME,PATH,TYPE,TRAN,RM,SIZE,MODEL,MOUNTPOINTS
  log "Planned target=${DEVICE_OPT:-interactive} hostname=${HOST_OPT:-rpi3} username=${USER_OPT:-maker} adhan=$ADHAN"
  exit 0
fi
step 'Installing required Ubuntu packages (requires sudo).'

run sudo apt-get update
run sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl xz-utils openssl util-linux parted file
for cmd in lsblk findmnt blockdev xz curl openssl dd partprobe mount umount udevadm; do command -v "$cmd" >/dev/null || die "Missing executable: $cmd"; done

printf '\nConnected disks (choose the WHOLE USB disk, not a partition):\n'
lsblk -e 7 -o NAME,PATH,TYPE,TRAN,RM,SIZE,MODEL,FSTYPE,MOUNTPOINTS
if [[ -n "$DEVICE_OPT" ]]; then DEVICE="$DEVICE_OPT"; else ask DEVICE 'USB card reader / USB drive device path (example /dev/sdb)'; fi
[[ "$DEVICE" == /dev/* && -b "$DEVICE" ]] || die 'Device path is not a valid block device.'
[[ "$(lsblk -dnro TYPE "$DEVICE")" == disk ]] || die 'Select the full disk, not /dev/sdb1.'
DEVICE=$(readlink -f -- "$DEVICE")
[[ "$VERBOSE" != yes ]] || log "Target device resolved to $DEVICE"
transport=$(lsblk -dnro TRAN "$DEVICE" | tr -d '[:space:]')
removable=$(lsblk -dnro RM "$DEVICE" | tr -d '[:space:]')
if [[ "$transport" != usb && "$removable" != 1 ]]; then
  die "Device is not reported as USB/removable (TRAN=$transport, RM=$removable). Refusing for safety."
fi
# Refuse any disk containing an important live filesystem (or swap).
while IFS= read -r part; do
  while IFS= read -r where; do
    case "$where" in
      /|/boot|/boot/efi|/home|/usr|/var|/var/*|/home/*) die "Device contains active system mount $where. Refusing." ;;
    esac
  done < <(lsblk -nro MOUNTPOINTS "$part" | sed '/^$/d')
done < <(lsblk -nrpo NAME "$DEVICE")
if swapon --noheadings --raw --show=NAME 2>/dev/null | grep -Fqx "$DEVICE"; then die 'Target is active swap.'; fi
# For a swap partition of the target:
while IFS= read -r part; do
  if swapon --noheadings --raw --show=NAME 2>/dev/null | grep -Fqx "$part"; then die 'A target partition is active swap.'; fi
done < <(lsblk -nrpo NAME "$DEVICE")

if [[ -n "$HOST_OPT" ]]; then HOSTNAME="$HOST_OPT"; else ask HOSTNAME 'Raspberry Pi hostname' 'rpi3'; fi
valid_host "$HOSTNAME" || die 'Hostname must be 1-63 lowercase letters/digits/hyphens, starting with a letter and not ending in a hyphen.'
if [[ -n "$USER_OPT" ]]; then USERNAME="$USER_OPT"; else ask USERNAME 'Linux username' 'maker'; fi
valid_name "$USERNAME" || die 'Invalid username. Use lowercase letters, digits, underscore, hyphen; start with lowercase or underscore.'
ask_secret PASSWORD 'Linux account password (hidden)'
[[ ${#PASSWORD} -ge 12 ]] || die 'Use an account password of at least 12 characters.'
ask_secret repeat 'Confirm Linux account password (hidden)'
[[ "$PASSWORD" == "$repeat" ]] || die 'Password confirmation does not match.'
repeat=''
if [[ -n "$SSID_OPT" ]]; then WIFI_SSID="$SSID_OPT"; else ask WIFI_SSID 'Wi-Fi SSID (leave blank for Ethernet only)' ''; fi
if [[ -n "$WIFI_SSID" ]]; then
  ask_secret WIFI_PASSWORD 'Wi-Fi passphrase (hidden)'
  [[ ${#WIFI_PASSWORD} -ge 8 && ${#WIFI_PASSWORD} -le 63 ]] || die 'Wi-Fi WPA2 passphrase must have 8-63 characters.'
  if [[ -n "$COUNTRY_OPT" ]]; then COUNTRY="$COUNTRY_OPT"; else ask COUNTRY 'Two-letter Wi-Fi regulatory country code' 'US'; fi
  [[ "$COUNTRY" =~ ^[A-Z]{2}$ ]] || die 'Country must be a two-letter uppercase ISO country code.'
fi

PUBKEY=''
if [[ -f "$HOME/.ssh/id_ed25519.pub" ]]; then DEFAULT_PUBKEY="$HOME/.ssh/id_ed25519.pub"; else DEFAULT_PUBKEY=''; fi
ask KEY_PATH 'Existing SSH public key path (blank to skip)' "$DEFAULT_PUBKEY"
if [[ -n "$KEY_PATH" ]]; then
  [[ -f "$KEY_PATH" ]] || die 'SSH public key file does not exist.'
  PUBKEY=$(head -n 1 -- "$KEY_PATH")
  command -v ssh-keygen >/dev/null || die 'ssh-keygen is needed to validate the public key.'
  ssh-keygen -lf "$KEY_PATH" >/dev/null 2>&1 || die 'Public SSH key is invalid.'
  [[ "$PUBKEY" =~ ^(ssh-ed25519|ssh-rsa|ecdsa-sha2-[a-z0-9-]+)[[:space:]] ]] || die 'Unsupported public-key format.'
fi

if [[ -n "$PUBKEY" ]]; then
  ask SSH_PASSWORD_LOGIN 'Allow SSH password authentication? (yes/no)' 'no'
else
  log 'No SSH public key chosen: SSH password authentication is required.'
  SSH_PASSWORD_LOGIN=yes
fi
case "$SSH_PASSWORD_LOGIN" in yes|y) SSH_PASSWORD_LOGIN=true ;; no|n) SSH_PASSWORD_LOGIN=false ;; *) die 'Answer yes or no.' ;; esac
ask SUDO_NOPASSWD 'Allow passwordless sudo? (yes/no)' 'no'
case "$SUDO_NOPASSWD" in yes|y) SUDO_RULE='ALL=(ALL) NOPASSWD:ALL' ;; no|n) SUDO_RULE='' ;; *) die 'Answer yes or no.' ;; esac

mkdir -p "$WORKDIR"
if [[ -s "$IMAGE" ]]; then
  ask USE_CACHE 'Existing compressed image found. Reuse it? (yes/no)' 'yes'
else USE_CACHE=no; fi
if [[ "$USE_CACHE" != yes && "$USE_CACHE" != y ]]; then
  log 'Downloading latest Raspberry Pi OS Lite 64-bit image.'
  curl --fail --location --retry 3 --retry-delay 2 --connect-timeout 30 --output "${IMAGE}.download" "$IMAGE_URL"
  mv -f -- "${IMAGE}.download" "$IMAGE"
fi
[[ "$VERBOSE" != yes ]] || log "Downloaded archive size: $(stat -c %s "$IMAGE") bytes"
step 'Checking xz archive integrity.'
xz -t "$IMAGE" || die 'Image decompression integrity test failed.'
if [[ -n "$SHA_OPT" ]]; then EXPECTED_SHA256="$SHA_OPT"; else ask EXPECTED_SHA256 'Optional official SHA256 for compressed image (blank to skip)' ''; fi
if [[ -n "$EXPECTED_SHA256" ]]; then
  [[ "$EXPECTED_SHA256" =~ ^[[:xdigit:]]{64}$ ]] || die 'SHA256 must contain 64 hex characters.'
  actual=$(sha256sum "$IMAGE" | awk '{print $1}')
  [[ "${actual,,}" == "${EXPECTED_SHA256,,}" ]] || die 'Image SHA256 DOES NOT match. Refusing to flash.'
fi

printf '\n========== FINAL DESTRUCTIVE OPERATION ==========\n'
lsblk "$DEVICE" -o NAME,PATH,TYPE,TRAN,RM,SIZE,MODEL,MOUNTPOINTS
printf 'Hostname: %s | User: %s | Timezone: %s | Wi-Fi: %s\n' "$HOSTNAME" "$USERNAME" "$TIMEZONE" "${WIFI_SSID:+configured}"
printf 'ALL DATA ON %s WILL BE DESTROYED.\n' "$DEVICE"
printf 'Adhan first-boot deployment: %s\n' "$ADHAN"
log "Awaiting erase confirmation for $DEVICE"
read -r -p "Type ERASE $DEVICE to confirm: " confirm
[[ "$confirm" == "ERASE $DEVICE" ]] || die 'Confirmation mismatch. Nothing was flashed.'

# Unmount all target partitions, including automounted card partitions.
mapfile -t partitions < <(lsblk -nrpo NAME "$DEVICE" | tail -n +2)
for ((idx=${#partitions[@]}-1; idx>=0; idx--)); do
  sudo umount -- "${partitions[idx]}" 2>/dev/null || true
done
while IFS= read -r where; do [[ -z "$where" ]] || die "Still mounted at $where; unmount manually."; done < <(lsblk -nro MOUNTPOINTS "$DEVICE" | sed '/^$/d')
for part in "${partitions[@]}"; do
  while IFS= read -r where; do [[ -z "$where" ]] || die "Still mounted at $where; unmount manually."; done < <(lsblk -nro MOUNTPOINTS "$part" | sed '/^$/d')
done
step "Flashing image to $DEVICE. Do not disconnect the USB device."
set +e
xz -dc -- "$IMAGE" | sudo dd of="$DEVICE" bs=4M iflag=fullblock status=progress conv=fsync
rc=("${PIPESTATUS[@]}")
set -e
(( rc[0] == 0 && rc[1] == 0 )) || die "Image write failed (xz=${rc[0]}, dd=${rc[1]})."
sync
run sudo partprobe "$DEVICE"
run sudo udevadm settle

# Partition paths differ between /dev/sdb1 and /dev/mmcblk0p1.
BOOT_PART=$(lsblk -nrpo NAME,TYPE,FSTYPE "$DEVICE" | awk '$2=="part" && ($3=="vfat" || $3=="fat32") {print $1; exit}')
[[ -n "$BOOT_PART" && -b "$BOOT_PART" ]] || die 'Boot FAT partition not found after flashing.'
TEMP_DIR=$(mktemp -d)
MOUNTDIR=$(mktemp -d /tmp/rpi-boot.XXXXXXXX)
run sudo mount -o rw "$BOOT_PART" "$MOUNTDIR"
MOUNTED=1
[[ -f "$MOUNTDIR/config.txt" ]] || die 'Boot partition lacks config.txt, unexpected image structure.'
[[ -f "$MOUNTDIR/user-data" && -f "$MOUNTDIR/network-config" && -f "$MOUNTDIR/meta-data" ]] || die 'Image does not contain the expected cloud-init provisioning files. Do not boot; inspect release/init format.'

HASH=$(printf '%s' "$PASSWORD" | openssl passwd -6 -stdin)
PASSWORD=''
{
  printf '#cloud-config\n'
  printf 'hostname: %s\nmanage_etc_hosts: true\ntimezone: %s\nlocale: en_US.UTF-8\n' "$HOSTNAME" "$TIMEZONE"
  printf 'users:\n  - name: %s\n' "$USERNAME"
  printf '    groups: users,adm,dialout,audio,netdev,video,plugdev,cdrom,games,input,gpio,spi,i2c,render,sudo\n'
  printf '    shell: /bin/bash\n    lock_passwd: false\n    passwd: %s\n' "$(yaml_quote "$HASH")"
  if [[ -n "$SUDO_RULE" ]]; then printf '    sudo: %s\n' "$(yaml_quote "$SUDO_RULE")"; else printf '    sudo: null\n'; fi
  if [[ -n "$PUBKEY" ]]; then printf '    ssh_authorized_keys:\n      - %s\n' "$(yaml_quote "$PUBKEY")"; fi
  printf 'enable_ssh: true\nssh_pwauth: %s\n' "$SSH_PASSWORD_LOGIN"
  printf 'package_update: false\npackage_upgrade: false\n'
  if [[ "$ADHAN" == yes ]]; then
    # First boot provisioning creates a dedicated retryable systemd oneshot unit.
    # cloud-init writes the installer script; systemd starts it only after networking.
    printf 'write_files:\n'
    printf '  - path: /usr/local/sbin/rpi-adhan-bootstrap\n    owner: root:root\n    permissions: "0755"\n    content: |\n'
    while IFS= read -r line; do printf '      %s\n' "$line"; done <<'BOOT'
#!/usr/bin/env bash
set -Eeuo pipefail
exec > >(tee -a /var/log/rpi-adhan-bootstrap.log) 2>&1
printf '[%s] Starting Adhan deployment\n' "$(date -Is)"
# Do not report success without the app/service actually installed.
getent passwd '__TARGET_USER__' >/dev/null
apt-get update
apt-get install -y git ca-certificates
repo='/home/__TARGET_USER__/raspberry-pi-adhan'
if [[ ! -d "$repo/.git" ]]; then
  runuser -u '__TARGET_USER__' -- git clone --branch fix/headless-bluetooth-audio --single-branch https://github.com/aashekh-dev/raspberry-pi-adhan.git "$repo"
fi
cd "$repo"
ADHAND_USER='__TARGET_USER__' bash deploy/install.sh
systemctl is-active --quiet adhand.service
printf '[%s] Adhan deployment verified active\n' "$(date -Is)"
BOOT
    printf '  - path: /etc/systemd/system/rpi-adhan-bootstrap.service\n    owner: root:root\n    permissions: "0644"\n    content: |\n'
    while IFS= read -r line; do printf '      %s\n' "$line"; done <<'UNIT'
[Unit]
Description=Install Raspberry Pi Adhan application on first boot
Wants=network-online.target
After=network-online.target
ConditionPathExists=!/var/lib/rpi-adhan/bootstrap-done
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/rpi-adhan-bootstrap
ExecStartPost=/usr/bin/mkdir -p /var/lib/rpi-adhan
ExecStartPost=/usr/bin/touch /var/lib/rpi-adhan/bootstrap-done
TimeoutStartSec=0
[Install]
WantedBy=multi-user.target
UNIT
    printf 'runcmd:\n'
    printf '  - [ systemctl, daemon-reload ]\n'
    printf '  - [ systemctl, enable, --now, rpi-adhan-bootstrap.service ]\n'
  fi
} > "$TEMP_DIR/user-data"
if [[ "$ADHAN" == yes ]]; then
  # Placeholder replacement is constrained by valid_name() above.
  sed -i "s/__TARGET_USER__/$USERNAME/g" "$TEMP_DIR/user-data"
fi
{
  printf 'network:\n  version: 2\n  ethernets:\n    eth0:\n      dhcp4: true\n      optional: true\n'
  if [[ -n "$WIFI_SSID" ]]; then
    printf '  wifis:\n    renderer: NetworkManager\n    wlan0:\n      dhcp4: true\n      optional: true\n      regulatory-domain: %s\n      access-points:\n        %s:\n          password: %s\n' "$(yaml_quote "$COUNTRY")" "$(yaml_quote "$WIFI_SSID")" "$(yaml_quote "$WIFI_PASSWORD")"
  fi
} > "$TEMP_DIR/network-config"
WIFI_PASSWORD=''
# Basic format validation when PyYAML is already installed, not a required dependency.
if command -v python3 >/dev/null && python3 -c 'import yaml' 2>/dev/null; then
  python3 - "$TEMP_DIR/user-data" "$TEMP_DIR/network-config" <<'PY'
import sys, yaml
for path in sys.argv[1:]:
    with open(path, encoding='utf-8') as f:
        assert isinstance(yaml.safe_load(f), dict), f'Invalid YAML in {path}'
PY
fi
step 'Installing cloud-init user-data (contents intentionally excluded from logs)'
sudo install -m 0600 "$TEMP_DIR/user-data" "$MOUNTDIR/user-data"
step 'Installing cloud-init network-config (Wi-Fi credentials intentionally excluded from logs)'
sudo install -m 0600 "$TEMP_DIR/network-config" "$MOUNTDIR/network-config"
# Keep the image-provided meta-data intact. It is used to enable the NoCloud datasource.
sync
sudo umount "$MOUNTDIR"
MOUNTED=0
sudo udevadm settle
log "SUCCESS: $DEVICE is flashed and provisioned for $HOSTNAME."
log "Adhan first-boot bootstrap enabled: $ADHAN"
log "Detailed host-side log: $LOGFILE"
printf 'First boot: power on the Raspberry Pi, then connect using ssh %s@%s.local\n' "$USERNAME" "$HOSTNAME"
printf 'If mDNS fails, find the device IP from your router and SSH directly.\n'
if [[ "$ADHAN" == yes ]]; then printf 'Adhan: http://%s.local:8000; status: sudo systemctl status rpi-adhan-bootstrap adhand; logs: sudo journalctl -u rpi-adhan-bootstrap -u adhand -f\n' "$HOSTNAME"; fi
printf 'Note: Wi-Fi password and account password hash remain readable from the FAT boot partition; store the card securely.\n'

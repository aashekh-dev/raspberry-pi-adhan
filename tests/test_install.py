"""Installer tests never call the host's package manager or systemd."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def renderer():
    spec = importlib.util.spec_from_file_location("render_service", ROOT / "deploy/render_service.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_service_uses_real_user_home_and_uid():
    output = renderer().render_service(ROOT / "deploy/adhand.service", "adhan", "/srv/adhan", 1042,
                                       "/srv/adhan/my project%")
    assert "User=adhan" in output
    assert 'WorkingDirectory=/srv/adhan/my project%%\n' in output
    assert 'ExecStart=/usr/bin/env .venv/bin/uvicorn main:app' in output
    assert 'Environment="HOME=/srv/adhan"' in output
    assert 'Environment="XDG_RUNTIME_DIR=/run/user/1042"' in output
    assert 'Environment="PULSE_SERVER=unix:/run/user/1042/pulse/native"' in output
    assert 'Environment="DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1042/bus"' in output
    assert "user@1042.service" in output
    assert "/home/pi/" not in output
    assert "@ADHAND_" not in output


@pytest.mark.parametrize("value", ['/srv/quote"here', '/srv/back\\slash'])
def test_service_escapes_paths(value):
    output = renderer().render_service(ROOT / "deploy/adhand.service", "adhan", "/srv/adhan", 1042, value)
    assert f'WorkingDirectory={value}\n' in output


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="systemd parser unavailable")
def test_rendered_directory_is_accepted_by_systemd(tmp_path):
    output = renderer().render_service(ROOT / "deploy/adhand.service", "adhan", "/srv/adhan", 1042,
                                       str(tmp_path / "repo with spaces%"))
    directory = next(line for line in output.splitlines() if line.startswith("WorkingDirectory="))
    service = tmp_path / "directory-test.service"
    service.write_text(f"[Service]\n{directory}\nExecStart=/bin/true\n")
    result = subprocess.run(["systemd-analyze", "verify", str(service)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="systemd parser unavailable")
def test_rendered_executable_handles_literal_dollars_and_quotes(tmp_path):
    repo = tmp_path / 'repo with $literal% "quote"'
    executable = repo / ".venv/bin/uvicorn"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    output = renderer().render_service(ROOT / "deploy/adhand.service", "adhan", "/srv/adhan", 1042, str(repo))
    settings = [line for line in output.splitlines() if line.startswith(("WorkingDirectory=", "ExecStart=", "Environment="))]
    service = tmp_path / "executable-test.service"
    service.write_text("[Service]\n" + "\n".join(settings) + "\n")
    result = subprocess.run(["systemd-analyze", "verify", str(service)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    launched = subprocess.run(["/usr/bin/env", ".venv/bin/uvicorn"], cwd=repo,
                              capture_output=True, text=True, timeout=10)
    assert launched.returncode == 0, launched.stderr


@pytest.fixture
def fake_installer(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launcher = bin_dir / "fake-command"
    launcher.write_text(f"#!{sys.executable}\n" + '''
import json, os, pathlib, shutil, subprocess, sys
name, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps([name, *args]) + "\\n")
if name == "getent":
    print("adhan:x:1042:1042::" + os.environ["FAKE_INSTALL_HOME"] + ":/bin/bash")
elif name == "id":
    print("1042" if len(args) > 1 else ("0" if args == ["-u"] else "adhan"))
elif name == "wireplumber":
    print("wireplumber " + os.environ["FAKE_WP_VERSION"])
elif name == "sudo":
    if args[:1] == ["-u"]:
        args = args[2:]
    if args[:1] == ["--"]:
        args = args[1:]
    sys.exit(subprocess.call(args))
elif name == "install":
    shutil.copyfile(args[-2], args[-1])
elif name == "pactl":
    print("Server Name: PulseAudio (on PipeWire)")
elif name == "python3":
    if args[:2] == ["-m", "venv"]:
        bin_dir = pathlib.Path(args[2]) / "bin"
        bin_dir.mkdir(parents=True)
        for tool in ("pip", "python"):
            shutil.copyfile(sys.argv[0], bin_dir / tool)
            (bin_dir / tool).chmod(0o755)
    else:
        sys.exit(subprocess.call([sys.executable, *args]))
elif name == "tee":
    pathlib.Path(os.environ["FAKE_SERVICE"]).write_text(sys.stdin.read())
''')
    launcher.chmod(0o755)
    for name in ("getent", "id", "wireplumber", "sudo", "install", "systemctl", "loginctl", "pactl",
                 "apt-get", "python3", "tee", "crontab"):
        (bin_dir / name).symlink_to(launcher)
    user_home = tmp_path / "service user"
    user_home.mkdir()
    log = tmp_path / "commands.jsonl"
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", SUDO_USER="adhan", USER="root",
               FAKE_INSTALL_HOME=str(user_home), FAKE_LOG=str(log), FAKE_WP_VERSION="0.5.6")
    return env, user_home, log


@pytest.mark.parametrize("version,relative,content", [
    ("0.4.17", "bluetooth.lua.d/90-adhand-headless.lua", '["with-logind"] = false'),
    ("0.5.6", "wireplumber.conf.d/90-adhand-headless.conf", "monitor.bluez.seat-monitoring = disabled"),
])
def test_headless_audio_setup_for_sudo_user(fake_installer, version, relative, content):
    env, user_home, log = fake_installer
    env["FAKE_WP_VERSION"] = version
    result = subprocess.run(["bash", str(ROOT / "deploy/setup-audio.sh")], env=env,
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert content in (user_home / ".config/wireplumber" / relative).read_text()
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    assert ["loginctl", "enable-linger", "adhan"] in commands
    assert ["systemctl", "start", "user@1042.service"] in commands
    assert any(command[:3] == ["systemctl", "--user", "enable"] and "pipewire-pulse.socket" in command
               and "wireplumber.service" in command for command in commands)
    owner_commands = [command for command in commands if command[:3] == ["sudo", "-u", "adhan"]]
    assert any("XDG_RUNTIME_DIR=/run/user/1042" in command and
               "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1042/bus" in command
               for command in owner_commands)


def test_audio_setup_removes_only_its_obsolete_version_config(fake_installer):
    env, user_home, _ = fake_installer
    legacy_dir = user_home / ".config/wireplumber/bluetooth.lua.d"
    legacy_dir.mkdir(parents=True)
    obsolete = legacy_dir / "90-adhand-headless.lua"
    obsolete.write_text("old configuration")
    other = legacy_dir / "custom.lua"
    other.write_text("my configuration")
    result = subprocess.run(["bash", str(ROOT / "deploy/setup-audio.sh")], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert not obsolete.exists()
    assert other.read_text() == "my configuration"


def test_complete_install_has_a2dp_dependencies_and_renders_account(fake_installer, tmp_path):
    env, user_home, log = fake_installer
    repo = tmp_path / "repo with spaces"
    shutil.copytree(ROOT / "deploy", repo / "deploy")
    env["FAKE_SERVICE"] = str(tmp_path / "adhand.service")
    result = subprocess.run(["bash", str(repo / "deploy/install.sh")], env=env,
                            text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    install = next(command for command in commands if command[:2] == ["apt-get", "install"])
    for package in ("mpv", "bluez", "pipewire", "pipewire-pulse", "wireplumber",
                    "libspa-0.2-bluetooth", "pulseaudio-utils", "dbus-user-session"):
        assert package in install
    service = Path(env["FAKE_SERVICE"]).read_text()
    assert "User=adhan" in service
    assert f'Environment="HOME={user_home}"' in service
    assert 'Environment="XDG_RUNTIME_DIR=/run/user/1042"' in service
    assert f'WorkingDirectory={repo}\n' in service
    assert ["systemctl", "restart", "adhand"] in commands

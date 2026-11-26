"""Tests for Game Mode split-tunnel config, clean shutdown, and parent-PID watchdog."""
from __future__ import annotations

import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from relay.core.config import ConfigStore, sanitize_app_name
from relay.proxy.gametunnel import (
    GameTunnelClient,
    _is_pid_alive,
    build_singbox_config,
    resolve_game_binaries,
    run_game_tunnel_supervisor,
)
from relay.service.controller import RelayService


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_sanitize_app_name_and_config_persistence(tmp_path: Path):
    assert sanitize_app_name("RobloxPlayerBeta") == "RobloxPlayerBeta.exe"
    assert sanitize_app_name(r"C:\Games\Roblox\RobloxPlayerBeta.exe") == "RobloxPlayerBeta.exe"
    assert sanitize_app_name("bad name.exe") == ""
    assert sanitize_app_name("bad;cmd.exe") == ""

    store = ConfigStore(tmp_path)
    cfg = store.load().with_updates(game_mode_apps=["RobloxPlayerBeta", "Valorant.exe", "robloxplayerbeta.exe"])
    store.save(cfg)
    reloaded = store.load()
    assert reloaded.game_mode_apps == ("RobloxPlayerBeta.exe", "Valorant.exe")


def test_singbox_config_is_valid_and_prevents_loops(tmp_path: Path):
    cfg = build_singbox_config(["RobloxPlayerBeta.exe", "Discord.exe"], socks_port=11894)
    assert cfg["route"]["final"] == "direct"
    rules = cfg["route"]["rules"]
    assert any(r.get("outbound") == "direct" and "usque.exe" in r.get("process_name", []) for r in rules)
    assert any(r.get("outbound") == "direct" and r.get("ip_is_private") is True for r in rules)
    assert any(
        r.get("outbound") == "usque-socks" and "RobloxPlayerBeta.exe" in r.get("process_name", [])
        for r in rules
    )

    bins = resolve_game_binaries()
    if bins is not None:
        cfg_file = tmp_path / "singbox-check.json"
        cfg_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        res = subprocess.run([str(bins[0]), "check", "-c", str(cfg_file)], capture_output=True, text=True, timeout=10)
        assert res.returncode == 0, res.stderr


def test_game_tunnel_supervisor_clean_stop_and_auth(tmp_path: Path, monkeypatch):
    # Use python -c "import time; time.sleep(60)" as a safe stand-in for sing-box.exe
    fake_bin = tmp_path / "fake.exe"
    fake_bin.write_bytes(b"")
    cfg_file = tmp_path / "cfg.json"
    cfg_file.write_text("{}", encoding="utf-8")
    port = _free_port()
    token = "test-secret-token-123456"

    monkeypatch.setattr("relay.proxy.gametunnel.resolve_game_binaries", lambda _d=None: (fake_bin, fake_bin))
    monkeypatch.setattr("relay.proxy.gametunnel.default_data_dir", lambda: tmp_path)

    real_popen = subprocess.Popen
    spawned_pids: list[int] = []

    def _mock_popen(_cmd, **kwargs):
        p = real_popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
        spawned_pids.append(p.pid)
        return p

    monkeypatch.setattr("relay.proxy.gametunnel.subprocess.Popen", _mock_popen)

    args = SimpleNamespace(config=str(cfg_file), control_port=port, parent_pid=0, token=token)
    t = threading.Thread(target=run_game_tunnel_supervisor, args=(args,), daemon=True)
    t.start()

    opener = build_opener(ProxyHandler({}))
    client = GameTunnelClient(tmp_path, control_port=port)
    (tmp_path / "control.token").write_text(token, encoding="utf-8")

    # Wait for supervisor health
    for _ in range(30):
        if client.is_running():
            break
        time.sleep(0.1)
    assert client.is_running()
    assert len(spawned_pids) == 1
    child_pid = spawned_pids[0]
    assert _is_pid_alive(child_pid)

    # Unauthorized /stop must return 403
    bad_req = Request(f"http://127.0.0.1:{port}/stop", method="POST", data=b"{}", headers={"X-Relay-Token": "wrong"})
    try:
        opener.open(bad_req, timeout=2.0)
        assert False, "Expected 403 Forbidden"
    except HTTPError as exc:
        assert exc.code == 403

    # Authorized stop must terminate both supervisor AND child process cleanly
    assert client.stop() is True
    t.join(timeout=4.0)
    assert not t.is_alive()
    assert not _is_pid_alive(child_pid)


def test_game_tunnel_supervisor_auto_exits_when_parent_dies(tmp_path: Path, monkeypatch):
    fake_bin = tmp_path / "fake.exe"
    fake_bin.write_bytes(b"")
    cfg_file = tmp_path / "cfg.json"
    cfg_file.write_text("{}", encoding="utf-8")
    port = _free_port()

    monkeypatch.setattr("relay.proxy.gametunnel.resolve_game_binaries", lambda _d=None: (fake_bin, fake_bin))
    monkeypatch.setattr("relay.proxy.gametunnel.default_data_dir", lambda: tmp_path)

    real_popen = subprocess.Popen
    spawned_pids: list[int] = []

    def _mock_popen(_cmd, **kwargs):
        p = real_popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
        spawned_pids.append(p.pid)
        return p

    monkeypatch.setattr("relay.proxy.gametunnel.subprocess.Popen", _mock_popen)

    # Spawn a short-lived dummy parent process that exits after 0.6s
    parent = real_popen([sys.executable, "-c", "import time; time.sleep(0.6)"])
    args = SimpleNamespace(config=str(cfg_file), control_port=port, parent_pid=parent.pid, token="tok")
    t = threading.Thread(target=run_game_tunnel_supervisor, args=(args,), daemon=True)
    t.start()

    parent.wait(timeout=3.0)
    t.join(timeout=4.0)
    assert not t.is_alive(), "Supervisor must auto-exit when parent process dies"
    assert spawned_pids and not _is_pid_alive(spawned_pids[0]), "Child sing-box must be killed on parent death"


def test_service_disable_always_stops_game_tunnel(tmp_path: Path):
    stopped = []

    class FakeProxyStore:
        def active_port(self): return None
        def capture_baseline(self): return []
        def load_baseline(self): return []
        def save_baseline(self, b): pass
        def clear_baseline(self): pass
        def apply_proxy(self, port, *, system_proxy): pass
        def restore_baseline(self, b): pass
        def set_boot_safety_net(self, e): pass

    class FakeWorker:
        def health(self, p): return None
        def stats(self, p): return None
        def is_ready(self, p): return False
        def start(self, p, c, *, force_restart=False): pass
        def stop(self, p): pass
        def update_config(self, p, c): return True
        def flush(self, p, *, clear_persisted=False): return True

    class FakeGameTunnel:
        def is_running(self): return False
        def stop(self):
            stopped.append(True)
            return True

    svc = RelayService(ConfigStore(tmp_path), FakeProxyStore(), FakeWorker(), game_tunnel=FakeGameTunnel())
    svc.disable()
    assert stopped == [True]

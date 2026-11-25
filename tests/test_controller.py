"""Unit tests for RelayService using dependency-injected fake OS stores."""
import pytest

from relay.core.config import ConfigStore, NetworkMode, RelayConfig
from relay.service.controller import RelayService


class FakeProxyStore:
    def __init__(self, fail_apply: bool = False):
        self.fail_apply = fail_apply
        self._active_port: int | None = None
        self.system_proxy = False
        self.saved_baseline: list[dict] | None = None
        self.restored_count = 0
        self.boot_guard = False

    def active_port(self) -> int | None:
        return self._active_port

    def capture_baseline(self) -> list[dict]:
        return [{"Name": "ProxyEnable", "Exists": True, "Value": 0, "Kind": 4}]

    def load_baseline(self) -> list[dict]:
        return self.saved_baseline or self.capture_baseline()

    def save_baseline(self, baseline: list[dict]) -> None:
        self.saved_baseline = baseline

    def clear_baseline(self) -> None:
        self.saved_baseline = None

    def apply_proxy(self, port: int, *, system_proxy: bool) -> None:
        if self.fail_apply:
            raise OSError("Simulated registry write failure")
        self._active_port = port
        self.system_proxy = system_proxy

    def restore_baseline(self, baseline: list[dict]) -> None:
        self._active_port = None
        self.system_proxy = False
        self.restored_count += 1

    def set_boot_safety_net(self, enabled: bool) -> None:
        self.boot_guard = enabled


class FakeWorkerManager:
    def __init__(self):
        self.running: dict[int, RelayConfig] = {}
        self.stop_calls: list[int] = []

    def health(self, port: int) -> dict | None:
        if port not in self.running:
            return None
        cfg = self.running[port]
        return {"ok": True, "pid": 9999, "mode": cfg.mode.value, "doh": cfg.doh_enabled}

    def stats(self, port: int) -> dict | None:
        return self.health(port)

    def is_ready(self, port: int) -> bool:
        return port in self.running

    def start(self, port: int, config: RelayConfig, *, force_restart: bool = False) -> None:
        if force_restart and port in self.running:
            self.stop(port)
        self.running[port] = config

    def stop(self, port: int) -> None:
        self.stop_calls.append(port)
        self.running.pop(port, None)


def test_enable_disable_lifecycle_and_boot_guard(tmp_path):
    proxy_store = FakeProxyStore()
    worker_mgr = FakeWorkerManager()
    svc = RelayService(ConfigStore(tmp_path), proxy_store, worker_mgr, port=11893)

    assert not svc.status().connected
    st = svc.enable()
    assert st.connected
    assert proxy_store.active_port() == 11893
    assert proxy_store.system_proxy is False
    assert 11893 in worker_mgr.running

    st2 = svc.disable()
    assert not st2.connected
    assert proxy_store.active_port() is None
    assert proxy_store.boot_guard is False
    assert proxy_store.restored_count == 1
    assert 11893 in worker_mgr.stop_calls


def test_enable_rolls_back_on_registry_failure(tmp_path):
    proxy_store = FakeProxyStore(fail_apply=True)
    worker_mgr = FakeWorkerManager()
    svc = RelayService(ConfigStore(tmp_path), proxy_store, worker_mgr, port=11893)

    with pytest.raises(OSError):
        svc.enable()
    assert proxy_store.restored_count == 1
    assert proxy_store.boot_guard is False


def test_update_config_persists_across_instances(tmp_path):
    svc = RelayService(ConfigStore(tmp_path), FakeProxyStore(), FakeWorkerManager())
    svc.update_config(mode=NetworkMode.STRICT, doh_enabled=False)

    svc2 = RelayService(ConfigStore(tmp_path), FakeProxyStore(), FakeWorkerManager())
    assert svc2.config.mode == NetworkMode.STRICT
    assert svc2.config.doh_enabled is False

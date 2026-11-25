"""Application orchestrator coordinating ConfigStore, ProxySettingsStore, and WorkerManager."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading

from relay.core.config import RELAY_PORT, ConfigStore, RelayConfig, default_data_dir
from relay.platform.diagnostics import run_diagnostics
from relay.platform.registry import ProxySettingsStore, WindowsProxyStore
from relay.platform.worker import LocalWorkerManager, WorkerProcessManager


@dataclass(frozen=True)
class ServiceStatus:
    active_port: int | None
    ready: bool
    stats: dict | None

    @property
    def connected(self) -> bool:
        return bool(self.active_port is not None and self.ready)


class RelayService:
    """Thread-safe use-case facade with injected platform dependencies."""

    def __init__(
        self,
        config_store: ConfigStore,
        proxy_store: ProxySettingsStore,
        worker_manager: WorkerProcessManager,
        port: int = RELAY_PORT,
    ):
        self._lock = threading.Lock()
        self._config_store = config_store
        self._proxy_store = proxy_store
        self._worker_manager = worker_manager
        self.port = port
        self.config: RelayConfig = self._config_store.load()

    @property
    def data_dir(self) -> Path:
        return self._config_store.data_dir

    def update_config(self, **updates) -> RelayConfig:
        with self._lock:
            self.config = self._config_store.save(self.config.with_updates(**updates))
            return self.config

    def apply_config_live(self, *, registry_only: bool = False) -> ServiceStatus:
        """Hot-reload configuration into the running worker without dropping active connections."""
        with self._lock:
            current_port = self._proxy_store.active_port()
            if current_port is None:
                return ServiceStatus(active_port=None, ready=False, stats=None)
            if not registry_only:
                updated = False
                if hasattr(self._worker_manager, "update_config"):
                    updated = self._worker_manager.update_config(current_port, self.config)
                if not updated:
                    self._worker_manager.start(self.port, self.config, force_restart=True)
                    current_port = self.port
            self._proxy_store.apply_proxy(current_port, system_proxy=self.config.system_proxy)
        return self.status()

    def flush_caches(self, *, clear_persisted: bool = False) -> bool:
        with self._lock:
            current_port = self._proxy_store.active_port()
            if current_port is None:
                return False
            if hasattr(self._worker_manager, "flush"):
                return bool(self._worker_manager.flush(current_port, clear_persisted=clear_persisted))
            return False

    def recover_if_needed(self) -> ServiceStatus:
        """Watchdog: if proxy settings point to Relay but the worker died, restart it or restore baseline."""
        st = self.status()
        if st.active_port is not None and not st.ready:
            try:
                return self.enable(force_restart=True)
            except Exception:
                # Never leave ProxyEnable=1 pointing at a dead worker.
                self.disable(stop_worker=False)
                raise
        return st

    def enable(self, *, force_restart: bool = False) -> ServiceStatus:
        with self._lock:
            current_port = self._proxy_store.active_port()
            if current_port is not None and current_port != self.port:
                self._worker_manager.stop(current_port)

            self._worker_manager.start(self.port, self.config, force_restart=force_restart)

            baseline = (
                self._proxy_store.load_baseline()
                if current_port is not None
                else self._proxy_store.capture_baseline()
            )
            self._proxy_store.save_baseline(baseline)
            try:
                self._proxy_store.apply_proxy(self.port, system_proxy=self.config.system_proxy)
            except OSError:
                self._proxy_store.restore_baseline(baseline)
                raise

        return self.status()

    def disable(self, *, stop_worker: bool = True) -> ServiceStatus:
        with self._lock:
            current_port = self._proxy_store.active_port()
            if current_port is not None:
                baseline = self._proxy_store.load_baseline()
                self._proxy_store.restore_baseline(baseline)
                self._proxy_store.clear_baseline()
                if stop_worker:
                    self._worker_manager.stop(current_port)
            elif stop_worker:
                if self._worker_manager.is_ready(self.port):
                    self._worker_manager.stop(self.port)
        return self.status()

    def status(self) -> ServiceStatus:
        active_port = self._proxy_store.active_port()
        if active_port is None:
            return ServiceStatus(active_port=None, ready=False, stats=None)
        stats = self._worker_manager.stats(active_port)
        ready = stats is not None or self._worker_manager.is_ready(active_port)
        return ServiceStatus(active_port=active_port, ready=ready, stats=stats)

    def run_diagnostics(self) -> list[dict]:
        st = self.status()
        return run_diagnostics(st.active_port if st.connected else None)


def create_default_service(data_dir: Path | None = None) -> RelayService:
    base = data_dir or default_data_dir()
    return RelayService(
        config_store=ConfigStore(base),
        proxy_store=WindowsProxyStore(base),
        worker_manager=LocalWorkerManager(base),
        port=RELAY_PORT,
    )

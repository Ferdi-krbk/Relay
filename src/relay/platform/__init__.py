"""OS platform boundary: Windows proxy registry, worker process lifecycle, diagnostics."""
from relay.platform.diagnostics import DIAGNOSTIC_TARGETS, DiagnosticResult, run_diagnostics
from relay.platform.registry import ProxySettingsStore, WindowsProxyStore
from relay.platform.worker import LocalWorkerManager, WorkerProcessManager

__all__ = [
    "DIAGNOSTIC_TARGETS",
    "DiagnosticResult",
    "LocalWorkerManager",
    "ProxySettingsStore",
    "WindowsProxyStore",
    "WorkerProcessManager",
    "run_diagnostics",
]

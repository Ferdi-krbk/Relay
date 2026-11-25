"""Core domain models, configuration, PAC generator, and telemetry."""
from relay.core.config import (
    DEFAULT_FORCE_DOMAINS,
    LEGACY_PORTS,
    MODE_PRESETS,
    RELAY_PORT,
    ConfigStore,
    ModePreset,
    NetworkMode,
    RelayConfig,
    domain_matches,
    sanitize_domain,
)
from relay.core.pac import generate_pac
from relay.core.telemetry import ConnectionEvent, ProxyTelemetry

__all__ = [
    "ConfigStore",
    "ConnectionEvent",
    "DEFAULT_FORCE_DOMAINS",
    "LEGACY_PORTS",
    "MODE_PRESETS",
    "ModePreset",
    "NetworkMode",
    "ProxyTelemetry",
    "RELAY_PORT",
    "RelayConfig",
    "domain_matches",
    "generate_pac",
    "sanitize_domain",
]

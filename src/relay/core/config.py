"""Typed domain configuration, network mode presets, and atomic JSON persistence."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
import os
from pathlib import Path
from typing import Iterable

RELAY_PORT = 11893
TUNNEL_PORT = 11894
GAME_CONTROL_PORT = 11895
GAME_SOCKS_SHIM_PORT = 11896
GAME_TUNNEL_PORT = 11897
LEGACY_PORTS: tuple[int, ...] = (11888, 11889, 11890, 11891, 11892)
ALL_MANAGED_PORTS: tuple[int, ...] = (*LEGACY_PORTS, RELAY_PORT)

DEFAULT_FORCE_DOMAINS: tuple[str, ...] = (
    "discord.com",
    "discordapp.com",
    "discordapp.net",
    "discord.gg",
    "discord.media",
    "roblox.com",
    "rbxcdn.com",
    "wattpad.com",
    "pastebin.com",
)

DEFAULT_GAME_APPS: tuple[str, ...] = (
    "RobloxPlayerBeta.exe",
    "RobloxPlayerLauncher.exe",
    "RobloxStudioBeta.exe",
    "Discord.exe",
)

# Major unblocked high-bandwidth services kept direct in Strict (Tünel) mode
# so video/productivity stays at full native speed while all other domains use MASQUE.
STRICT_DIRECT_DOMAINS: tuple[str, ...] = (
    "google.com",
    "googleapis.com",
    "gstatic.com",
    "googleusercontent.com",
    "googlevideo.com",
    "youtube.com",
    "ytimg.com",
    "ggpht.com",
    "microsoft.com",
    "msftconnecttest.com",
    "office.com",
    "live.com",
    "windowsupdate.com",
    "github.com",
    "githubusercontent.com",
    "apple.com",
    "icloud.com",
    "whatsapp.com",
    "whatsapp.net",
    "chatgpt.com",
    "openai.com",
    "claude.ai",
    "anthropic.com",
    "cloudflare-dns.com",
    "dns.google",
)


class NetworkMode(str, Enum):
    AUTO = "auto"
    STRICT = "strict"
    FAST = "fast"
    COMPAT = "compat"


LEGACY_MODE_ALIASES: dict[str, NetworkMode] = {
    "eduroam": NetworkMode.STRICT,
    "superonline": NetworkMode.FAST,
    "turktelekom": NetworkMode.COMPAT,
}


def parse_network_mode(raw: str | NetworkMode, default: NetworkMode = NetworkMode.AUTO) -> NetworkMode:
    """Parse a NetworkMode from enum, canonical string, or legacy profile alias."""
    if isinstance(raw, NetworkMode):
        return raw
    text = (raw or "").strip().lower()
    if text in LEGACY_MODE_ALIASES:
        return LEGACY_MODE_ALIASES[text]
    try:
        return NetworkMode(text)
    except ValueError:
        return default


@dataclass(frozen=True)
class ModePreset:
    mode: NetworkMode
    label: str
    short_label: str
    delay: float
    race_delay: float
    prefer_tls_records: bool
    race_both: bool = True


MODE_PRESETS: dict[NetworkMode, ModePreset] = {
    NetworkMode.AUTO: ModePreset(
        mode=NetworkMode.AUTO,
        label="Otomatik",
        short_label="Oto",
        delay=0.0,
        race_delay=0.18,
        prefer_tls_records=False,
        race_both=True,
    ),
    NetworkMode.STRICT: ModePreset(
        mode=NetworkMode.STRICT,
        label="Tünel Öncelikli",
        short_label="Tünel",
        delay=0.04,
        race_delay=0.18,
        prefer_tls_records=False,
        race_both=True,
    ),
    NetworkMode.FAST: ModePreset(
        mode=NetworkMode.FAST,
        label="TCP Parçalama",
        short_label="TCP",
        delay=0.0,
        race_delay=0.05,
        prefer_tls_records=False,
        race_both=True,
    ),
    NetworkMode.COMPAT: ModePreset(
        mode=NetworkMode.COMPAT,
        label="TLS Parçalama",
        short_label="TLS",
        delay=0.008,
        race_delay=0.05,
        prefer_tls_records=True,
        race_both=True,
    ),
}


def sanitize_domain(raw: str) -> str:
    """Normalize a user-entered domain or URL to a clean lowercase hostname."""
    text = (raw or "").strip().lower()
    for prefix in ("https://", "http://", "wss://", "ws://"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    text = text.split("/", 1)[0].split("?", 1)[0].split(":", 1)[0].strip().lstrip(".").rstrip(".")
    if not text or any(ch.isspace() for ch in text) or "." not in text:
        return ""
    if any(ch in text for ch in ('"', "'", "\\", ";", "(", ")", "{", "}")):
        return ""
    return text


def domain_matches(host: str, domains: Iterable[str]) -> bool:
    """Return True if host equals or is a subdomain of any entry in domains."""
    clean_host = host.lower().rstrip(".")
    for domain in domains:
        if clean_host == domain or clean_host.endswith("." + domain):
            return True
    return False


def _clean_domain_list(items: Iterable[str]) -> tuple[str, ...]:
    cleaned = {d for d in (sanitize_domain(item) for item in items) if d}
    return tuple(sorted(cleaned))


def sanitize_app_name(raw: str) -> str:
    """Normalize a user-entered process/executable name (e.g. 'RobloxPlayerBeta.exe')."""
    text = (raw or "").strip().strip('"').strip("'")
    if not text:
        return ""
    if "\\" in text or "/" in text:
        text = text.replace("/", "\\").rsplit("\\", 1)[-1]
    if any(ch.isspace() for ch in text):
        return ""
    if any(ch in text for ch in ('"', "'", ";", "(", ")", "{", "}", "|", "<", ">", "*", "?")):
        return ""
    if not text.lower().endswith(".exe"):
        text = text + ".exe"
    return text if len(text) > 4 else ""


def _clean_app_list(items: Iterable[str]) -> tuple[str, ...]:
    seen: dict[str, str] = {}
    for item in items:
        clean = sanitize_app_name(item)
        if clean:
            seen.setdefault(clean.lower(), clean)
    return tuple(sorted(seen.values(), key=str.lower))


@dataclass(frozen=True)
class RelayConfig:
    mode: NetworkMode = NetworkMode.AUTO
    doh_enabled: bool = True
    system_proxy: bool = False
    animations: bool = True
    start_with_windows: bool = False
    bypass_domains: tuple[str, ...] = field(default_factory=tuple)
    force_fragment_domains: tuple[str, ...] = field(default_factory=lambda: tuple(sorted(DEFAULT_FORCE_DOMAINS)))
    game_mode_apps: tuple[str, ...] = field(default_factory=lambda: _clean_app_list(DEFAULT_GAME_APPS))

    @property
    def preset(self) -> ModePreset:
        return MODE_PRESETS[self.mode]

    def with_updates(self, **kwargs) -> RelayConfig:
        mode = parse_network_mode(kwargs["mode"], default=self.mode) if "mode" in kwargs else self.mode
        bypass = (
            _clean_domain_list(kwargs["bypass_domains"])
            if "bypass_domains" in kwargs
            else self.bypass_domains
        )
        force = (
            _clean_domain_list(kwargs["force_fragment_domains"])
            if "force_fragment_domains" in kwargs
            else self.force_fragment_domains
        )
        game_apps = (
            _clean_app_list(kwargs["game_mode_apps"])
            if "game_mode_apps" in kwargs
            else self.game_mode_apps
        )
        return RelayConfig(
            mode=mode,
            doh_enabled=bool(kwargs.get("doh_enabled", self.doh_enabled)),
            system_proxy=bool(kwargs.get("system_proxy", self.system_proxy)),
            animations=bool(kwargs.get("animations", self.animations)),
            start_with_windows=bool(kwargs.get("start_with_windows", self.start_with_windows)),
            bypass_domains=bypass,
            force_fragment_domains=force,
            game_mode_apps=game_apps,
        )

    def to_dict(self) -> dict:
        return {
            "mode": self.mode.value,
            "doh_enabled": self.doh_enabled,
            "system_proxy": self.system_proxy,
            "animations": self.animations,
            "start_with_windows": self.start_with_windows,
            "bypass_domains": list(self.bypass_domains),
            "force_fragment_domains": list(self.force_fragment_domains),
            "game_mode_apps": list(self.game_mode_apps),
        }

    @classmethod
    def from_dict(cls, data: dict) -> RelayConfig:
        if not isinstance(data, dict):
            return cls()
        defaults = cls()
        updates: dict = {}
        if isinstance(data.get("mode"), str):
            updates["mode"] = parse_network_mode(data["mode"], default=defaults.mode)
        if isinstance(data.get("doh_enabled"), bool):
            updates["doh_enabled"] = data["doh_enabled"]
        if isinstance(data.get("system_proxy"), bool):
            updates["system_proxy"] = data["system_proxy"]
        if isinstance(data.get("animations"), bool):
            updates["animations"] = data["animations"]
        if isinstance(data.get("start_with_windows"), bool):
            updates["start_with_windows"] = data["start_with_windows"]
        if isinstance(data.get("bypass_domains"), list):
            updates["bypass_domains"] = data["bypass_domains"]
        if isinstance(data.get("force_fragment_domains"), list):
            updates["force_fragment_domains"] = data["force_fragment_domains"]
        if isinstance(data.get("game_mode_apps"), list):
            updates["game_mode_apps"] = data["game_mode_apps"]
        return defaults.with_updates(**updates)


def default_data_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Relay"


class ConfigStore:
    """Atomic JSON configuration store."""

    def __init__(self, data_dir: Path | None = None):
        self.data_dir = data_dir or default_data_dir()
        self.path = self.data_dir / "config.json"

    def load(self) -> RelayConfig:
        if not self.path.is_file():
            return RelayConfig()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
            return RelayConfig.from_dict(raw)
        except (OSError, ValueError):
            return RelayConfig()

    def save(self, config: RelayConfig) -> RelayConfig:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(config.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        return config

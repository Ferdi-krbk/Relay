"""Unit tests for domain configuration, sanitization, and atomic persistence."""
from relay.core.config import (
    ConfigStore,
    NetworkMode,
    RelayConfig,
    domain_matches,
    sanitize_domain,
)


def test_sanitize_domain_normalizes_urls_and_rejects_injection():
    assert sanitize_domain("https://Discord.COM/channels/@me") == "discord.com"
    assert sanitize_domain("  .Roblox.com:443/  ") == "roblox.com"
    assert sanitize_domain("wss://gateway.discord.gg/?v=10") == "gateway.discord.gg"
    assert sanitize_domain("localhost") == ""
    assert sanitize_domain("not a domain") == ""
    assert sanitize_domain('evil.com"); alert(1)//') == ""


def test_domain_matches_exact_and_subdomains():
    domains = ("discord.com", "roblox.com")
    assert domain_matches("discord.com", domains)
    assert domain_matches("cdn.discord.com.", domains)
    assert not domain_matches("notdiscord.com", domains)


def test_config_store_roundtrip_and_invalid_fallback(tmp_path):
    store = ConfigStore(tmp_path)
    default_cfg = store.load()
    assert default_cfg.mode == NetworkMode.AUTO
    assert default_cfg.doh_enabled is True
    assert default_cfg.system_proxy is False
    assert "discord.com" in default_cfg.force_fragment_domains
    assert default_cfg.bypass_domains == ()

    updated = default_cfg.with_updates(
        mode="compat",
        doh_enabled=False,
        bypass_domains=["https://GarantiBBVA.com.tr/login", "invalid"],
    )
    store.save(updated)
    reloaded = store.load()
    assert reloaded.mode == NetworkMode.COMPAT
    assert reloaded.doh_enabled is False
    assert reloaded.bypass_domains == ("garantibbva.com.tr",)

    # Corrupted file should safely fall back to defaults instead of crashing.
    store.path.write_text("{corrupt json", encoding="utf-8")
    assert store.load().mode == NetworkMode.AUTO

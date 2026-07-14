"""
xray_config.py – Xray JSON configuration generator.

Public API
----------
generate_xray_config(port, node, settings) -> dict
    Build a complete, valid Xray config dict for the given Port + Node pair.

write_config_file(port_id, config) -> Path
    Serialise the config dict to `configs/port_<id>.json` and return the path.

remove_config_file(port_id) -> None
    Delete the config file when the port is stopped / deleted.

Design notes
------------
- Each running port gets its own Xray process with its own config file.
  This lets us run N ports simultaneously with different nodes / protocols.
- Config files live in `backend/configs/` so they are easy to inspect for
  debugging. They are created on start and removed on stop.
- The generator handles the three most common VLESS transport combinations:
    tcp  / none | tls | reality
    ws   / none | tls
    grpc / none | tls
  Additional transports (quic, http/2, kcp) can be wired in the
  `_build_stream_settings()` helper.
- Raw-link fields that carry extra parameters (ws path, host header, sni,
  fp, public-key for Reality, …) are parsed from `node.raw_link` when
  present; otherwise sensible defaults are used.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import models

logger = logging.getLogger(__name__)

# Directory where per-port Xray config files are written
CONFIGS_DIR = Path(__file__).parent / "configs"


# ---------------------------------------------------------------------------
# Raw-link parser (VLESS URI → extra params dict)
# ---------------------------------------------------------------------------

def _parse_vless_link(raw_link: str | None) -> dict[str, str]:
    """
    Parse a VLESS share link and return its query parameters as a flat dict.

    Example link:
        vless://uuid@host:443?type=ws&security=tls&path=%2Fws&host=cdn.example.com&sni=cdn.example.com#Label

    Returns an empty dict when raw_link is None or unparseable.
    """
    if not raw_link:
        return {}
    try:
        parsed = urlparse(raw_link)
        params: dict[str, str] = {}
        for key, values in parse_qs(parsed.query).items():
            params[key] = values[0]   # take first value only
        return params
    except Exception as exc:
        logger.warning("Failed to parse raw_link: %s – %s", raw_link, exc)
        return {}


# ---------------------------------------------------------------------------
# Stream-settings builder
# ---------------------------------------------------------------------------

def _build_stream_settings(node: models.Node, extra: dict[str, str]) -> dict[str, Any]:
    """
    Build the `streamSettings` block for the outbound VLESS entry.

    Parameters come from the structured Node columns with fallback to the
    extra dict parsed from raw_link.
    """
    network  = node.network.lower()
    security = (extra.get("security") or node.security).lower()

    stream: dict[str, Any] = {
        "network": network,
        "security": security,
    }

    # --- TLS ----------------------------------------------------------------
    if security == "tls":
        sni = extra.get("sni") or extra.get("host") or node.address
        stream["tlsSettings"] = {
            "serverName": sni,
            "allowInsecure": False,
            "fingerprint": extra.get("fp", "chrome"),
            "alpn": extra.get("alpn", "h2,http/1.1").split(","),
        }

    # --- Reality ------------------------------------------------------------
    elif security == "reality":
        stream["realitySettings"] = {
            "serverName": extra.get("sni", node.address),
            "fingerprint": extra.get("fp", "chrome"),
            "shortId":     extra.get("sid", ""),
            "publicKey":   extra.get("pbk", ""),
            "spiderX":     extra.get("spx", "/"),
        }

    # --- Network-specific transport -----------------------------------------
    if network == "ws":
        stream["wsSettings"] = {
            "path": extra.get("path", "/"),
            "headers": {"Host": extra.get("host", node.address)},
        }

    elif network == "grpc":
        stream["grpcSettings"] = {
            "serviceName": extra.get("serviceName", extra.get("path", "")),
            "multiMode":   False,
        }

    elif network == "http":
        # HTTP/2 (h2)
        stream["httpSettings"] = {
            "host": [extra.get("host", node.address)],
            "path": extra.get("path", "/"),
        }

    # tcp with no extra settings → no additional block needed

    return stream


# ---------------------------------------------------------------------------
# Routing builder
# ---------------------------------------------------------------------------

def _build_routing(mode: str) -> dict[str, Any]:
    """
    Return a `routing` block for the given routing_mode setting.

    global  → every connection exits through the proxy outbound
    direct  → every connection exits directly (bypass mode)
    rule    → Chinese IPs/domains go direct, geosite:ads blocked,
              everything else proxied (standard split-tunnel)
    """
    if mode == "direct":
        return {
            "domainStrategy": "IPIfNonMatch",
            "rules": [
                {"type": "field", "outboundTag": "direct", "network": "tcp,udp"},
            ],
        }

    if mode == "rule":
        return {
            "domainStrategy": "IPIfNonMatch",
            "rules": [
                {
                    "type": "field",
                    "outboundTag": "block",
                    "domain": ["geosite:category-ads-all"],
                },
                {
                    "type": "field",
                    "outboundTag": "direct",
                    "domain": ["geosite:cn", "geosite:private"],
                },
                {
                    "type": "field",
                    "outboundTag": "direct",
                    "ip": ["geoip:cn", "geoip:private"],
                },
                # everything else → proxy
                {"type": "field", "outboundTag": "proxy", "network": "tcp,udp"},
            ],
        }

    # "global" (default)
    return {
        "domainStrategy": "IPIfNonMatch",
        "rules": [
            {"type": "field", "outboundTag": "proxy", "network": "tcp,udp"},
        ],
    }


# ---------------------------------------------------------------------------
# Main config generator
# ---------------------------------------------------------------------------

def generate_xray_config(
    port: models.Port,
    node: models.Node,
    settings: models.Settings,
) -> dict[str, Any]:
    """
    Build a complete, valid Xray JSON configuration.

    Inbound  : HTTP or SOCKS proxy on 127.0.0.1:<local_port>
    Outbound : VLESS to the assigned node (with mux / sniffing from settings)
    Routing  : Driven by settings.routing_mode
    """
    extra = _parse_vless_link(node.raw_link)

    # --- Inbound ------------------------------------------------------------
    inbound: dict[str, Any] = {
        "tag":      f"inbound-{port.id}",
        "port":     port.local_port,
        "listen":   "127.0.0.1",
        "protocol": port.protocol,    # "socks" or "http"
    }

    if port.protocol == "socks":
        inbound["settings"] = {"auth": "noauth", "udp": True, "ip": "127.0.0.1"}
    else:
        inbound["settings"] = {"allowTransparent": False}

    if settings.enable_sniffing:
        inbound["sniffing"] = {
            "enabled":     True,
            "destOverride": ["http", "tls", "quic"],
            "routeOnly":   False,
        }

    api_port = 10000 + port.id
    inbound_api: dict[str, Any] = {
        "listen": "127.0.0.1",
        "port": api_port,
        "protocol": "dokodemo-door",
        "settings": {
            "address": "127.0.0.1"
        },
        "tag": "api"
    }

    # --- Outbound (VLESS) ---------------------------------------------------
    user: dict[str, Any] = {
        "id":         node.uuid,
        "encryption": "none",   # VLESS always "none"
        "flow":       extra.get("flow", ""),
    }

    if not user["flow"]:
        del user["flow"]    # omit empty flow to keep config clean

    outbound_proxy: dict[str, Any] = {
        "tag":      "proxy",
        "protocol": node.protocol,
        "settings": {
            "vnext": [
                {
                    "address": node.address,
                    "port":    node.port,
                    "users":   [user],
                }
            ]
        },
        "streamSettings": _build_stream_settings(node, extra),
    }

    if settings.enable_mux:
        outbound_proxy["mux"] = {"enabled": True, "concurrency": 8}
    else:
        outbound_proxy["mux"] = {"enabled": False}

    # --- Direct + Block outbounds (always present for routing rules) --------
    outbound_direct: dict[str, Any] = {"tag": "direct",  "protocol": "freedom"}
    outbound_block:  dict[str, Any] = {"tag": "block",   "protocol": "blackhole"}

    # --- Full config --------------------------------------------------------
    routing_config = _build_routing(settings.routing_mode)
    routing_config["rules"].insert(0, {
        "type": "field",
        "inboundTag": ["api"],
        "outboundTag": "api"
    })

    config: dict[str, Any] = {
        "log": {
            "loglevel": "warning",
            "access":   "",
            "error":    "",
        },
        "api": {
            "tag": "api",
            "services": ["StatsService"]
        },
        "stats": {},
        "inbounds":  [inbound, inbound_api],
        "outbounds": [outbound_proxy, outbound_direct, outbound_block],
        "routing":   routing_config,
        "dns": {
            # Use a fast public DoH resolver as a fallback
            "servers": ["1.1.1.1", "8.8.8.8", "localhost"],
        },
        "policy": {
            "levels": {"0": {"handshakeMBS": 4, "connIdle": 300}},
            "system": {"statsOutboundDownlink": True, "statsOutboundUplink": True},
        },
    }

    return config


# ---------------------------------------------------------------------------
# Config file I/O
# ---------------------------------------------------------------------------

def write_config_file(port_id: int, config: dict[str, Any]) -> Path:
    """
    Write `config` as pretty-printed JSON to `configs/port_<port_id>.json`.
    Creates the `configs/` directory if it does not exist.
    Returns the absolute Path to the written file.
    """
    CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    config_path = CONFIGS_DIR / f"port_{port_id}.json"
    config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.debug("Wrote Xray config → %s", config_path)
    return config_path


def remove_config_file(port_id: int) -> None:
    """Delete the config file for `port_id` if it exists."""
    config_path = CONFIGS_DIR / f"port_{port_id}.json"
    if config_path.exists():
        config_path.unlink()
        logger.debug("Removed Xray config ← %s", config_path)

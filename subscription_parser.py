"""
subscription_parser.py – VPN subscription fetcher and VLESS URI parser.

Public API
----------
parse_vless_uri(uri)
    Parse a single ``vless://`` share link into a ``ParsedNode`` dataclass.
    Returns ``None`` when the URI is malformed or uses an unsupported scheme.

decode_subscription_content(raw_content)
    Accept raw bytes/string from a subscription URL (either Base64-encoded
    or plain-text, one URI per line) and return a list of proxy URI strings.

get_subscription_headers()
    Generate a dictionary of HTTP headers that mimic a real Android VPN client
    app request, including randomised hardware identifiers.  Used by
    ``fetch_subscription_raw`` on every outbound request.

async fetch_subscription_raw(url)
    Fetch the raw content of a subscription URL using httpx, sending the
    client-spoofing headers from ``get_subscription_headers()``.
    Raises ``SubscriptionFetchError`` on network or HTTP errors.

async fetch_and_parse(url)
    Convenience wrapper: fetch → decode → parse.
    Returns ``(list[ParsedNode], list[str])`` – parsed nodes and error strings.

VLESS URI anatomy
-----------------
    vless://<uuid>@<host>:<port>?<query>#<fragment(name)>

Supported query parameters
--------------------------
    type / network  → transport layer  (tcp | ws | grpc | http | quic | kcp)
    security        → TLS mode         (none | tls | reality)
    sni             → TLS server-name indication
    fp              → TLS fingerprint  (chrome | firefox | safari | …)
    pbk             → Reality public key
    sid             → Reality short-ID
    spx             → Reality spider-X path
    flow            → XTLS flow        (xtls-rprx-vision | …)
    path            → WebSocket path / gRPC service name
    host            → WebSocket Host header (CDN hostname)
    alpn            → Comma-separated ALPN values
    headerType      → HTTP header type for TCP transport
"""

from __future__ import annotations

import base64
import logging
import random
import re
import string
import uuid
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

import httpx

logger = logging.getLogger(__name__)

# Subscription fetcher timeout (seconds)
_FETCH_TIMEOUT = 20.0

# Schemes we know how to parse (extend later for vmess / trojan / ss)
_SUPPORTED_SCHEMES = {"vless"}

# Regex to do a fast pre-check before full URL parsing
_URI_RE = re.compile(r"^(vless|vmess|trojan|ss)://", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class SubscriptionFetchError(RuntimeError):
    """Raised when the subscription URL cannot be fetched."""


# ---------------------------------------------------------------------------
# ParsedNode – typed result of parsing one URI
# ---------------------------------------------------------------------------

@dataclass
class ParsedNode:
    """
    All fields extracted from a single vless:// share link.

    Fields that map directly to the ``nodes`` DB table are named identically.
    Extra fields (sni, fp, flow, …) are preserved in ``raw_link`` via the
    original URI string and are also consumed by ``xray_config.py`` when it
    parses raw_link at process-launch time.
    """
    name:     str   = ""
    protocol: str   = "vless"
    address:  str   = ""
    port:     int   = 0
    uuid:     str   = ""
    network:  str   = "tcp"
    security: str   = "none"
    raw_link: str   = ""
    # --- Extra params stored for round-trip fidelity in raw_link ------------
    sni:      str   = ""
    fp:       str   = ""
    pbk:      str   = ""
    sid:      str   = ""
    spx:      str   = "/"
    flow:     str   = ""
    path:     str   = "/"
    host:     str   = ""
    alpn:     str   = ""

    def is_valid(self) -> bool:
        """Return True when the minimum required fields are populated."""
        return bool(self.address and self.port and self.uuid)


# ---------------------------------------------------------------------------
# URI parser
# ---------------------------------------------------------------------------

def parse_vless_uri(uri: str) -> Optional[ParsedNode]:
    """
    Parse a ``vless://`` share link into a ``ParsedNode``.

    Returns ``None`` on any parse failure so callers can collect errors
    without raising exceptions in a tight loop.
    """
    uri = uri.strip()
    if not uri:
        return None

    # Fast reject for unsupported schemes
    m = _URI_RE.match(uri)
    if m is None:
        return None
    scheme = m.group(1).lower()
    if scheme not in _SUPPORTED_SCHEMES:
        logger.debug("Skipping unsupported scheme: %s", scheme)
        return None

    try:
        parsed = urlparse(uri)
    except Exception as exc:
        logger.debug("urlparse failed for %r: %s", uri[:80], exc)
        return None

    # --- Mandatory fields ---------------------------------------------------
    uuid    = parsed.username or ""
    address = parsed.hostname or ""
    port    = parsed.port    or 0

    if not (uuid and address and port):
        logger.debug("Missing mandatory field in URI: %r", uri[:80])
        return None

    # --- Query parameters ---------------------------------------------------
    try:
        params: dict[str, str] = {
            k: v[0] for k, v in parse_qs(parsed.query, keep_blank_values=True).items()
        }
    except Exception:
        params = {}

    network  = (params.get("type") or params.get("network") or "tcp").lower()
    security = (params.get("security") or "none").lower()
    sni      = params.get("sni", "")
    fp       = params.get("fp",  "chrome")
    pbk      = params.get("pbk", "")
    sid      = params.get("sid", "")
    spx      = unquote(params.get("spx", "/"))
    flow     = params.get("flow", "")
    path     = unquote(params.get("path", "/"))
    host     = params.get("host", "")
    alpn     = unquote(params.get("alpn", ""))

    # --- Fragment = node name -----------------------------------------------
    name = unquote(parsed.fragment) if parsed.fragment else f"{address}:{port}"

    node = ParsedNode(
        name     = name,
        protocol = scheme,
        address  = address,
        port     = port,
        uuid     = uuid,
        network  = network,
        security = security,
        raw_link = uri,
        sni      = sni,
        fp       = fp,
        pbk      = pbk,
        sid      = sid,
        spx      = spx,
        flow     = flow,
        path     = path,
        host     = host,
        alpn     = alpn,
    )

    if not node.is_valid():
        logger.debug("ParsedNode failed validity check: %r", uri[:80])
        return None

    return node


# ---------------------------------------------------------------------------
# Content decoder (Base64 or plain-text subscription body)
# ---------------------------------------------------------------------------

def _try_base64_decode(content: str) -> Optional[str]:
    """
    Attempt to decode ``content`` as Base64 (standard or URL-safe variant).

    Returns the decoded UTF-8 string, or ``None`` if decoding fails.
    The subscription must contain recognisable proxy URIs after decoding for
    the result to be considered valid.
    """
    # Strip whitespace and padding-equalize
    stripped = content.strip().replace("\n", "").replace("\r", "")

    for variant in (stripped, stripped + "=" * (-len(stripped) % 4)):
        for alphabet in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                decoded = alphabet(variant).decode("utf-8", errors="replace")
                # Sanity check: decoded text should contain at least one proxy URI
                if _URI_RE.search(decoded):
                    return decoded
            except Exception:
                continue

    return None


def decode_subscription_content(raw_content: str) -> list[str]:
    """
    Accept a raw subscription body and return a flat list of proxy URI strings.

    Detection order
    ---------------
    1. Try Base64 (standard + URL-safe, with/without padding).
    2. Fall back to treating the content as plain text, one URI per line.

    Non-proxy lines (comments, blank lines, unsupported schemes) are silently
    filtered out; callers receive only strings that begin with a known scheme.
    """
    # Try Base64 first
    decoded = _try_base64_decode(raw_content)
    text = decoded if decoded is not None else raw_content

    uris: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if _URI_RE.match(line):
            uris.append(line)

    logger.debug(
        "decode_subscription_content: found %d URI(s) (%s)",
        len(uris),
        "base64" if decoded is not None else "plain-text",
    )
    return uris


# ---------------------------------------------------------------------------
# WAF-bypass header generator
# ---------------------------------------------------------------------------

# Pool of plausible Android device models (used for X-Device-Model spoofing)
_ANDROID_MODELS = [
    "Xiaomi 14", "Xiaomi 13 Pro", "Redmi Note 13 Pro",
    "Samsung Galaxy S24", "Samsung Galaxy A55",
    "POCO F6 Pro", "OnePlus 12", "Realme GT 6",
    "Google Pixel 8", "Google Pixel 8a",
    "Motorola Edge 50 Pro",
]

# Pool of matching Android version strings for X-Ver-Os
_ANDROID_VERSIONS = ["13", "14", "14", "14", "15"]

# App version pool – format matching a typical VPN client UA
_APP_VERSIONS = ["2.8.0", "2.9.1", "3.0.2", "3.1.0", "3.2.5"]


def _random_hwid() -> str:
    """
    Generate a random hardware-ID string that resembles an Android device
    fingerprint: 16 uppercase hex characters grouped as 8-4-4.

    Example: ``A3F2B19C-4D7E-8A21``
    """
    hex_chars = string.hexdigits.upper()[:16]   # 0-9 A-F
    seg1 = "".join(random.choices(hex_chars, k=8))
    seg2 = "".join(random.choices(hex_chars, k=4))
    seg3 = "".join(random.choices(hex_chars, k=4))
    return f"{seg1}-{seg2}-{seg3}"


def get_subscription_headers() -> dict[str, str]:
    """
    Build an HTTP header dict that mimics an Android VPN client application.

    Every call generates fresh random values for the device-specific fields
    (model, hardware ID, OS version) so successive requests don't share a
    static fingerprint that WAFs can block by pattern.

    Returned headers
    ----------------
    User-Agent        – ``<AppName>/<version> (Android <os_ver>; <model>)``
    X-App-Version     – app semantic version extracted from the UA string
    X-Device-Locale   – always ``RU`` (matches the target subscription locale)
    X-Device-Os       – ``Android <version>``
    X-Device-Model    – randomly chosen Android device model name
    X-Hwid            – randomly generated hardware-ID token
    X-Ver-Os          – bare Android version string
    Connection        – ``Keep-Alive``
    Accept-Encoding   – ``gzip, deflate``
    Accept-Language   – ``ru-RU,en,*``
    """
    model_name = random.choice(_ANDROID_MODELS)
    ver_os     = random.choice(_ANDROID_VERSIONS)
    app_ver    = random.choice(_APP_VERSIONS)
    hwid       = _random_hwid()

    device_os  = f"Android {ver_os}"
    ua         = f"VPNApp/{app_ver} (Android {ver_os}; {model_name})"

    return {
        "User-Agent":       ua,
        "X-App-Version":    ua.split("/")[1].split()[0] if "/" in ua else "2.8.0",
        "X-Device-Locale":  "RU",
        "X-Device-Os":      device_os,
        "X-Device-Model":   model_name,
        "X-Hwid":           hwid,
        "X-Ver-Os":         ver_os,
        "Connection":       "Keep-Alive",
        "Accept-Encoding":  "gzip, deflate",
        "Accept-Language":  "ru-RU,en,*",
    }


# ---------------------------------------------------------------------------
# Async HTTP fetcher
# ---------------------------------------------------------------------------

async def fetch_subscription_raw(url: str) -> str:
    """
    Fetch the raw body of a subscription URL.

    Sends ``get_subscription_headers()`` on every request to spoof a real
    Android VPN client and bypass basic WAF / hotlink rules used by many
    subscription providers.  Raises ``SubscriptionFetchError`` on failure.
    """
    headers = get_subscription_headers()
    logger.debug(
        "fetch_subscription_raw: ua=%r hwid=%r",
        headers["User-Agent"],
        headers["X-Hwid"],
    )
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=_FETCH_TIMEOUT,
        ) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.text
    except httpx.HTTPStatusError as exc:
        raise SubscriptionFetchError(
            f"HTTP {exc.response.status_code} from {url}"
        ) from exc
    except httpx.RequestError as exc:
        raise SubscriptionFetchError(
            f"Network error fetching {url}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# High-level convenience function
# ---------------------------------------------------------------------------

async def fetch_and_parse(url: str) -> tuple[list[ParsedNode], list[str]]:
    """
    Fetch a subscription URL and parse every vless:// link inside it.

    Returns
    -------
    nodes  : list[ParsedNode]  – successfully parsed nodes
    errors : list[str]         – human-readable messages for failed lines
    """
    raw = await fetch_subscription_raw(url)
    uris = decode_subscription_content(raw)

    nodes:  list[ParsedNode] = []
    errors: list[str]        = []

    for uri in uris:
        node = parse_vless_uri(uri)
        if node is not None:
            nodes.append(node)
        else:
            errors.append(f"Failed to parse: {uri[:100]}")

    logger.info(
        "fetch_and_parse(%s): %d parsed, %d errors", url, len(nodes), len(errors)
    )
    return nodes, errors

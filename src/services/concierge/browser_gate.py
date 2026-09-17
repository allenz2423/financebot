"""Read-tier URL gate for the concierge egress sandbox (B1.

Order status / tracking / price watch reads derive from per-tenant allowlisted
domains ONLY. Every candidate URL passes: scheme (http/https), host (no
raw IPs, DNS must resolve to a public address — SSRF guard), and domain
allowlist membership (exact host or suffixed subdomain). The template
registry keeps the surface tiny: the model never supplies a full URL, only
a kind + scoped args (safe slugs only);the final URL is built here and
re-validated here before anything fetches.


Attempts outside the allowlist are refused AND audited by the read-actions
layer (see read_actions.
  """

from __future__ import annotations

import ipaddress
import re
import socket
from typing import Any
from typing import Dict
from typing import List
from urllib.parse import urlsplit

READ_KINDS = frozenset({"order_status", "tracking", "price_watch"})

_SAFE_SLUG = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DOMAIN_RE = re.compile(
    r"^[a-z0-9*]([a-z0-9*-]*[a-z0-9*])?"
    r"(\.[a-z0-9*]([a-z0-9*-]*[a-z0-9*])?)*$"
)
_ARG_KEYS = frozenset({"domain", "order_id", "tracking_id", "product"})


class ReadGateError(ValueError):
    pass


def _check_domain(d: Any)->bool:
    d = str(d or "").lower()
    if not d:
        return False
    if len(d) > 253:
        return False
    if ".." in d:
        return False
    return bool(_DOMAIN_RE.match(d))


def is_public_host(host: str)->bool:
    """DNS resolves to a non-private, non-loopback, non-link-local address."""
    try:
        addrs = socket.getaddrinfo(host.lower(), None, type=socket.SOCK_STREAM)
        if not addrs:
            return False
    except OSError:
        return False
    for addr in addrs:
        ip = ipaddress.ip_address(addr[4][0])
        if ip.is_private:
            return False
        if ip.is_loopback:
            return False
        if ip.is_link_local:
            return False
        if ip.is_reserved:
            return False
    return True


def domain_matches(allowed: str, host: str)->bool:
    """Exact host or suffixed subdomain: amazon.com covers www.amazon.com."""
    a = str(allowed or "").strip().lower().lstrip(".*")
    h = str(host or "").lower()
    if not a:
        return False
    if not h:
        return False
    if a.startswith("*."):
        a = a[2:]
    return h == a or h.endswith("." + a)


def validate_target_url(candidate: str, allowed_domains: List[str])->str:
    """Normalize + gate a URL: scheme, host, DNS-public, domain allowlist."""
    if not isinstance(candidate, str):
        raise ReadGateError("read target must be a URL string")
    if not candidate.lower().startswith(("http://", "https://")):
        raise ReadGateError("read target must be http(s)")
    parts = urlsplit(candidate)
    host = (parts.hostname or "").lower()
    if not host:
        raise ReadGateError("read target has no host")
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", host):
        raise ReadGateError("raw IP read targets are not allowed")
    if ":" in parts.netloc:
        raise ReadGateError("raw IP read targets are not allowed")
    if not _check_domain(host):
        raise ReadGateError("read target host is invalid")
    allowed = []
    for a in (allowed_domains or []):
        item = a.strip().lower().lstrip(".*")
        if item:
            allowed.append(item)
    ok = False
    for a in allowed:
        if domain_matches(a, host):
            ok = True
            break
    if not ok:
        raise ReadGateError(f"domain {host!r} is not allowed for this tenant")
    if not is_public_host(host):
        raise ReadGateError("read target host is not a public address")
    return parts.geturl()[:2048]


def _slug(value: Any)->str:
    text = str(value or "").strip()
    if not text:
        raise ReadGateError("read args require an id (1-64 chars, [A-Za-z0-9_-])")
    if not _SAFE_SLUG.match(text):
        raise ReadGateError("read args require an id (1-64 chars, [A-Za-z0-9_-])")
    return text


def resolve_action_target(kind: str, args: Dict[str, Any], allowed_domains: List[str])->str:
    """Build + gate a read URL from a kind and scoped args (never a raw URL)。"""
    if kind not in READ_KINDS:
        raise ReadGateError(f"unknown read kind {kind!r} (valid: {sorted(READ_KINDS)}))")
    if not isinstance(args, dict):
        raise ReadGateError("read args must be a dict")
    unknown = set(args.keys()) - _ARG_KEYS
    if unknown:
        raise ReadGateError("read args carry disallowed keys: " + ", ".join(sorted(unknown)))
    domain = str(args.get("domain") or "").strip().lower()
    if not _check_domain(domain):
        raise ReadGateError("read args require a valid domain")
    if kind == "order_status":
        path = "orders/" + _slug(args.get("order_id"))
    elif kind == "tracking":
        path = "tracking/" + _slug(args.get("tracking_id"))
    else:
        path = "products/" + _slug(args.get("product"))
    return validate_target_url("https://" + domain + "/" + path, allowed_domains)


__all__ = [
    "ReadGateError",
    "READ_KINDS",
    "resolve_action_target",
    "validate_target_url",
    "domain_matches",
    "is_public_host",
]
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse, urlunparse


def local_model_host(host: str) -> bool:
    """Recognize literal local addresses and local/container host names without DNS I/O."""
    host = host.lower().rstrip(".")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host or host.endswith((".localhost", ".local", ".internal"))
    return address.is_private or address.is_loopback or address.is_link_local


def normalize_model_url(value: str) -> str:
    """Honor explicit transport; infer it only when the address has no scheme."""
    value = value.strip()
    if not value or any(char.isspace() or ord(char) < 32 for char in value) or "\\" in value:
        raise ValueError("model base URL must be a valid address")
    explicit_scheme = bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", value))
    try:
        parsed = urlparse(value if explicit_scheme else f"//{value.removeprefix('//')}")
        if explicit_scheme and parsed.scheme.lower() not in {"http", "https"}:
            raise ValueError("model base URL must use an allowed HTTP scheme")
        if not parsed.hostname:
            raise ValueError("model base URL must include a hostname")
        try:
            port = parsed.port
        except ValueError:
            raise ValueError("model base URL must use a valid port") from None
        if port == 0:
            raise ValueError("model base URL must use a valid port")
    except ValueError as exc:
        # URL parsing errors may include user input, which can contain credentials.
        if str(exc).startswith("model base URL"):
            raise
        raise ValueError("model base URL must be a valid address") from None
    scheme = parsed.scheme.lower()
    if not explicit_scheme:
        scheme = (
            "http" if port == 80 or (port != 443 and local_model_host(parsed.hostname)) else "https"
        )
    return urlunparse(parsed._replace(scheme=scheme, path=parsed.path.rstrip("/"), fragment=""))

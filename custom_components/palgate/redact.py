"""Debug-log redaction for Palgate.

Sensitive values are replaced by a short keyed tag, e.g. <redacted:3f9a1c>.
The same value always gets the same tag on this installation (the key is the
HA instance UUID: stable across restarts and upgrades), so logs of several
gates can be correlated, but the tag cannot be reversed or recomputed without
the key. Secrets get a plain <redacted>, with no tag.

Developers can switch redaction off by explicitly setting the logger
"custom_components.palgate.unredacted" to debug in the HA logger config.
"""

from __future__ import annotations

import hmac
import logging
import os
import re
from typing import Any, Callable

from homeassistant.helpers import instance_id

_LOGGER = logging.getLogger(__name__)

# Used as a switch only. Its own level is tested (not the inherited one), so
# enabling debug for the whole integration does not disable redaction.
_RAW_LOGGER = logging.getLogger(f"{__package__}.unredacted")
_warned = False

_REDACT_KEY: bytes = os.urandom(16)  # fallback; replaced by init_redaction()
_SECRET_KEYS = {"key", "token"}
_TAGGED_KEYS = re.compile(
    r"_?id|user_?id|sn|address|addresscoord|phone|pn|firstname|lastname|username|"
    r"(custom)?name\d*",
    re.IGNORECASE,
)
_URL_IDS = re.compile(r"(/(?:device|init)/|[?&](?:pn|id)=)([^/?&]+)")


def _raw_enabled() -> bool:
    return _RAW_LOGGER.level == logging.DEBUG


async def init_redaction(hass) -> None:
    """Key the redaction tags with this installation's stable HA instance UUID."""
    global _REDACT_KEY, _warned
    _REDACT_KEY = (await instance_id.async_get(hass)).encode()
    if _raw_enabled() and not _warned:
        _warned = True
        _LOGGER.warning("Unredacted debug logging is enabled. Do not share these logs")


def redact_tag(value: Any) -> str:
    """Return <redacted:xxxxxx>, keeping any ':N' output suffix visible."""
    base, num = re.fullmatch(r"(.*?)(:\d+)?", str(value), re.S).groups()
    digest = hmac.new(_REDACT_KEY, base.encode(), "sha256").hexdigest()[:6]
    return f"<redacted:{digest}>{num or ''}"


def redact(obj: Any) -> Any:
    """Return a copy of a JSON-like object that is safe to log."""
    if isinstance(obj, dict):
        return {
            k: "<redacted>" if k in _SECRET_KEYS
            else redact_tag(v) if _TAGGED_KEYS.fullmatch(k) and v not in (None, "")
            else redact(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(i) for i in obj]
    return obj


def redact_url(url: Any) -> str:
    """Tag device ids, phone numbers and linking codes inside a URL."""
    return _URL_IDS.sub(lambda m: m[1] + redact_tag(m[2]), str(url))


class Redacted:
    """Log argument that is redacted only if the record is actually emitted."""

    def __init__(self, obj: Any, fn: Callable[[Any], Any] = redact) -> None:
        self._obj, self._fn = obj, fn

    def __str__(self) -> str:
        return str(self._obj if _raw_enabled() else self._fn(self._obj))

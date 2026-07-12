"""Memory-only known-secret redaction at durable and transport boundaries."""

from __future__ import annotations

import base64
import json
import shlex
from threading import RLock
from typing import Any, Iterable, Mapping
from urllib.parse import quote, quote_plus


REDACTED = "[REDACTED]"
_lock = RLock()
_known_secrets: set[str] = set()


def register_known_secrets(secrets: Iterable[str]) -> None:
    """Register non-empty secrets in process memory before evidence transport."""

    values = {secret for secret in secrets if isinstance(secret, str) and secret}
    with _lock:
        _known_secrets.update(values)


def redact(value: Any) -> Any:
    """Return a recursively scrubbed copy without mutating caller-owned input."""

    with _lock:
        variants = tuple(sorted({variant for secret in _known_secrets for variant in _variants(secret)}, key=len, reverse=True))
    return _redact_value(value, variants)


def redact_bytes(value: bytes) -> bytes:
    """Scrub raw transport payloads, including a secret split across chunks.

    Evidence is opaque to the controller, so it is scrubbed as bytes at its
    only browser-facing boundary.  Joining the supplied payload before
    replacement makes a boundary between network chunks irrelevant.
    """

    with _lock:
        variants = tuple(
            variant.encode("utf-8")
            for variant in sorted({variant for secret in _known_secrets for variant in _variants(secret)}, key=len, reverse=True)
        )
    result = value
    for variant in variants:
        result = result.replace(variant, REDACTED.encode("utf-8"))
    return result


def redact_chunks(chunks: Iterable[bytes]) -> bytes:
    """Return one safe payload even when a canary spans input chunks."""

    return redact_bytes(b"".join(chunks))


def _redact_value(value: Any, variants: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        result = value
        for variant in variants:
            result = result.replace(variant, REDACTED)
        return result
    if isinstance(value, Mapping):
        return {str(key): _redact_value(item, variants) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item, variants) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item, variants) for item in value]
    return value


def _variants(secret: str) -> set[str]:
    raw = secret.encode("utf-8")
    escaped = json.dumps(secret)[1:-1]
    base64_standard = base64.b64encode(raw).decode("ascii")
    base64_urlsafe = base64.urlsafe_b64encode(raw).decode("ascii")
    return {
        secret,
        escaped,
        quote(secret, safe=""),
        quote_plus(secret, safe=""),
        shlex.quote(secret),
        base64_standard,
        base64_standard.rstrip("="),
        base64_urlsafe,
        base64_urlsafe.rstrip("="),
    }

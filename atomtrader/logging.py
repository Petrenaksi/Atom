"""Structured logging with secret redaction.

Every log event passes through ``redact_secrets`` which:
  * replaces any value registered via ``register_secret`` (exact substring match),
  * scrubs strings that look like private keys or seed phrases,
  * masks values under sensitive key names (``private_key``, ``api_key``, ...).

Redaction is defense in depth: secrets should never be passed to loggers at all.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping
from typing import Any

import structlog

REDACTED = "[REDACTED]"

_registered: set[str] = set()

_SENSITIVE_KEYS = re.compile(
    r"(private[_-]?key|secret|seed|mnemonic|passphrase|password|api[_-]?key|auth|token_secret|keypair)",
    re.IGNORECASE,
)
_PATTERNS = (
    # EVM private key: 64 hex chars, optionally 0x-prefixed. (Addresses are 40 hex: untouched.)
    re.compile(r"\b(?:0x)?[0-9a-fA-F]{64}\b"),
    # Solana secret key in base58 (64 bytes -> ~87-88 chars). Pubkeys are 32-44 chars: untouched.
    re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{80,90}\b"),
    # Solana secret key as JSON byte array.
    re.compile(r"\[\s*(?:\d{1,3}\s*,\s*){63}\d{1,3}\s*\]"),
)
# A value consisting *only* of 12-24 lowercase words looks like a bare BIP-39 mnemonic.
# (Matching mnemonic-like runs inside prose would redact ordinary decision rationales.)
_MNEMONIC = re.compile(r"^\s*(?:[a-z]{3,8}\s+){11,23}[a-z]{3,8}\s*$")
# Transaction signatures/hashes share the shape of private keys. Values under these keys
# skip the *pattern* scrub (still exact-match scrubbed against registered secrets).
_PATTERN_EXEMPT_KEYS = frozenset({"tx_ref", "tx_hash", "tx_signature", "block_hash"})


def register_secret(value: str | None) -> None:
    """Register a secret value so any accidental appearance in logs is masked."""
    if value and len(value) >= 6:
        _registered.add(value)


def _scrub(text: str, patterns: bool = True) -> str:
    for secret in _registered:
        if secret in text:
            text = text.replace(secret, REDACTED)
    if patterns:
        if _MNEMONIC.match(text):
            return REDACTED
        for pattern in _PATTERNS:
            text = pattern.sub(REDACTED, text)
    return text


def _scrub_value(value: Any, patterns: bool = True) -> Any:
    if isinstance(value, str):
        return _scrub(value, patterns)
    if isinstance(value, Mapping):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and _SENSITIVE_KEYS.search(k):
                out[k] = REDACTED
            else:
                out[k] = _scrub_value(v, patterns and k not in _PATTERN_EXEMPT_KEYS)
        return out
    if isinstance(value, (list, tuple)):
        return type(value)(_scrub_value(v, patterns) for v in value)
    return value


def redact_secrets(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    return _scrub_value(event_dict)  # type: ignore[return-value]


def configure_logging(json: bool = True, level: int = logging.INFO) -> None:
    renderer: Any = (
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            redact_secrets,  # last before rendering: also covers formatted tracebacks
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str = "atom") -> Any:
    return structlog.get_logger(name)

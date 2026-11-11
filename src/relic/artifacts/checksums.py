"""Checksum computation and verification for Relic artifacts.

This module provides cryptographic checksum utilities for artifact integrity
verification. All artifacts MUST have a checksum computed from their content
to ensure tamper detection.

Privacy: This module works with content hashes only - no raw content is ever
stored or transmitted.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def compute_checksum(content: dict[str, Any] | list | str) -> str:
    """Compute SHA-256 checksum of content.

    Args:
        content: Dictionary, list, or string to hash

    Returns:
        SHA-256 hex digest (64 characters)
    """
    if isinstance(content, dict):
        content_str = json.dumps(content, sort_keys=True, default=_json_serializer)
    elif isinstance(content, list):
        content_str = json.dumps(content, sort_keys=True, default=_json_serializer)
    else:
        content_str = str(content)

    return hashlib.sha256(content_str.encode("utf-8")).hexdigest()


def _json_serializer(obj: Any) -> str:
    """Custom JSON serializer for non-serializable objects."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "__str__"):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def verify_checksum(content: dict[str, Any] | list | str, expected_checksum: str) -> bool:
    """Verify content matches expected checksum.

    Args:
        content: Content to verify
        expected_checksum: Expected SHA-256 hex digest

    Returns:
        True if checksums match, False otherwise
    """
    if len(expected_checksum) != 64:
        return False

    try:
        actual = compute_checksum(content)
        return actual == expected_checksum
    except Exception:
        return False



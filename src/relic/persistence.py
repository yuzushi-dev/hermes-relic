"""Privacy classification levels shared across Relic."""

from __future__ import annotations

import logging
from enum import Enum

try:
    import structlog
    logger = structlog.get_logger(__name__)
except ImportError:
    logger = logging.getLogger(__name__)


class PrivacyLevel(Enum):
    """Privacy classification levels for memory blocks.

    S0: Hard violation - content must be rejected
    S1: Quarantine - content requires review before use
    S2: Warning - overpersonalization detected
    """
    S0_HARD_VIOLATION = "s0"
    S1_QUARANTINE = "s1"
    S2_WARNING = "s2"
    SAFE = "safe"

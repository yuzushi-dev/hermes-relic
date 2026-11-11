"""Tests for artifact checksum computation and verification.

These tests verify that:
- Checksums are correctly computed using SHA-256
- Checksum verification works correctly
- Content is never stored raw, only as hashes
"""

from __future__ import annotations

from relic.artifacts.checksums import compute_checksum, verify_checksum


class TestComputeChecksum:
    """Tests for compute_checksum function."""

    def test_checksum_of_dict(self):
        """Test checksum computation for dictionary content."""
        content = {"key": "value", "number": 42}
        checksum = compute_checksum(content)
        assert len(checksum) == 64
        assert checksum.isalnum()

    def test_checksum_of_list(self):
        """Test checksum computation for list content."""
        content = ["item1", "item2", "item3"]
        checksum = compute_checksum(content)
        assert len(checksum) == 64

    def test_checksum_of_string(self):
        """Test checksum computation for string content."""
        content = "Hello, World!"
        checksum = compute_checksum(content)
        assert len(checksum) == 64

    def test_checksum_deterministic(self):
        """Test checksum is deterministic for same content."""
        content = {"key": "value"}
        checksum1 = compute_checksum(content)
        checksum2 = compute_checksum(content)
        assert checksum1 == checksum2

    def test_checksum_different_for_different_content(self):
        """Test different content produces different checksums."""
        content1 = {"key": "value1"}
        content2 = {"key": "value2"}
        checksum1 = compute_checksum(content1)
        checksum2 = compute_checksum(content2)
        assert checksum1 != checksum2

    def test_checksum_sorted_keys(self):
        """Test checksum is independent of key order."""
        content1 = {"a": 1, "b": 2}
        content2 = {"b": 2, "a": 1}
        checksum1 = compute_checksum(content1)
        checksum2 = compute_checksum(content2)
        assert checksum1 == checksum2


class TestVerifyChecksum:
    """Tests for verify_checksum function."""

    def test_verify_checksum_valid(self):
        """Test checksum verification with valid checksum."""
        content = {"key": "value"}
        checksum = compute_checksum(content)
        assert verify_checksum(content, checksum) is True

    def test_verify_checksum_invalid(self):
        """Test checksum verification with invalid checksum."""
        content = {"key": "value"}
        wrong_checksum = "a" * 64
        assert verify_checksum(content, wrong_checksum) is False

    def test_verify_checksum_invalid_length(self):
        """Test checksum verification rejects invalid length."""
        content = {"key": "value"}
        short_checksum = "abc"
        assert verify_checksum(content, short_checksum) is False

    def test_verify_checksum_tampered_content(self):
        """Test checksum verification detects tampering."""
        content = {"key": "value"}
        checksum = compute_checksum(content)
        tampered = {"key": "modified"}
        assert verify_checksum(tampered, checksum) is False


class TestPrivacyGuarantees:
    """Tests for privacy guarantees in checksum module."""

    def test_no_raw_content_in_checksum(self):
        """Test that compute_checksum doesn't expose raw content."""
        sensitive_content = {
            "user_id": "user123",
            "raw_chat": "User said: I have cancer and need medical help",
            "private_info": "SSN: 123-45-6789",
        }
        # Compute checksum - should not raise
        checksum = compute_checksum(sensitive_content)
        # Checksum should be just hex characters
        assert checksum.isalnum()
        assert len(checksum) == 64
        # Original content should not appear in checksum
        assert "cancer" not in checksum
        assert "SSN" not in checksum
        assert "user123" not in checksum

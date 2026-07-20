"""Tests for auth/passwords.py — bcrypt hashing + mock-mode bypass.

Covers:
    - PasswordHasher.hash() returns a $2b$ bcrypt hash
    - PasswordHasher.verify() with correct/wrong password in real mode
    - PasswordHasher.verify() always returns True in mock mode
    - PasswordHasher.verify() with None hash returns False
    - PasswordHasher.skip_verification property
"""

from __future__ import annotations

import pytest

from audio_graphy.auth.passwords import PasswordHasher


@pytest.mark.unit
class TestPasswordHasher:
    """Unit tests for PasswordHasher."""

    def test_hash_returns_bcrypt_format(self) -> None:
        """hash() should return a bcrypt hash starting with $2b$."""
        hasher = PasswordHasher(adapter_mode="real", bcrypt_rounds=4)
        result = hasher.hash("mypassword123")
        assert isinstance(result, str)
        assert result.startswith("$2b$")
        assert len(result) >= 59

    def test_verify_correct_password_real_mode(self) -> None:
        """verify() with correct password should return True in real mode."""
        hasher = PasswordHasher(adapter_mode="real", bcrypt_rounds=4)
        password = "testPassword456"
        hash_val = hasher.hash(password)
        assert hasher.verify(password, hash_val) is True

    def test_verify_wrong_password_real_mode(self) -> None:
        """verify() with wrong password should return False in real mode."""
        hasher = PasswordHasher(adapter_mode="real", bcrypt_rounds=4)
        hash_val = hasher.hash("correct_password")
        assert hasher.verify("wrong_password", hash_val) is False

    def test_verify_mock_mode_always_true(self) -> None:
        """verify() should always return True in mock mode, regardless of password."""
        hasher = PasswordHasher(adapter_mode="mock")
        assert hasher.verify("anything", "$2b$12$somehash") is True
        assert hasher.verify("wrong", "$2b$12$otherhash") is True
        assert hasher.verify("", "$2b$12$third") is True

    def test_verify_none_hash_real_mode(self) -> None:
        """verify() with None hash should return False in real mode."""
        hasher = PasswordHasher(adapter_mode="real", bcrypt_rounds=4)
        assert hasher.verify("password", None) is False

    def test_verify_empty_hash_real_mode(self) -> None:
        """verify() with empty string hash should return False in real mode."""
        hasher = PasswordHasher(adapter_mode="real", bcrypt_rounds=4)
        assert hasher.verify("password", "") is False

    def test_skip_verification_mock_mode(self) -> None:
        """skip_verification property should return True for mock mode."""
        hasher = PasswordHasher(adapter_mode="mock")
        assert hasher.skip_verification is True

    def test_skip_verification_real_mode(self) -> None:
        """skip_verification property should return False for real mode."""
        hasher = PasswordHasher(adapter_mode="real", bcrypt_rounds=4)
        assert hasher.skip_verification is False

    def test_hash_then_verify_roundtrip(self) -> None:
        """Full roundtrip: hash then verify should succeed."""
        hasher = PasswordHasher(adapter_mode="real", bcrypt_rounds=4)
        password = "roundtrip_test_Pass!"
        hash_val = hasher.hash(password)
        assert hasher.verify(password, hash_val) is True
        assert hasher.verify("different", hash_val) is False

    def test_different_passwords_different_hashes(self) -> None:
        """Same hasher should produce different hashes for different passwords."""
        hasher = PasswordHasher(adapter_mode="real", bcrypt_rounds=4)
        hash1 = hasher.hash("password1")
        hash2 = hasher.hash("password2")
        assert hash1 != hash2

    def test_default_adapter_mode_is_mock(self) -> None:
        """Default adapter_mode should be 'mock'."""
        hasher = PasswordHasher()
        assert hasher.skip_verification is True

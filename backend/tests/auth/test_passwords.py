"""Tests for auth/passwords.py — bcrypt hashing.

Covers:
    - PasswordHasher.hash() returns a $2b$ bcrypt hash
    - PasswordHasher.verify() with correct/wrong password
    - PasswordHasher.verify() with None/empty/malformed hash returns False
    - verify() has no bypass: no constructor argument can make a wrong
      password succeed (regression guard for the removed mock-mode bypass)
"""

from __future__ import annotations

import pytest

from audio_graphy.auth.passwords import PasswordHasher


@pytest.mark.unit
class TestPasswordHasher:
    """Unit tests for PasswordHasher."""

    def test_hash_returns_bcrypt_format(self) -> None:
        """hash() should return a bcrypt hash starting with $2b$."""
        hasher = PasswordHasher(bcrypt_rounds=4)
        result = hasher.hash("mypassword123")
        assert isinstance(result, str)
        assert result.startswith("$2b$")
        assert len(result) >= 59

    def test_verify_correct_password(self) -> None:
        """verify() with the correct password should return True."""
        hasher = PasswordHasher(bcrypt_rounds=4)
        password = "testPassword456"
        hash_val = hasher.hash(password)
        assert hasher.verify(password, hash_val) is True

    def test_verify_wrong_password(self) -> None:
        """verify() with a wrong password should return False."""
        hasher = PasswordHasher(bcrypt_rounds=4)
        hash_val = hasher.hash("correct_password")
        assert hasher.verify("wrong_password", hash_val) is False

    def test_verify_none_hash(self) -> None:
        """verify() with None hash should return False."""
        hasher = PasswordHasher(bcrypt_rounds=4)
        assert hasher.verify("password", None) is False

    def test_verify_empty_hash(self) -> None:
        """verify() with an empty string hash should return False."""
        hasher = PasswordHasher(bcrypt_rounds=4)
        assert hasher.verify("password", "") is False

    def test_verify_malformed_hash_is_rejected_not_raised(self) -> None:
        """A stored value that is not a bcrypt digest fails closed."""
        hasher = PasswordHasher(bcrypt_rounds=4)
        # "mock" was the placeholder seeded by older fixtures.
        assert hasher.verify("anything", "mock") is False
        assert hasher.verify("anything", "not-a-bcrypt-hash") is False

    def test_default_hasher_still_verifies(self) -> None:
        """The default constructor must not skip verification.

        Regression guard: verification used to be short-circuited whenever
        ``adapter_mode == "mock"``, which was also the default — so a
        default-constructed hasher accepted every password.
        """
        hasher = PasswordHasher(bcrypt_rounds=4)
        hash_val = hasher.hash("real_password")
        assert hasher.verify("real_password", hash_val) is True
        assert hasher.verify("wrong_password", hash_val) is False

    def test_hash_then_verify_roundtrip(self) -> None:
        """Full roundtrip: hash then verify should succeed."""
        hasher = PasswordHasher(bcrypt_rounds=4)
        password = "roundtrip_test_Pass!"
        hash_val = hasher.hash(password)
        assert hasher.verify(password, hash_val) is True
        assert hasher.verify("different", hash_val) is False

    def test_different_passwords_different_hashes(self) -> None:
        """Same hasher should produce different hashes for different passwords."""
        hasher = PasswordHasher(bcrypt_rounds=4)
        hash1 = hasher.hash("password1")
        hash2 = hasher.hash("password2")
        assert hash1 != hash2

    def test_secrets_longer_than_bcrypt_limit_roundtrip(self) -> None:
        """Inputs beyond bcrypt's 72-byte limit truncate consistently."""
        hasher = PasswordHasher(bcrypt_rounds=4)
        password = "x" * 100
        hash_val = hasher.hash(password)
        assert hasher.verify(password, hash_val) is True

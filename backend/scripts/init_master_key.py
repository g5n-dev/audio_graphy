"""Create or validate the Docker Compose audio master-key file.

The key value is never printed. The dedicated Compose volume is mounted
read-only by the backend after this one-shot initializer completes.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import logging
import os
import stat
from pathlib import Path

logger = logging.getLogger(__name__)


def _decode_key(value: bytes) -> bytes | None:
    stripped = value.strip()
    if len(stripped) == 32:
        return stripped
    try:
        decoded = base64.urlsafe_b64decode(stripped)
    except (ValueError, binascii.Error):
        return None
    return decoded if len(decoded) == 32 else None


def _write_all(descriptor: int, value: bytes) -> None:
    pending = memoryview(value)
    while pending:
        written = os.write(descriptor, pending)
        if written <= 0:
            raise OSError("failed to write master-key state")
        pending = pending[written:]


def _state_has_ciphertext(state_dir: Path) -> bool:
    return next(state_dir.rglob("*.enc"), None) is not None


def initialize_key(
    path: Path,
    *,
    owner_uid: int,
    owner_gid: int,
    state_dir: Path | None = None,
) -> None:
    """Create/validate a key and reject silent replacement of an old key."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError(f"refusing symlink master-key path: {path}")

    fingerprint_path: Path | None = None
    if state_dir is not None:
        state_dir.mkdir(parents=True, exist_ok=True)
        fingerprint_path = state_dir / ".audio_master_key.sha256"
        if not path.exists() and (fingerprint_path.exists() or _state_has_ciphertext(state_dir)):
            raise RuntimeError(
                "master key is missing while persistent encrypted state exists; "
                "restore the original key"
            )

    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except FileExistsError:
        existing_stat = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(existing_stat.st_mode):
            raise RuntimeError(f"master-key path is not a regular file: {path}") from None
        if _decode_key(path.read_bytes()) is None:
            raise RuntimeError(f"existing master-key file is malformed: {path}") from None
    else:
        try:
            key = base64.urlsafe_b64encode(os.urandom(32))
            _write_all(descriptor, key)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    os.chmod(path, 0o600, follow_symlinks=False)
    if os.geteuid() == 0:
        os.chown(path, owner_uid, owner_gid, follow_symlinks=False)

    decoded_key = _decode_key(path.read_bytes())
    if decoded_key is None:
        raise RuntimeError(f"master-key file became malformed: {path}")
    if fingerprint_path is not None:
        fingerprint = hashlib.sha256(decoded_key).hexdigest().encode("ascii")
        try:
            descriptor = os.open(
                fingerprint_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            fingerprint_stat = fingerprint_path.stat(follow_symlinks=False)
            if (
                fingerprint_path.is_symlink()
                or not stat.S_ISREG(fingerprint_stat.st_mode)
                or fingerprint_stat.st_size > 128
            ):
                raise RuntimeError(
                    f"invalid master-key fingerprint file: {fingerprint_path}"
                ) from None
            existing = fingerprint_path.read_bytes().strip()
            if not hmac.compare_digest(existing, fingerprint):
                raise RuntimeError(
                    "master key does not match the persistent-state fingerprint"
                ) from None
        else:
            try:
                _write_all(descriptor, fingerprint)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        os.chmod(fingerprint_path, 0o600, follow_symlinks=False)
        if os.geteuid() == 0:
            os.chown(
                fingerprint_path,
                owner_uid,
                owner_gid,
                follow_symlinks=False,
            )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--owner-uid", type=int, default=1000)
    parser.add_argument("--owner-gid", type=int, default=1000)
    parser.add_argument("--state-dir", type=Path)
    args = parser.parse_args()
    initialize_key(
        args.path,
        owner_uid=args.owner_uid,
        owner_gid=args.owner_gid,
        state_dir=args.state_dir,
    )
    logger.info("audio master key ready at %s", args.path)


if __name__ == "__main__":
    main()

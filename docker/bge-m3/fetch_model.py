#!/usr/bin/env python3
"""Pre-populate the TEI (text-embeddings-inference) HuggingFace cache for bge-m3.

Why this exists
---------------
TEI's built-in Rust downloader has **no resume**. ``BAAI/bge-m3`` ships its ONNX
weights as external data: ``onnx/model.onnx`` is a ~700 KB graph and
``onnx/model.onnx_data`` is a ~2.2 GB blob. On a slow or lossy link the response
body for that blob is truncated mid-stream, TEI logs

    WARN  Could not download `onnx/model.onnx_data`: request error: error decoding response body

and then every fallback fails too (``model.safetensors`` genuinely does not exist
in that repo, ``pytorch_model.bin`` hits the same truncation), so the ORT backend
aborts with "cannot get file size" and the container never becomes healthy.

``huggingface_hub`` *does* resume — it streams into ``<blob>.incomplete`` and
re-issues a ranged request on retry — and it only renames the temp file to its
final ``<sha256>`` blob name once the whole file has arrived. Running this once
before TEI starts leaves a complete cache on disk; TEI then finds every artifact
locally and downloads nothing.

Modes
-----
``--verify-only``  stdlib only, no network, no pip. Exit 0 when the cache is
                   already complete. This is the fast path on every restart.
``--deep``         additionally re-hash every cached blob and compare it with the
                   blob's filename (HF blob names *are* the file's sha256), which
                   proves the cached weights are byte-exact. Slow (~2.3 GB read).
default            verify, and download whatever is missing with bounded retries.

The download path needs ``huggingface_hub``; the verify path deliberately does
not, so a warm cache costs nothing and works with no network at all.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import time
from pathlib import Path

MODEL_ID = os.environ.get("BGE_M3_MODEL", "BAAI/bge-m3")
CACHE_DIR = Path(os.environ.get("HUGGINGFACE_HUB_CACHE", "/data"))
MAX_ATTEMPTS = int(os.environ.get("BGE_M3_FETCH_ATTEMPTS", "8"))

# Exactly the artifacts TEI asks for when it boots `--model-id BAAI/bge-m3`
# (see its `download_artifacts` and ORT backend logs). Anything outside this set
# — pytorch_model.bin, the colbert/sparse linear heads — is another 2+ GB that
# TEI never reads, so it is deliberately excluded.
REQUIRED = (
    "config.json",
    "config_sentence_transformers.json",
    "sentence_bert_config.json",
    "1_Pooling/config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "onnx/model.onnx",
    "onnx/model.onnx_data",
)
# Fetched when upstream has it, but never required: TEI boots without it, and
# demanding it would turn a warm cache into a pointless network round trip.
OPTIONAL = ("special_tokens_map.json",)
ALLOW_PATTERNS = [*REQUIRED, *OPTIONAL]

_SHA256_NAME = re.compile(r"^[0-9a-f]{64}$")


def _repo_dir() -> Path:
    return CACHE_DIR / ("models--" + MODEL_ID.replace("/", "--"))


def _snapshot_dirs() -> list[Path]:
    """Every materialised snapshot, newest ref first."""
    repo = _repo_dir()
    snapshots = repo / "snapshots"
    if not snapshots.is_dir():
        return []
    ordered: list[Path] = []
    ref = repo / "refs" / "main"
    if ref.is_file():
        head = snapshots / ref.read_text().strip()
        if head.is_dir():
            ordered.append(head)
    ordered.extend(sorted(p for p in snapshots.iterdir() if p.is_dir() and p not in ordered))
    return ordered


def _missing_in(snapshot: Path, *, deep: bool) -> list[str]:
    """Return the required paths that are absent, empty, or (deep) corrupt."""
    bad: list[str] = []
    for rel in REQUIRED:
        entry = snapshot / rel
        if not entry.exists():  # follows symlinks; a dangling link is "missing"
            bad.append(f"{rel}: absent")
            continue
        blob = entry.resolve()
        size = blob.stat().st_size
        if size == 0:
            bad.append(f"{rel}: empty")
            continue
        # huggingface_hub streams into `<sha>.incomplete` and only renames to the
        # bare `<sha>` blob name after the *whole* file landed, so a 64-hex blob
        # name is itself the completeness receipt for an LFS file.
        if not deep or not _SHA256_NAME.match(blob.name):
            continue
        digest = hashlib.sha256()
        with blob.open("rb") as fh:
            for chunk in iter(lambda: fh.read(8 << 20), b""):
                digest.update(chunk)
        if digest.hexdigest() != blob.name:
            bad.append(f"{rel}: sha256 {digest.hexdigest()} != blob name {blob.name}")
    return bad


def verify(*, deep: bool) -> tuple[bool, str]:
    snapshots = _snapshot_dirs()
    if not snapshots:
        return False, f"no snapshot under {_repo_dir()}"
    reasons: list[str] = []
    for snapshot in snapshots:
        bad = _missing_in(snapshot, deep=deep)
        if not bad:
            total = sum(
                (snapshot / rel).resolve().stat().st_size for rel in REQUIRED
            )
            mode = "deep-verified" if deep else "verified"
            return True, f"{snapshot} {mode}, {len(REQUIRED)} files, {total / 2**20:.1f} MiB"
        reasons.append(f"{snapshot.name}: " + "; ".join(bad))
    return False, " | ".join(reasons)


def download() -> None:
    from huggingface_hub import snapshot_download

    last: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            path = snapshot_download(
                repo_id=MODEL_ID,
                cache_dir=str(CACHE_DIR),
                allow_patterns=ALLOW_PATTERNS,
                max_workers=int(os.environ.get("BGE_M3_FETCH_WORKERS", "4")),
                token=os.environ.get("HF_TOKEN") or None,
            )
            print(f"[bge-m3-cache-init] snapshot_download -> {path}", flush=True)
            return
        except Exception as exc:  # noqa: BLE001 - retry anything transport-shaped
            last = exc
            backoff = min(60, 2**attempt)
            print(
                f"[bge-m3-cache-init] attempt {attempt}/{MAX_ATTEMPTS} failed: "
                f"{type(exc).__name__}: {exc}; retrying in {backoff}s "
                "(partial bytes are kept and resumed)",
                file=sys.stderr,
                flush=True,
            )
            if attempt == MAX_ATTEMPTS:
                break
            time.sleep(backoff)
    raise SystemExit(f"[bge-m3-cache-init] giving up after {MAX_ATTEMPTS} attempts: {last}")


def main() -> int:
    deep = "--deep" in sys.argv
    verify_only = "--verify-only" in sys.argv

    ok, detail = verify(deep=deep)
    if ok:
        print(f"[bge-m3-cache-init] cache complete: {detail}", flush=True)
        return 0
    if verify_only:
        print(f"[bge-m3-cache-init] cache incomplete: {detail}", file=sys.stderr, flush=True)
        return 1

    print(f"[bge-m3-cache-init] cache incomplete ({detail}); fetching {MODEL_ID}", flush=True)
    download()

    ok, detail = verify(deep=False)
    if not ok:
        print(f"[bge-m3-cache-init] still incomplete after download: {detail}", file=sys.stderr)
        return 1
    print(f"[bge-m3-cache-init] cache complete: {detail}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

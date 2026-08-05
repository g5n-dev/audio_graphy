"""Executable ratchet over the ``audio_graphy`` package layering.

Five decoupling streams moved metrics into ``observability``, split auth/identity out
of ``api``, and sank the bitemporal graph adapter into ``storage``. Nothing enforced
those boundaries afterwards, so the next refactor could quietly re-add the edges. This
module freezes the layering as it stands today and fails on any regression.

Layer order, lowest first — a module may import anything at its own layer or below::

    0  models, adapters, observability, schemas
    1  storage, llm
    2  core, auth
    3  services, tags, eval, analytics
    4  api
    5  package root (config, db, errors, main, scheduler, tag_worker)

A violation is an import from a strictly higher layer into a lower one. The parser is
pure ``ast``: it counts every static import site, including the ones under
``if TYPE_CHECKING:`` and inside function bodies. Those deferred forms are exactly how
the surviving cycles are worked around today, so excluding them would hide the debt
this file exists to track.

``KNOWN_VIOLATIONS`` splits into two groups.

Accepted — the target is a dependency-free leaf whose only sin is where it lives:

* ``audio_graphy.errors`` and ``audio_graphy.config`` (46 edges between them) sit at the
  package root because they are cross-cutting. ``errors`` imports nothing from the
  package at all and ``config`` only reaches ``adapters.bundle`` from a
  ``TYPE_CHECKING`` block and a function body, so neither can close a runtime cycle.
* ``audio_graphy.db``, imported once by ``eval.cli``, has the same shape.
* ``core.silero_framing`` holds four framing constants and imports nothing at all --
  the batch VAD image copies exactly five files, which is the proof. It cannot live
  under ``adapters`` without pulling ``adapters/__init__.py`` into that image.
* ``core.types``, ``core.llm_cache_crypto``, ``core.crypto`` and ``core.audio_timeline``
  are value/exception/helper modules with no upward edges of their own; ``storage``,
  ``models`` and ``schemas`` reaching into them is a naming problem, not a cycle.

Outstanding debt — real upward runtime dependencies that should be inverted:

* ``core.retention`` and ``auth.middleware`` reaching into ``services``
  (``receptions``, ``reception_erasure``) and ``core.streaming_tag_scheduler`` reaching
  into ``tags.recompute``. The domain layer depends on the orchestration layer above it.
  The ``llm_gateway`` half of this group is gone: the gateway moved to
  ``audio_graphy.llm``, below ``core``, with ``services.llm_gateway`` left as a
  re-export shim so existing importers keep working.
* ``adapters`` ↔ ``core.chunker`` is a genuine cycle (``core.chunker`` imports
  ``adapters.bundle`` and ``adapters.protocols`` at module scope), already papered over
  with ``TYPE_CHECKING`` and lazy imports in the three adapter modules on the other side.
``optimizers`` used to be pinned to the same layer as ``services`` because
``optimizers.artifacts`` imported two helpers from that layer -- ``canonical_checksum``
and ``estimate_prompt_tokens``. (An earlier note here called it a mutual cycle; the
history shows the edge only ever ran one way, optimizers -> services -- wrong-layer
placement, not a cycle.) Both helpers moved to ``core.canonical``, the original
modules re-export them, and ``optimizers`` now sits below ``services``. Sharing the
implementation rather than copying it also matters on its own: a checksum the
compiler computes has to equal the one a search manifest records, and a token
estimate made at compile time has to equal the one the extractor enforces.

Fixing any entry means deleting it here: ``test_known_violations_still_exist`` fails on
a stale allowlist, so this list cannot rot into permanent permission.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import audio_graphy

PACKAGE = audio_graphy.__name__
PACKAGE_DIR = Path(audio_graphy.__file__ or "").resolve().parent

LAYERS: tuple[tuple[str, ...], ...] = (
    ("models", "adapters", "observability", "schemas"),
    # `llm` wraps an LLMAdapter with retry, caching and cost accounting. It sits
    # here, below core, because core/extractor, retrieval, rerank,
    # community_summary and streaming_retrieval all need the request contract —
    # while it lived under services/ those five imports pointed upward.
    ("storage", "llm"),
    ("core", "auth"),
    # `optimizers` sits below `services`: the prompt compiler reads
    # `core.canonical` for checksums and token estimates, and `services.prompt_lab`
    # drives the compiler. The upward import that previously forced same-layer
    # placement is gone.
    ("optimizers",),
    ("services", "tags", "eval", "analytics"),
    ("api",),
)
ROOT_LAYER = len(LAYERS)
SUBPACKAGE_LAYER: dict[str, int] = {
    name: depth for depth, names in enumerate(LAYERS) for name in names
}

KNOWN_VIOLATIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("audio_graphy.adapters.bundle", "audio_graphy.config"),
        ("audio_graphy.adapters.mock_streaming_vad", "audio_graphy.core.chunker"),
        ("audio_graphy.adapters.protocols", "audio_graphy.core.chunker"),
        ("audio_graphy.adapters.real.streaming_vad_silero", "audio_graphy.core.chunker"),
        # Accepted: core.silero_framing declares four framing constants and imports
        # nothing whatsoever. The batch VAD container proves it -- its whole import
        # closure is five files. Putting the constants under adapters/ instead would
        # drag adapters/__init__.py, and with it the LLM/embedding/ASR adapters, into
        # that image; see the module docstring.
        ("audio_graphy.adapters.real.streaming_vad_silero", "audio_graphy.core.silero_framing"),
        ("audio_graphy.api.auth", "audio_graphy.config"),
        ("audio_graphy.api.auth", "audio_graphy.errors"),
        ("audio_graphy.api.bi_temporal", "audio_graphy.errors"),
        ("audio_graphy.api.compression_admin", "audio_graphy.errors"),
        ("audio_graphy.api.deps", "audio_graphy.errors"),
        ("audio_graphy.api.dsar", "audio_graphy.errors"),
        ("audio_graphy.api.eval", "audio_graphy.errors"),
        ("audio_graphy.api.graph", "audio_graphy.config"),
        ("audio_graphy.api.graph", "audio_graphy.errors"),
        # Accepted (errors is a package-root cross-cutting leaf; see the module
        # docstring's taxonomy): the two open-API routers raise APIError like
        # every other router.
        ("audio_graphy.api.integration_admin", "audio_graphy.errors"),
        ("audio_graphy.api.open", "audio_graphy.errors"),
        ("audio_graphy.api.leiden_admin", "audio_graphy.errors"),
        ("audio_graphy.api.prompt_lab", "audio_graphy.errors"),
        ("audio_graphy.api.prompts", "audio_graphy.errors"),
        ("audio_graphy.api.reception_state_insights", "audio_graphy.errors"),
        ("audio_graphy.api.reception_tags", "audio_graphy.errors"),
        ("audio_graphy.api.receptions", "audio_graphy.errors"),
        ("audio_graphy.api.search", "audio_graphy.errors"),
        ("audio_graphy.api.segments", "audio_graphy.errors"),
        ("audio_graphy.api.speakers", "audio_graphy.errors"),
        ("audio_graphy.api.stats", "audio_graphy.errors"),
        ("audio_graphy.api.tag_governance", "audio_graphy.errors"),
        ("audio_graphy.api.tag_insights", "audio_graphy.errors"),
        ("audio_graphy.api.tags", "audio_graphy.errors"),
        ("audio_graphy.api.ws_stream", "audio_graphy.config"),
        ("audio_graphy.api.ws_stream", "audio_graphy.errors"),
        ("audio_graphy.auth.identity", "audio_graphy.errors"),
        ("audio_graphy.auth.jwt_utils", "audio_graphy.errors"),
        ("audio_graphy.auth.middleware", "audio_graphy.errors"),
        ("audio_graphy.auth.middleware", "audio_graphy.services.receptions"),
        ("audio_graphy.auth.roles", "audio_graphy.errors"),
        ("audio_graphy.auth.tenants", "audio_graphy.errors"),
        ("audio_graphy.auth.ws_auth", "audio_graphy.errors"),
        ("audio_graphy.core.retention", "audio_graphy.config"),
        ("audio_graphy.core.retention", "audio_graphy.services.reception_erasure"),
        ("audio_graphy.core.streaming_tag_scheduler", "audio_graphy.tags.recompute"),
        ("audio_graphy.eval.cli", "audio_graphy.config"),
        ("audio_graphy.eval.cli", "audio_graphy.db"),
        ("audio_graphy.eval.runner", "audio_graphy.config"),
        ("audio_graphy.models.voiceprint_vector", "audio_graphy.core.crypto"),
        ("audio_graphy.schemas.receptions", "audio_graphy.core.audio_timeline"),
        ("audio_graphy.services.ingestion", "audio_graphy.errors"),
        ("audio_graphy.services.llm_runtime", "audio_graphy.config"),
        ("audio_graphy.services.reception_audio_operations", "audio_graphy.errors"),
        ("audio_graphy.services.reception_automation", "audio_graphy.errors"),
        ("audio_graphy.services.reception_pipeline", "audio_graphy.errors"),
        ("audio_graphy.services.reception_state_insights", "audio_graphy.errors"),
        ("audio_graphy.services.reception_tagging", "audio_graphy.errors"),
        ("audio_graphy.services.receptions", "audio_graphy.errors"),
        ("audio_graphy.services.stage_projection", "audio_graphy.errors"),
        ("audio_graphy.services.topic_clusters", "audio_graphy.errors"),
        ("audio_graphy.storage.community_state", "audio_graphy.core.types"),
        ("audio_graphy.storage.file_index", "audio_graphy.core.types"),
        ("audio_graphy.storage.graph_bitemporal", "audio_graphy.core.types"),
        ("audio_graphy.storage.graph_networkx", "audio_graphy.core.types"),
        ("audio_graphy.storage.llm_cache_store", "audio_graphy.core.llm_cache_crypto"),
        ("audio_graphy.storage.llm_hot_cache", "audio_graphy.core.llm_cache_crypto"),
        ("audio_graphy.storage.mysql_audio_vector", "audio_graphy.core.types"),
        ("audio_graphy.storage.mysql_vector", "audio_graphy.core.types"),
        ("audio_graphy.storage.vector_index_cache", "audio_graphy.core.types"),
        ("audio_graphy.tags.recompute", "audio_graphy.errors"),
    }
)


def _source_files() -> list[Path]:
    return [path for path in sorted(PACKAGE_DIR.rglob("*.py")) if "__pycache__" not in path.parts]


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(PACKAGE_DIR.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _layer_of(module: str) -> int | None:
    parts = module.split(".")
    if parts[0] != PACKAGE:
        return None
    if len(parts) == 1:
        return ROOT_LAYER
    # An unmapped subpackage defaults to the top layer, so a newly added one shows up as
    # a violation rather than silently escaping the ratchet.
    return SUBPACKAGE_LAYER.get(parts[1], ROOT_LAYER)


def _imported_modules(path: Path, module: str, known: frozenset[str]) -> Iterator[str]:
    """Yield the absolute module names ``module`` imports, relative forms resolved."""
    is_package = path.name == "__init__.py"
    package_parts = module.split(".") if is_package else module.split(".")[:-1]
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            kept = len(package_parts) - (node.level - 1)
            if kept < 0:
                continue
            base_parts = package_parts[:kept]
            if node.module:
                base_parts = [*base_parts, node.module]
            base = ".".join(base_parts)
        else:
            base = node.module or ""
        if not base:
            continue
        for alias in node.names:
            # `from pkg import submodule` is the same edge as `from pkg.submodule import X`;
            # normalising keeps one entry per module pair regardless of call-site style.
            candidate = f"{base}.{alias.name}"
            yield candidate if candidate in known else base


def _discover_violations() -> set[tuple[str, str]]:
    files = _source_files()
    known = frozenset(_module_name(path) for path in files)
    violations: set[tuple[str, str]] = set()
    for path in files:
        source = _module_name(path)
        source_layer = _layer_of(source)
        if source_layer is None:
            continue
        for target in _imported_modules(path, source, known):
            target_layer = _layer_of(target)
            if target_layer is not None and target_layer > source_layer:
                violations.add((source, target))
    return violations


def _format(edges: list[tuple[str, str]]) -> str:
    return "\n".join(f'  ("{source}", "{target}"),' for source, target in edges)


def test_no_unfrozen_layering_violations() -> None:
    unexpected = sorted(_discover_violations() - KNOWN_VIOLATIONS)
    assert not unexpected, (
        f"{len(unexpected)} import(s) cross the layer boundary upward and are not frozen "
        f"in KNOWN_VIOLATIONS:\n{_format(unexpected)}\n"
        "Invert the dependency, or justify the edge in the module docstring and add it here."
    )


def test_known_violations_still_exist() -> None:
    stale = sorted(KNOWN_VIOLATIONS - _discover_violations())
    assert not stale, (
        f"{len(stale)} KNOWN_VIOLATIONS entr(ies) no longer exist:\n{_format(stale)}\n"
        "Delete them so the allowlist keeps shrinking instead of granting stale permission."
    )


def test_core_compression_import_stays_free_of_fastapi() -> None:
    """``core.compression`` used to reach ``api.metrics``; the counters now live lower."""
    probe = (
        "import sys\n"
        f"sys.path.insert(0, {str(PACKAGE_DIR.parent)!r})\n"
        f"import {PACKAGE}.core.compression\n"
        "print(','.join(sorted(m for m in sys.modules if m.split('.')[0] == 'fastapi')))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, f"probe failed:\n{result.stderr}"
    leaked = result.stdout.strip()
    assert not leaked, (
        f"importing {PACKAGE}.core.compression pulled FastAPI into sys.modules: {leaked}. "
        "Core must not depend on the web framework — keep observability counters in "
        f"{PACKAGE}.observability.metrics."
    )

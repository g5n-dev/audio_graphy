"""Shared fixtures for core algorithm layer tests.

Provides:
    - mock_bundle: AdapterBundle with all mock adapters
    - scripted_llm: A configurable LLM adapter for deterministic test responses
    - tmp_working_dir, file_index, graph_store, vector_store
    - test data fixtures (audio files, sample transcripts)
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import audio_graphy.models  # noqa: F401
from audio_graphy.adapters.protocols import LLMResponse
from audio_graphy.models.base import Base
from tests.dbreset import drop_every_table_async, ensure_database, suite_database

# MySQL connection for integration tests
MYSQL_HOST = os.environ.get("MODEL_TEST_MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = os.environ.get("MODEL_TEST_MYSQL_PORT", "3307")
MYSQL_USER = os.environ.get("MODEL_TEST_MYSQL_USER", "audiography")
MYSQL_PASSWORD = os.environ.get("MODEL_TEST_MYSQL_PASSWORD", "change-me")
MYSQL_DB = suite_database("core")
ASYNC_DSN = (
    f"mysql+aiomysql://{MYSQL_USER}:{MYSQL_PASSWORD}"
    f"@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DB}?charset=utf8mb4"
)


# ============================================================
# Scripted LLM Adapter — deterministic responses for tests
# ============================================================


class ScriptedLLMAdapter:
    """A test LLM adapter that returns predefined responses.

    Allows tests to control exactly what the LLM returns, enabling
    deterministic testing of parsing, gleaning, filtering, etc.

    Usage:
        adapter = ScriptedLLMAdapter()
        adapter.set_response("entity extraction", '<GraphRAG output>')
        adapter.set_response("gleaning", '<gleaning output>')
        # The adapter checks the prompt content for keywords to pick the response
    """

    def __init__(self, *, model: str = "test-llm") -> None:
        self.model = model
        self._responses: dict[str, str] = {}
        self._default_response: str = '("实体","测试实体","车型")<|COMPLETE|>'
        self._call_count = 0
        self._cache: dict[str, LLMResponse] = {}
        self.response_schemas: list[Mapping[str, Any] | None] = []

    def set_response(self, keyword: str, response: str) -> None:
        """Set a response that will be returned when the prompt contains keyword."""
        self._responses[keyword] = response

    def set_default_response(self, response: str) -> None:
        """Set the fallback response when no keyword matches."""
        self._default_response = response

    @staticmethod
    def compute_prompt_hash(model: str, messages: Sequence[dict[str, str]]) -> str:
        """MD5 of (model, messages) — same as MockLLMAdapter."""
        payload = json.dumps({"model": model, "messages": list(messages)}, ensure_ascii=False)
        return hashlib.md5(payload.encode("utf-8")).hexdigest()

    async def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        cache_key: str | None = None,
        response_schema: Mapping[str, Any] | None = None,
    ) -> LLMResponse:
        # Keep the fake honest about the production transport contract: callers
        # may require strict structured output and the schema must not be
        # silently discarded merely because this adapter is test-only.
        self.response_schemas.append(response_schema)
        self._call_count += 1

        prompt_hash = self.compute_prompt_hash(self.model, messages)

        # Check cache
        if cache_key and cache_key in self._cache:
            cached = self._cache[cache_key]
            return LLMResponse(
                text=cached.text,
                model=cached.model,
                prompt_hash=cached.prompt_hash,
                cached=True,
                usage=cached.usage,
            )

        # Build the full prompt text to match keywords
        full_text = " ".join(m.get("content", "") for m in messages)

        # Find matching response
        response_text = self._default_response
        for keyword, resp in self._responses.items():
            if keyword in full_text:
                response_text = resp
                break

        response = LLMResponse(
            text=response_text,
            model=self.model,
            prompt_hash=prompt_hash,
            cached=False,
            usage={
                "prompt_tokens": sum(len(m.get("content", "")) for m in messages) // 2,
                "completion_tokens": len(response_text) // 2,
                "total_tokens": (
                    sum(len(m.get("content", "")) for m in messages) + len(response_text)
                )
                // 2,
            },
        )

        if cache_key:
            self._cache[cache_key] = response

        return response

    @property
    def call_count(self) -> int:
        """Number of non-cached calls made."""
        return self._call_count


# ============================================================
# Async DB fixtures (for integration tests)
# ============================================================


@pytest_asyncio.fixture
async def async_engine() -> AsyncIterator[Any]:
    """Create an async SQLAlchemy engine and initialise all tables (function-scoped)."""
    # Probed here rather than at import time: a pytest.skip during conftest import
    # is fatal, not a skip, so it took down every test in the package including the
    # ones needing no database.
    ensure_database(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        name=MYSQL_DB,
    )
    engine = create_async_engine(ASYNC_DSN, echo=False, pool_size=5)

    # Introspect and drop rather than metadata.drop_all, so a schema left behind by
    # older models cannot wedge the fixture that exists to reset it.
    await drop_every_table_async(engine)
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn))

    yield engine

    await drop_every_table_async(engine)
    await engine.dispose()


@pytest_asyncio.fixture
async def async_session_factory(async_engine: Any) -> async_sessionmaker[AsyncSession]:
    """Return an async_sessionmaker bound to the test engine."""
    return async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def async_db_session(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Provide an async session with automatic table cleanup."""
    import contextlib

    from sqlalchemy import text

    session = async_session_factory()
    yield session
    await session.close()

    async with async_session_factory() as cleanup:
        await cleanup.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for table in Base.metadata.sorted_tables:
            with contextlib.suppress(Exception):
                await cleanup.execute(text(f"TRUNCATE TABLE `{table.name}`"))
        await cleanup.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        await cleanup.commit()


# ============================================================
# Working dir + storage fixtures
# ============================================================


@pytest.fixture
def tmp_working_dir(tmp_path: Path) -> Path:
    """Temporary working_dir directory."""
    return tmp_path / "working_dir"


@pytest_asyncio.fixture
async def file_index(tmp_working_dir: Path) -> Any:
    """FileIndex instance."""
    from audio_graphy.storage.file_index import FileIndex

    return FileIndex(tmp_working_dir, tenant_id="default")


@pytest_asyncio.fixture
async def graph_store(tmp_working_dir: Path) -> Any:
    """NetworkXGraphStore instance."""
    from audio_graphy.storage.graph_networkx import NetworkXGraphStore

    return NetworkXGraphStore(tmp_working_dir, tenant_id="default")


@pytest_asyncio.fixture
async def vector_store(async_session_factory: async_sessionmaker[AsyncSession]) -> Any:
    """MySQLVectorStore instance."""
    from audio_graphy.storage.mysql_vector import MySQLVectorStore

    return MySQLVectorStore(async_session_factory, dim=1024)


# ============================================================
# Adapter bundle fixtures
# ============================================================


@pytest.fixture
def mock_bundle() -> Any:
    """Build a mock AdapterBundle with zero error rate for deterministic tests."""
    from audio_graphy.adapters.bundle import AdapterBundle
    from audio_graphy.adapters.mock_asr import MockASRAdapter
    from audio_graphy.adapters.mock_embed import MockEmbedAdapter
    from audio_graphy.adapters.mock_llm import MockLLMAdapter
    from audio_graphy.adapters.mock_vad import MockVADAdapter

    return AdapterBundle(
        vad=MockVADAdapter(),
        asr=MockASRAdapter(flaky=False),
        strong_llm=MockLLMAdapter(model="test-strong", error_rate=0.0),
        weak_llm=MockLLMAdapter(model="test-weak", error_rate=0.0),
        embed=MockEmbedAdapter(dim=1024),
    )


@pytest.fixture
def scripted_bundle() -> Any:
    """Build an AdapterBundle with ScriptedLLMAdapter for deterministic extraction tests."""
    from audio_graphy.adapters.bundle import AdapterBundle
    from audio_graphy.adapters.mock_asr import MockASRAdapter
    from audio_graphy.adapters.mock_embed import MockEmbedAdapter
    from audio_graphy.adapters.mock_vad import MockVADAdapter

    strong_llm = ScriptedLLMAdapter(model="test-strong")
    weak_llm = ScriptedLLMAdapter(model="test-weak")

    return AdapterBundle(
        vad=MockVADAdapter(),
        asr=MockASRAdapter(flaky=False),
        strong_llm=strong_llm,
        weak_llm=weak_llm,
        embed=MockEmbedAdapter(dim=1024),
    )


# ============================================================
# Audio file fixture
# ============================================================


@pytest.fixture
def sample_audio_file(tmp_path: Path) -> Path:
    """Create a minimal dummy audio file for mock VAD/ASR adapters."""
    audio_path = tmp_path / "sample.wav"
    # Write enough bytes so the mock VAD/ASR produce reasonable segments
    audio_path.write_bytes(b"\x00" * 500_000)  # ~500KB → ~5s of mock audio
    return audio_path


# ============================================================
# Prompt template fixture
# ============================================================


@pytest.fixture
def entity_prompt_template() -> str:
    """Load the entity extraction prompt template from file."""
    prompt_path = (
        Path(__file__).resolve().parent.parent.parent / "audio_graphy" / "prompts" / "entity_zh.md"
    )
    if prompt_path.exists():
        return prompt_path.read_text(encoding="utf-8")
    # Fallback inline prompt for tests
    return (
        "请从以下对话文本中抽取实体和关系。\n\n"
        "实体类型: {entity_types}\n\n"
        "输出格式:\n"
        '("实体"{tuple_delimiter}名称{tuple_delimiter}类型{tuple_delimiter}描述)'
        "{record_delimiter}"
        '("关系"{tuple_delimiter}源实体{tuple_delimiter}关系{tuple_delimiter}目标实体{tuple_delimiter}描述)'
        "{completion_delimiter}\n\n"
        "输入:\n{input_text}"
    )


# ============================================================
# GraphRAG-formatted LLM response for entity extraction
# ============================================================


@pytest.fixture
def sample_graphrag_response() -> str:
    """A well-formed GraphRAG delimiter protocol response for testing."""
    from audio_graphy.core.types import (
        COMPLETION_DELIMITER,
        RECORD_DELIMITER,
        TUPLE_DELIMITER,
    )

    return (
        f'("实体"{TUPLE_DELIMITER}CS75 Plus{TUPLE_DELIMITER}车型{TUPLE_DELIMITER}'
        f"长安CS75 Plus是热门SUV车型)"
        f"{RECORD_DELIMITER}"
        f'("实体"{TUPLE_DELIMITER}张敏{TUPLE_DELIMITER}坐席{TUPLE_DELIMITER}销售顾问张敏)'
        f"{RECORD_DELIMITER}"
        f'("实体"{TUPLE_DELIMITER}5万元{TUPLE_DELIMITER}价格方案{TUPLE_DELIMITER}全款优惠5万元)'
        f"{RECORD_DELIMITER}"
        f'("实体"{TUPLE_DELIMITER}36期分期{TUPLE_DELIMITER}金融政策{TUPLE_DELIMITER}36期分期付款方案)'
        f"{RECORD_DELIMITER}"
        f'("实体"{TUPLE_DELIMITER}哈弗H6{TUPLE_DELIMITER}竞品{TUPLE_DELIMITER}哈弗H6是竞品车型)'
        f"{RECORD_DELIMITER}"
        f'("关系"{TUPLE_DELIMITER}张敏{TUPLE_DELIMITER}推荐{TUPLE_DELIMITER}CS75 Plus{TUPLE_DELIMITER}'
        f"坐席张敏向客户推荐了CS75 Plus)"
        f"{RECORD_DELIMITER}"
        f'("关系"{TUPLE_DELIMITER}CS75 Plus{TUPLE_DELIMITER}搭配{TUPLE_DELIMITER}5万元{TUPLE_DELIMITER}'
        f"CS75 Plus搭配全款优惠5万元)"
        f"{RECORD_DELIMITER}"
        f'("关系"{TUPLE_DELIMITER}CS75 Plus{TUPLE_DELIMITER}搭配{TUPLE_DELIMITER}36期分期{TUPLE_DELIMITER}'
        f"CS75 Plus搭配36期分期金融政策)"
        f"{RECORD_DELIMITER}"
        f'("关系"{TUPLE_DELIMITER}客户{TUPLE_DELIMITER}对比{TUPLE_DELIMITER}哈弗H6{TUPLE_DELIMITER}'
        f"客户询问哈弗H6做对比)"
        f"{COMPLETION_DELIMITER}"
    )


@pytest.fixture
def sample_csv_response() -> str:
    """A CSV-style response (mock LLM default format) for parser robustness testing."""
    return (
        '("实体","CS75 Plus","车型","长安CS75 Plus是热门SUV车型")\n'
        '("实体","张敏","坐席","销售顾问张敏")\n'
        '("实体","5万元","价格方案","全款优惠5万元")\n'
        '("关系","张敏","推荐","CS75 Plus","坐席张敏向客户推荐了CS75 Plus")\n'
        '("关系","CS75 Plus","搭配","5万元","CS75 Plus搭配全款优惠5万元")'
    )


@pytest.fixture
def sample_recorded_at() -> datetime:
    """A fixed recorded_at timestamp for testing."""
    return datetime(2026, 7, 10, 14, 30, 0, tzinfo=UTC)

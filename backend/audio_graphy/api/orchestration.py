"""Orchestration topology — the pipeline as it actually runs, for the 任务与编排 page.

One GET, inspector-and-above. Every value here is REAL: stage configs read the
live Settings object, queue depths are counted from the database at request
time, and a stage whose adapter runs in mock mode says so. The page this feeds
was ported from a design prototype whose numbers were invented; the deal in
this repository is that production surfaces never render fabricated data, so
the endpoint exposes exactly what the deployment is — no throughput theatre,
no cost counters we do not meter.

Config editing is deliberately absent. Stage parameters are env-driven
(12-factor; see .env.example), so every config row ships locked with the env
key that controls it — the UI's job is to say where the knob lives, not to
pretend there is a runtime mutation API that does not exist.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from audio_graphy.api.deps import get_db
from audio_graphy.auth.roles import require_inspector_or_above
from audio_graphy.auth.tenants import get_tenant_id
from audio_graphy.models.recording import Recording
from audio_graphy.models.tag_governance import TagExtractionJob

router = APIRouter(prefix="/orchestration", tags=["orchestration"])


def _stage(
    *,
    stage_id: str,
    name: str,
    service: str,
    mode: str | None,
    queue: int = 0,
    note: str,
    config: list[tuple[str, str, str]],
    in_schema: list[str],
    out_schema: list[str],
) -> dict[str, Any]:
    """One pipeline stage. ``mode`` None means the stage has no adapter toggle."""

    state = "mock" if mode == "mock" else "busy" if queue > 10 else "ok"
    return {
        "id": stage_id,
        "name": name,
        "service": service,
        "adapter_mode": mode,
        "state": state,
        "queue": queue,
        "note": note,
        # [label, value, env_key] — env_key names the knob that owns the value.
        "config": [list(row) for row in config],
        "in_schema": in_schema,
        "out_schema": out_schema,
    }


@router.get(
    "/topology",
    summary="Pipeline topology with live config and queue depths",
    dependencies=[Depends(require_inspector_or_above())],
)
async def get_topology(
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    settings = request.app.state.settings
    tenant_id = get_tenant_id(request)

    recording_backlog = (
        await session.execute(
            select(func.count(Recording.id)).where(
                Recording.tenant_id == tenant_id,
                Recording.status.in_(("queued", "processing")),
            )
        )
    ).scalar_one()
    tag_job_backlog = (
        await session.execute(
            select(func.count(TagExtractionJob.id)).where(
                TagExtractionJob.tenant_id == tenant_id,
                TagExtractionJob.status.in_(("queued", "running")),
            )
        )
    ).scalar_one()

    stages = [
        _stage(
            stage_id="ingest",
            name="录音接入",
            service="ingestion",
            mode=None,
            queue=int(recording_backlog),
            note="注册/开放接口上传写入 Recording,静态加密与指纹在同一事务内完成。",
            config=[
                (
                    "单文件上限",
                    f"{settings.max_recording_audio_bytes // (1024 * 1024)} MiB",
                    "MAX_RECORDING_AUDIO_BYTES",
                ),
                ("处理并发", str(settings.pipeline_concurrency), "PIPELINE_CONCURRENCY"),
                ("幂等键", "open-upload:<external_ref>", "—"),
            ],
            in_schema=["audio: bytes", "store_id: str", "external_ref: str"],
            out_schema=["recording_id: int", "audio_sha256: str", "duration_ms: int"],
        ),
        _stage(
            stage_id="vad_asr_chunk",
            name="VAD · 转写 · 切分",
            service="silero-vad / funasr / chunker",
            mode=settings.adapter_asr_mode,
            note="VAD 定边界,整段转写后按语义与 token 预算切块;mock 模式的切分与语音内容无关。",
            config=[
                ("VAD 模式", settings.adapter_vad_mode, "ADAPTER_VAD_MODE"),
                ("ASR 模式", settings.adapter_asr_mode, "ADAPTER_ASR_MODE"),
                (
                    "流式 VAD 起始阈值",
                    str(settings.streaming_vad_onset_threshold),
                    "STREAMING_VAD_ONSET_THRESHOLD",
                ),
            ],
            in_schema=["recording_id: int", "audio_uri: str"],
            out_schema=["segment_id: int", "start_ms/end_ms: int", "text: str", "chunk_id: int"],
        ),
        _stage(
            stage_id="voiceprint",
            name="声纹与说话人",
            service="campplus / speaker_linker",
            mode=settings.adapter_voiceprint_mode,
            note="抽声纹向量并跨录音归并说话人;两阈值之间进人工待确认,不静默合并。",
            config=[
                (
                    "合并余弦阈值",
                    str(settings.voiceprint_cosine_threshold),
                    "VOICEPRINT_COSINE_THRESHOLD",
                ),
                (
                    "免歧义阈值",
                    str(settings.voiceprint_ambiguous_threshold),
                    "VOICEPRINT_AMBIGUOUS_THRESHOLD",
                ),
                (
                    "采样分段上限",
                    str(settings.voiceprint_sample_max_segments),
                    "VOICEPRINT_SAMPLE_MAX_SEGMENTS",
                ),
            ],
            in_schema=["segment_id: int", "audio_slice: bytes"],
            out_schema=["speaker_node_id: int", "cosine: float", "ambiguity_tag: str|null"],
        ),
        _stage(
            stage_id="assemble",
            name="接待组装",
            service="reception_merge",
            mode=None,
            note="相邻录音组合成一次接待;逻辑合并不改写源文件,合并优先级 显式 > 人工 > 自动。",
            config=[
                ("合并策略", "显式 > 人工 > 自动", "—"),
                (
                    "声纹一致性",
                    f"余弦 ≥ {settings.voiceprint_ambiguous_threshold}",
                    "VOICEPRINT_AMBIGUOUS_THRESHOLD",
                ),
                ("候选窗口", "扫描时指定(门店 + 时间窗)", "—"),
            ],
            in_schema=["recording_id: int", "speaker_node_id: int"],
            out_schema=[
                "reception_id: int",
                "merge_mode: logical|physical",
                "merge_confidence: float",
            ],
        ),
        _stage(
            stage_id="extract",
            name="标签抽取",
            service="tag_worker",
            mode=settings.adapter_llm_mode,
            queue=int(tag_job_backlog),
            note="按已发布 Schema 抽取标签事实,证据引用必带;人工更正永不被模型覆写。",
            config=[
                ("LLM 模式", settings.adapter_llm_mode, "ADAPTER_LLM_MODE"),
                ("强模型", settings.llm_strong_model, "LLM_STRONG_MODEL"),
                ("弱模型", settings.llm_weak_model, "LLM_WEAK_MODEL"),
                (
                    "强/弱并发",
                    f"{settings.llm_strong_concurrency} / {settings.llm_weak_concurrency}",
                    "LLM_STRONG_CONCURRENCY",
                ),
            ],
            in_schema=["reception_id: int", "dialogue_unit_id: int"],
            out_schema=["tag_fact_id: int", "label_key/value: str", "evidence_refs: []"],
        ),
        _stage(
            stage_id="graph",
            name="图谱写入",
            service="graph_networkx",
            mode=settings.adapter_embed_mode,
            note="实体归一(别名 + 模糊匹配)后写图;跨进程写有文件锁,损坏文件拒载不清空。",
            config=[
                ("实体模糊阈值", str(settings.entity_fuzzy_threshold), "ENTITY_FUZZY_THRESHOLD"),
                ("Embedding 模式", settings.adapter_embed_mode, "ADAPTER_EMBED_MODE"),
                ("边渲染预算", str(settings.graph_edge_render_budget), "GRAPH_EDGE_RENDER_BUDGET"),
            ],
            in_schema=["tag_fact_id: int", "entity_candidates: []"],
            out_schema=["edge: (src, rel, dst)", "confidence: enum"],
        ),
        _stage(
            stage_id="leiden",
            name="社区检测",
            service="leiden",
            mode=None,
            note="图谱快照上的 Leiden 聚类,结果绑定任务 ID;默认关闭,由 ENABLE_ADVANCED_GRAPH 启用。",
            config=[
                ("启用", str(settings.enable_advanced_graph), "ENABLE_ADVANCED_GRAPH"),
                (
                    "触发阈值",
                    f"{settings.leiden_threshold_percent}% 图变更",
                    "LEIDEN_THRESHOLD_PERCENT",
                ),
            ],
            in_schema=["graph_snapshot: GraphML"],
            out_schema=["leiden_job_id: int", "community_id: int", "level: int"],
        ),
        _stage(
            stage_id="index",
            name="向量索引",
            service="mysql_vector",
            mode=settings.adapter_embed_mode,
            note="文本块与实体入向量库,供问答双通道检索;音频嵌入(CLAP)单独开关。",
            config=[
                ("文本 Embedding", settings.adapter_embed_mode, "ADAPTER_EMBED_MODE"),
                ("音频 Embedding", settings.adapter_audio_embed_mode, "ADAPTER_AUDIO_EMBED_MODE"),
            ],
            in_schema=["chunk_text: str", "entity_name: str"],
            out_schema=["vector_id: int", "namespace: str"],
        ),
    ]

    links = [
        ["ingest", "vad_asr_chunk"],
        ["ingest", "voiceprint"],
        ["vad_asr_chunk", "voiceprint"],
        ["voiceprint", "assemble"],
        ["vad_asr_chunk", "assemble"],
        ["assemble", "extract"],
        ["extract", "graph"],
        ["graph", "leiden"],
        ["extract", "index"],
        ["graph", "index"],
    ]
    return {"stages": stages, "links": links}

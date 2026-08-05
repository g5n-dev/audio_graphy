"""任务与编排拓扑:值必须是真实部署的,权限与积压计数亦然。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit


class TestTopology:
    def test_requires_inspector(self, test_client: TestClient, auth_headers: dict) -> None:
        assert test_client.get("/api/v1/orchestration/topology").status_code == 401
        response = test_client.get(
            "/api/v1/orchestration/topology", headers=auth_headers["agent_t1"]
        )
        assert response.status_code == 403

    def test_stages_report_live_settings_not_prototype_numbers(
        self, test_client: TestClient, auth_headers: dict
    ) -> None:
        response = test_client.get(
            "/api/v1/orchestration/topology", headers=auth_headers["inspector_t1"]
        )
        assert response.status_code == 200, response.text
        body = response.json()
        stages = {stage["id"]: stage for stage in body["stages"]}
        assert set(stages) == {
            "ingest",
            "vad_asr_chunk",
            "voiceprint",
            "assemble",
            "extract",
            "graph",
            "leiden",
            "index",
        }

        settings = test_client.app.state.settings
        voiceprint = stages["voiceprint"]
        merge_row = next(r for r in voiceprint["config"] if r[0] == "合并余弦阈值")
        # 值来自 Settings 本体——原型的 0.82 是编的,这里必须跟部署一致。
        assert merge_row[1] == str(settings.voiceprint_cosine_threshold)
        assert merge_row[2] == "VOICEPRINT_COSINE_THRESHOLD"

        extract = stages["extract"]
        assert extract["adapter_mode"] == settings.adapter_llm_mode
        model_row = next(r for r in extract["config"] if r[0] == "强模型")
        assert model_row[1] == settings.llm_strong_model

        # mock adapter 的阶段必须自曝,而不是装作正常。
        if settings.adapter_llm_mode == "mock":
            assert extract["state"] == "mock"

        # 每条边的两端都是已声明的阶段。
        for source, target in body["links"]:
            assert source in stages and target in stages

    def test_queue_depth_counts_this_tenants_backlog(
        self, test_client: TestClient, auth_headers: dict, db_session_factory
    ) -> None:
        import asyncio

        from audio_graphy.models.recording import Recording

        async def _seed() -> None:
            async with db_session_factory() as session, session.begin():
                session.add(
                    Recording(
                        tenant_id="chang_an",
                        store_id="s1",
                        path="/tmp/orch-queued.wav",
                        status="queued",
                        pipeline_state="pending",
                    )
                )

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_seed())
        finally:
            loop.close()

        response = test_client.get(
            "/api/v1/orchestration/topology", headers=auth_headers["inspector_t1"]
        )
        ingest = next(s for s in response.json()["stages"] if s["id"] == "ingest")
        assert ingest["queue"] >= 1

        other = test_client.get("/api/v1/orchestration/topology", headers=auth_headers["admin_t2"])
        ingest_t2 = next(s for s in other.json()["stages"] if s["id"] == "ingest")
        assert ingest_t2["queue"] == 0, "积压计数必须按租户隔离"

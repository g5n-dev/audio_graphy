"""API contract for the prompt lab: who may do what, and what the wire exposes."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

_READINESS = "/api/v1/prompt-lab/readiness"
_COMPILATIONS = "/api/v1/prompt-lab/compilations"
_ARTIFACTS = "/api/v1/prompt-lab/artifacts"


def _compile_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"baseline_tagger_version_id": 1}
    body.update(overrides)
    return body


def test_readiness_reports_the_gap_for_an_empty_tenant(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
) -> None:
    response = test_client.get(_READINESS, headers=auth_headers["inspector_t1"])

    assert response.status_code == 200
    payload = response.json()
    assert payload["ready"] is False
    assert payload["gold_label_total"] == 0
    assert "reviewed_feedback_below_200" in payload["blockers"]
    assert payload["feedback_threshold"] == 200
    assert payload["domain_threshold"] == 30


def test_reading_the_lab_requires_inspector_or_above(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
) -> None:
    assert test_client.get(_READINESS, headers=auth_headers["viewer_t1"]).status_code == 403
    assert test_client.get(_READINESS, headers=auth_headers["agent_t1"]).status_code == 403
    assert test_client.get(_READINESS, headers=auth_headers["inspector_t1"]).status_code == 200


def test_compiling_requires_admin(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
) -> None:
    """An inspector can read the lab but cannot spend provider budget in it."""

    forbidden = test_client.post(
        _COMPILATIONS,
        headers=auth_headers["inspector_t1"],
        json=_compile_body(),
    )
    assert forbidden.status_code == 403


def test_an_unauthenticated_caller_reaches_nothing(test_client: TestClient) -> None:
    assert test_client.get(_READINESS).status_code == 401
    assert test_client.post(_COMPILATIONS, json=_compile_body()).status_code == 401


def test_a_missing_artifact_is_a_404_not_a_500(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
) -> None:
    response = test_client.get(f"{_ARTIFACTS}/999999", headers=auth_headers["inspector_t1"])

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PROMPT_LAB_NOT_FOUND"


def test_verbatim_redaction_cannot_be_requested_through_the_api(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
) -> None:
    """The mode exists in the model layer; the wire contract must not expose it."""

    response = test_client.post(
        _COMPILATIONS,
        headers=auth_headers["admin_t1"],
        json=_compile_body(compiler={"redaction_mode": "verbatim"}),
    )

    assert response.status_code == 422


def test_unknown_compiler_options_are_rejected_rather_than_ignored(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
) -> None:
    response = test_client.post(
        _COMPILATIONS,
        headers=auth_headers["admin_t1"],
        json=_compile_body(compiler={"compiler": "builtin", "unknown_knob": 1}),
    )

    assert response.status_code == 422


def test_a_budget_outside_its_bounds_is_rejected(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
) -> None:
    response = test_client.post(
        _COMPILATIONS,
        headers=auth_headers["admin_t1"],
        json=_compile_body(budget={"max_provider_calls": 100_000}),
    )

    assert response.status_code == 422


def test_a_duplicate_patch_decision_is_rejected(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
) -> None:
    """Two verdicts for one patch is ambiguous, so the batch is refused."""

    response = test_client.post(
        f"{_ARTIFACTS}/1/decisions",
        headers=auth_headers["admin_t1"],
        json={
            "decisions": [
                {"patch_id": "a" * 32, "decision": "accepted"},
                {"patch_id": "a" * 32, "decision": "rejected"},
            ]
        },
    )

    assert response.status_code == 422


def test_a_compilation_against_a_missing_baseline_is_a_404(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
) -> None:
    response = test_client.post(
        _COMPILATIONS,
        headers=auth_headers["admin_t1"],
        json=_compile_body(baseline_tagger_version_id=999999),
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PROMPT_LAB_NOT_FOUND"


def test_a_compiler_the_build_cannot_run_is_a_400_not_a_silent_builtin(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
) -> None:
    """CompilerName is wider than what is implemented; the gap must be visible.

    The failure this guards against is silent: the request is accepted, a builtin
    artifact comes back, and its ``compiler`` field says ``dspy_mipro``.
    """

    response = test_client.post(
        _COMPILATIONS,
        headers=auth_headers["admin_t1"],
        json=_compile_body(compiler={"compiler": "dspy_mipro"}),
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "PROMPT_LAB_INVALID"
    assert "dspy_mipro" in response.json()["error"]["message"]


def test_the_artifact_list_omits_prompt_bodies_when_empty(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
) -> None:
    response = test_client.get(_ARTIFACTS, headers=auth_headers["inspector_t1"])

    assert response.status_code == 200
    payload = response.json()
    assert payload == {"items": [], "total": 0}


# A preflight report as the optimizer worker actually writes it, measured on a real
# 12-definition harness spec: the candidate policy is 149 tokens longer than the
# baseline, and every fixed cost dwarfs the bare policy text it wraps.
_BUDGET_REPORT: dict[str, Any] = {
    "prompt_tokens": 415,
    "schema_tokens": 1_292,
    "fixed_tokens": 1_707,
    "usable_tokens": 10_800,
    "headroom_tokens": 9_093,
    "baseline_fixed_tokens": 1_558,
    "baseline_headroom_tokens": 9_242,
    "headroom_delta": -149,
    "headroom_shrink_ratio": 0.0161,
    "fits": True,
}


def _seed_artifact(
    db_session_factory: Any,
    *,
    tenant_id: str = "chang_an",
    budget_report: dict[str, Any] | None = None,
) -> int:
    """Persist a two-patch artifact through the service, as a compile would."""

    from audio_graphy.optimizers.artifacts import (
        CompiledPromptArtifact,
        PromptPatch,
    )
    from audio_graphy.services.prompt_lab import PromptLabService
    from tests.api.conftest import _run_async

    def _patch(patch_id: str, ordinal: int, body: str) -> PromptPatch:
        return PromptPatch(
            patch_id=patch_id,
            kind="rule_clarification",
            origin="builtin",
            ordinal=ordinal,
            body=body,
            rationale=f"cluster {patch_id}",
            target_tag_keys=("intent",),
        )

    artifact = CompiledPromptArtifact(
        baseline_prompt="基线规则",
        header="基线规则",
        compiler="builtin",
        compiler_version="builtin-proposer-v1",
        metric_version="prompt-lab-metric-v1",
        patches=(_patch("a" * 32, 1, "规则一"), _patch("b" * 32, 2, "规则二")),
        accepted_patch_ids=frozenset({"a" * 32, "b" * 32}),
    )
    service = PromptLabService(db_session_factory)
    row = _run_async(
        service.persist_artifact(
            tenant_id=tenant_id,
            compilation_id=1,
            artifact=artifact,
            baseline_tagger_version_id=1,
            gold_set_version_id=None,
            actor_user_id=1,
            input_budget_report=(
                dict(_BUDGET_REPORT) if budget_report is None else dict(budget_report)
            ),
            gradients=[
                {
                    "patch_id": patch.patch_id,
                    "tag_key": "intent",
                    "failure_stage": "tag_reasoning",
                    "gradient_text": f"诊断 {patch.patch_id}",
                    "proposed_edit": patch.body,
                    "evaluation": {"support": 6},
                }
                for patch in artifact.patches
            ],
        )
    )
    return int(row.id)


def test_the_diff_view_prices_the_candidate_against_its_baseline(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    """A reviewer must see what the prompt costs before accepting it."""

    artifact_id = _seed_artifact(db_session_factory)

    response = test_client.get(
        f"{_ARTIFACTS}/{artifact_id}/diff",
        headers=auth_headers["inspector_t1"],
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["baseline_prompt"] == "基线规则"
    assert "规则一" in payload["candidate_prompt"]
    assert "规则二" in payload["candidate_prompt"]
    assert len(payload["patches"]) == 2
    assert payload["input_budget_report"]["baseline_fixed_tokens"] == 1_558
    assert payload["fixed_token_delta"] == 1_707 - 1_558


def test_the_diff_prices_a_longer_candidate_as_a_cost_not_a_saving(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    """The comparison must be like with like, or its sign is decoration.

    ``prompt_token_estimate`` is the candidate's bare policy text; the baseline number
    on the same card used to be the baseline's whole transport cost (schema included).
    Subtracting the second from the first made a candidate that costs 149 more tokens
    read as a four-digit saving, which the review view then coloured as an improvement.
    """

    artifact_id = _seed_artifact(db_session_factory)

    response = test_client.get(
        f"{_ARTIFACTS}/{artifact_id}/diff",
        headers=auth_headers["inspector_t1"],
    )

    assert response.status_code == 200
    payload = response.json()
    budget = payload["input_budget_report"]
    # The bare policy text is far smaller than the transport cost that wraps it:
    # this is exactly the shape that made the old subtraction change sign.
    assert payload["prompt_token_estimate"] < budget["baseline_fixed_tokens"]
    assert payload["fixed_token_delta"] == 149
    assert payload["fixed_token_delta"] > 0
    # The two comparable measurements have to agree: giving up 149 tokens of headroom
    # is the same event as costing 149 more fixed tokens.
    assert payload["fixed_token_delta"] == -budget["headroom_delta"]
    # The unit-less fields are gone rather than merely recomputed; a reader cannot
    # accidentally pick the one that compares a policy against a transport cost.
    assert "token_delta" not in payload
    assert "baseline_prompt_token_estimate" not in payload


def test_the_diff_refuses_to_price_a_candidate_that_was_never_measured(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    """An unmeasured budget yields no difference at all, not a flattering zero."""

    artifact_id = _seed_artifact(db_session_factory, budget_report={})

    response = test_client.get(
        f"{_ARTIFACTS}/{artifact_id}/diff",
        headers=auth_headers["inspector_t1"],
    )

    assert response.status_code == 200
    assert response.json()["fixed_token_delta"] is None


def test_rejecting_a_patch_through_the_api_returns_the_new_artifact(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    artifact_id = _seed_artifact(db_session_factory)

    response = test_client.post(
        f"{_ARTIFACTS}/{artifact_id}/decisions",
        headers=auth_headers["admin_t1"],
        json={
            "decisions": [
                {"patch_id": "a" * 32, "decision": "accepted"},
                {"patch_id": "b" * 32, "decision": "rejected", "note": "与总则冲突"},
            ]
        },
    )

    assert response.status_code == 201
    child = response.json()
    assert child["id"] != artifact_id
    assert child["parent_artifact_id"] == artifact_id
    assert child["accepted_patch_ids"] == ["a" * 32]
    assert "规则一" in child["rendered_prompt"]
    assert "规则二" not in child["rendered_prompt"]


def test_gradients_expose_the_reasoning_and_the_verdict(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    """The gradient timeline is what makes an optimiser's advice reviewable."""

    artifact_id = _seed_artifact(db_session_factory)
    test_client.post(
        f"{_ARTIFACTS}/{artifact_id}/decisions",
        headers=auth_headers["admin_t1"],
        json={"decisions": [{"patch_id": "a" * 32, "decision": "accepted"}]},
    )

    response = test_client.get(
        "/api/v1/prompt-lab/gradients",
        headers=auth_headers["inspector_t1"],
        params={"artifact_id": artifact_id, "decision": "accepted"},
    )

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["patch_id"] for item in items] == ["a" * 32]
    assert items[0]["gradient_text"].startswith("诊断")
    assert items[0]["evaluation"] == {"support": 6}


def test_another_tenant_cannot_read_the_artifact(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    artifact_id = _seed_artifact(db_session_factory)

    response = test_client.get(
        f"{_ARTIFACTS}/{artifact_id}",
        headers=auth_headers["inspector_t2"],
    )

    assert response.status_code == 404, "absence and another tenant must look identical"

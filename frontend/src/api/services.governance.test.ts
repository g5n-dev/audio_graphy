import { beforeEach, describe, expect, expectTypeOf, it, vi } from "vitest";
import type {
  CreateTagJobRequest,
  FreezeTagGoldSetRequest,
  TagJobScope,
} from "@/types/api";

vi.mock("./client", () => ({
  httpClient: {
    get: vi.fn(),
    post: vi.fn(),
  },
}));

import { httpClient } from "./client";
import {
  adjudicateTagReview,
  cancelTagOptimizationRun,
  cancelTagJob,
  compareTagOptimizationTrials,
  createTagOptimizationRun,
  createTagDeployment,
  createTagEvaluation,
  createTagJob,
  createTagReviewBatch,
  decideTagReview,
  getTagEvolutionOverview,
  getTagOptimizationRun,
  getTagFactLineage,
  approveTagDeployment,
  freezeTagGoldSet,
  listTagBadcases,
  listTagDeploymentObservations,
  listTagOptimizationRuns,
  listTagReviews,
  listTagSchemas,
  releaseTagReview,
  resumeTagDeployment,
  rollbackTagDeployment,
} from "./services";

const mockedGet = httpClient.get as unknown as ReturnType<typeof vi.fn>;
const mockedPost = httpClient.post as unknown as ReturnType<typeof vi.fn>;

describe("tag governance services", () => {
  beforeEach(() => {
    mockedGet.mockReset();
    mockedPost.mockReset();
    mockedGet.mockResolvedValue({ data: { items: [], total: 0 } });
    mockedPost.mockResolvedValue({ data: { id: 1 } });
  });

  it("uses the versioned governance resource paths", async () => {
    await listTagSchemas();
    await listTagReviews({ status: "pending" });

    expect(mockedGet).toHaveBeenNthCalledWith(1, "/tag-schemas");
    expect(mockedGet).toHaveBeenNthCalledWith(2, "/tag-reviews", {
      params: { status: "pending" },
    });
  });

  it("releases only the current review claim without a force override", async () => {
    await releaseTagReview(501);

    expect(mockedPost).toHaveBeenCalledWith("/tag-reviews/501/release", {
      force: false,
    });
  });

  it("loads the complete lineage bundle for a canonical fact", async () => {
    await getTagFactLineage(701);
    expect(mockedGet).toHaveBeenCalledWith("/tag-facts/701/lineage");
  });

  it("sends the idempotency key separately from a bounded job scope", async () => {
    expectTypeOf<CreateTagJobRequest>().toEqualTypeOf<{
      job_type: "extract" | "recompute";
      scope: TagJobScope;
    }>();

    await createTagJob(
      {
        job_type: "recompute",
        scope: { reception_ids: [101], group_ids: ["model@v2"] },
      },
      "insight-rerun-stable",
    );

    expect(mockedPost).toHaveBeenCalledWith(
      "/tag-jobs",
      {
        job_type: "recompute",
        scope: { reception_ids: [101], group_ids: ["model@v2"] },
      },
      { headers: { "Idempotency-Key": "insight-rerun-stable" } },
    );
  });

  it("uses the explicit cancellation endpoint for a running tag job", async () => {
    await cancelTagJob(88);
    expect(mockedPost).toHaveBeenCalledWith("/tag-jobs/88/cancel");
  });

  it("starts evaluation asynchronously without accepting client-authored metrics", async () => {
    await createTagEvaluation(
      {
        tagger_version_id: 42,
        gold_set_version_id: 7,
        baseline_tagger_version_id: 41,
      },
      "evaluation-stable-key",
    );

    expect(mockedPost).toHaveBeenCalledWith(
      "/tag-evaluations",
      {
        tagger_version_id: 42,
        gold_set_version_id: 7,
        baseline_tagger_version_id: 41,
      },
      { headers: { "Idempotency-Key": "evaluation-stable-key" } },
    );
  });

  it("keeps structured review decisions aligned with the stable endpoint", async () => {
    await createTagReviewBatch({
      reason: "conflict",
      subjects: [
        {
          subject_type: "dialogue_unit",
          subject_id: 77,
          tag_key: "intent",
        },
      ],
    });
    await decideTagReview(501, {
      action: "correct",
      truth_state: "present",
      corrected_value: "purchase",
      primary_failure_stage: "tag_reasoning",
      reason_code: "model_misread",
      reason_codes: ["model_misread", "evidence_confirmed"],
      reviewer_confidence: 0.9,
      evidence_refs: [],
    });

    expect(mockedPost).toHaveBeenNthCalledWith(
      1,
      "/tag-reviews/create-batch",
      {
        reason: "conflict",
        subjects: [
          {
            subject_type: "dialogue_unit",
            subject_id: 77,
            tag_key: "intent",
          },
        ],
      },
    );
    expect(mockedPost).toHaveBeenNthCalledWith(
      2,
      "/tag-reviews/501/decide",
      {
        action: "correct",
        truth_state: "present",
        corrected_value: "purchase",
        primary_failure_stage: "tag_reasoning",
        reason_code: "model_misread",
        reason_codes: ["model_misread", "evidence_confirmed"],
        reviewer_confidence: 0.9,
        evidence_refs: [],
      },
    );
  });

  it("uses the explicit endpoint for third-round adjudication", async () => {
    await adjudicateTagReview(501, {
      action: "correct",
      truth_state: "present",
      corrected_value: "purchase",
      primary_failure_stage: "tag_reasoning",
      reason_code: "model_misread",
      reason_codes: ["model_misread"],
      reviewer_confidence: 0.95,
      evidence_refs: [],
    });

    expect(mockedPost).toHaveBeenCalledWith("/tag-reviews/501/adjudicate", {
      action: "correct",
      truth_state: "present",
      corrected_value: "purchase",
      primary_failure_stage: "tag_reasoning",
      reason_code: "model_misread",
      reason_codes: ["model_misread"],
      reviewer_confidence: 0.95,
      evidence_refs: [],
    });
    expect(mockedPost.mock.calls.at(-1)?.[1]).not.toHaveProperty("truth_tier");
    expect(mockedPost.mock.calls.at(-1)?.[1]).not.toHaveProperty(
      "annotator_round",
    );
    expect(mockedPost.mock.calls.at(-1)?.[1]).not.toHaveProperty(
      "adjudication",
    );
  });

  it("uses server-bound evolution resources without client-authored errors or baseline ids", async () => {
    await getTagEvolutionOverview();
    await listTagBadcases({ limit: 25 });
    await listTagOptimizationRuns();
    await getTagOptimizationRun(71);
    await compareTagOptimizationTrials(71, 11, 12);
    await cancelTagOptimizationRun(71);
    await createTagOptimizationRun({
      cohort: {
        source: "tag_insights",
        filters: { store_ids: ["S1"], scenarios: ["automotive"] },
      },
      target_policy: { policy: "balanced" },
      search_budget: { max_trials: 24, sealed_holdout_queries: 1 },
    });

    expect(mockedGet).toHaveBeenNthCalledWith(1, "/tag-evolution/overview");
    expect(mockedGet).toHaveBeenNthCalledWith(2, "/tag-badcases", {
      params: { limit: 25 },
    });
    expect(mockedGet).toHaveBeenNthCalledWith(3, "/tag-optimization-runs");
    expect(mockedGet).toHaveBeenNthCalledWith(
      4,
      "/tag-optimization-runs/71",
    );
    expect(mockedPost).toHaveBeenCalledWith(
      "/tag-optimization-runs/71/compare",
      { left_trial_id: 11, right_trial_id: 12 },
    );
    expect(mockedPost).toHaveBeenCalledWith(
      "/tag-optimization-runs/71/cancel",
    );
    expect(mockedPost).toHaveBeenCalledWith("/tag-optimization-runs", {
      cohort: {
        source: "tag_insights",
        filters: { store_ids: ["S1"], scenarios: ["automotive"] },
      },
      target_policy: { policy: "balanced" },
      search_budget: { max_trials: 24, sealed_holdout_queries: 1 },
    });
    expect(mockedPost.mock.calls.at(-1)?.[1]).not.toHaveProperty(
      "error_samples",
    );
    expect(mockedPost.mock.calls.at(-1)?.[1]).not.toHaveProperty(
      "production_tagger_version_id",
    );
    expect(mockedPost.mock.calls.at(-1)?.[1]).not.toHaveProperty(
      "gold_set_version_id",
    );
    expect(mockedPost.mock.calls.at(-1)?.[1]).not.toHaveProperty("objective");
    expect(mockedPost.mock.calls.at(-1)?.[1]).not.toHaveProperty("trigger");
  });

  it("freezes only a server-resolved cohort with a complete checklist", async () => {
    const body: FreezeTagGoldSetRequest = {
      version: "2026.07",
      cohort: {
        review_bundle_ids: ["release-2026-07"],
        truth_tiers: ["t2", "t3"],
        subject_types: ["dialogue_unit", "reception"],
      },
      completeness_checklist: {
        full_applicable_matrix: true,
        frozen_input_snapshots: true,
        reception_level_isolation: true,
        t2_t3_truth_only: true,
      },
    };

    await freezeTagGoldSet(5, body);

    expect(mockedPost).toHaveBeenCalledWith(
      "/tag-gold-sets/5/freeze",
      body,
    );
    expect(mockedPost.mock.calls.at(-1)?.[1]).not.toHaveProperty(
      "decision_ids",
    );
  });

  it("creates deployments without a gate override escape hatch", async () => {
    const body = {
      tagger_version_id: 42,
      evaluation_run_id: 7,
      baseline_tagger_version_id: 41,
    };

    await createTagDeployment(body);

    expect(mockedPost).toHaveBeenCalledWith("/tag-deployments", body);
    expect(mockedPost.mock.calls.at(-1)?.[1]).not.toHaveProperty(
      "override_reason",
    );
  });

  it("uses revision CAS for production approval, rollback and drift resume", async () => {
    await approveTagDeployment(9, 4);
    await rollbackTagDeployment(9, "canary quality gate regressed", 5);
    await resumeTagDeployment(9, "分布漂移复核完成，确认可以恢复推进", 6);
    expect(mockedPost).toHaveBeenNthCalledWith(
      1,
      "/tag-deployments/9/approve",
      undefined,
      { headers: { "If-Match": "4" } },
    );
    expect(mockedPost).toHaveBeenCalledWith(
      "/tag-deployments/9/rollback",
      { reason: "canary quality gate regressed" },
      { headers: { "If-Match": "5" } },
    );
    expect(mockedPost).toHaveBeenCalledWith(
      "/tag-deployments/9/resume",
      { reason: "分布漂移复核完成，确认可以恢复推进" },
      { headers: { "If-Match": "6" } },
    );
  });

  it("loads bounded deployment observations", async () => {
    await listTagDeploymentObservations(9, 50);
    expect(mockedGet).toHaveBeenCalledWith(
      "/tag-deployments/9/observations",
      { params: { limit: 50 } },
    );
  });
});

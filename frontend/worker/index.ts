import {
  demoExplore,
  demoRecordings,
  demoReceptions,
  demoStateInsights,
  demoStats,
  demoTagAnalysis,
  demoTagInsights,
  demoTopicClusters,
  demoUser,
  demoWorkspace,
} from "./demoData";
import {
  ensureDemoSchema,
  getRecord,
  listAuditEvents,
  listRecords,
  nextRecordId,
  putRecordWithAudit,
  putRecordsWithAudit,
  type D1Database,
} from "./durableState";

interface AssetsBinding {
  fetch(request: Request): Promise<Response>;
}

export interface Env {
  /**
   * Sites serves static assets ahead of the API worker in production. The
   * binding is optional in Vite's local Workers runtime, where returning 404
   * lets the client environment continue with its SPA fallback.
   */
  ASSETS?: AssetsBinding;
  /** Durable application state for the hosted demonstration. */
  DB?: D1Database;
}

const apiPrefix = "/api/v1";
const jsonHeaders = {
  "Content-Type": "application/json; charset=utf-8",
  "Cache-Control": "no-store",
};
const demoTenantId = "tenant-demo";

// 开放接口密钥的演示态:预置一把在用的、一把已吊销的,签发的追加在后。
const demoApiKeys: Array<{
  id: number;
  name: string;
  active: boolean;
  created_at: string;
  last_used_at: string | null;
}> = [
  {
    id: 1,
    name: "crm-sync",
    active: true,
    created_at: "2026-07-30T09:00:00Z",
    last_used_at: "2026-08-05T01:30:00Z",
  },
  {
    id: 2,
    name: "bi-export(已轮换)",
    active: false,
    created_at: "2026-07-21T09:00:00Z",
    last_used_at: "2026-07-29T18:00:00Z",
  },
];
const canonicalTargetLabels = [
  "stage",
  "intent",
  "objection",
  "next_step",
  "compliance_risk",
] as const;

type DemoRecord = Record<string, unknown> & { id: number };

interface MutableDemoTagInsights {
  tenant_id: string;
  selected_group_ids: string[];
  total_assignments: number;
  assignment_count: number;
  evidence_summary_total: number;
  evidence_summary_count: number;
  evidence_summary: Array<{
    reception_id: number;
    dialogue_unit_id: number;
    group_id: string;
    label_key: string;
    label_value: string;
    confidence: number | null;
    evidence_count: number;
    evidence_refs: Array<Record<string, unknown>>;
  }>;
  insights: {
    groups: Array<{
      group_key: string;
      version: string;
      group_id: string;
      source: string;
      priority: number;
    }>;
    overview: { assignment_count: number };
    distributions: Array<{
      group_key: string;
      label_key: string;
      value: string;
      count: number;
      proportion: number;
    }>;
  };
  generated_at: string;
  [key: string]: unknown;
}

const demoGovernanceSeeds: Record<string, DemoRecord[]> = {
  "tag-schemas": [
    {
      id: 1,
      tenant_id: demoTenantId,
      key: "sales-reception",
      name: "销售接待标准标签",
      description: "覆盖阶段、意图、异议、下一步和合规风险的标准体系。",
      created_at: "2026-07-18T03:00:00.000Z",
      updated_at: "2026-07-24T08:30:00.000Z",
      versions: [
        {
          id: 11,
          schema_id: 1,
          version: "v3.2",
          status: "published",
          checksum: "demo-schema-v32",
          definitions: canonicalTargetLabels.map((key) => ({
            key,
            name: key,
            category: "reception",
            value_type: "string",
            allowed_values: [],
            subject_types: ["dialogue_unit", "reception"],
            scenarios: ["gold", "automotive"],
            evidence_required: true,
            critical: key === "compliance_risk",
            threshold: key === "compliance_risk" ? 0.85 : 0.72,
          })),
          created_at: "2026-07-18T03:00:00.000Z",
          updated_at: "2026-07-24T08:30:00.000Z",
          published_at: "2026-07-20T08:00:00.000Z",
        },
      ],
    },
  ],
  "tagger-versions": [
    {
      id: 20,
      tenant_id: demoTenantId,
      schema_version_id: 11,
      version: "hybrid-v3.1",
      engine: "hybrid",
      prompt_content: "销售接待标签抽取演示基线提示词",
      rule_bundle: { compliance_guard: true },
      model_version: "qwen3.6-27b",
      thresholds: { default: 0.74, compliance_risk: 0.87 },
      checksum: "demo-tagger-v31",
      status: "qualified",
      created_at: "2026-07-18T08:00:00.000Z",
      updated_at: "2026-07-20T08:00:00.000Z",
    },
    {
      id: 21,
      tenant_id: demoTenantId,
      schema_version_id: 11,
      version: "hybrid-v3.2",
      engine: "hybrid",
      prompt_content: "销售接待标签抽取演示提示词",
      rule_bundle: { compliance_guard: true },
      model_version: "qwen3.6-27b",
      thresholds: { default: 0.72, compliance_risk: 0.85 },
      checksum: "demo-tagger-v32",
      status: "qualified",
      created_at: "2026-07-20T08:00:00.000Z",
      updated_at: "2026-07-24T08:30:00.000Z",
    },
    {
      id: 22,
      tenant_id: demoTenantId,
      schema_version_id: 11,
      version: "hybrid-v3.3-candidate",
      engine: "hybrid",
      prompt_content: "由冻结金标误差簇生成、等待隐藏集评估的候选提示词",
      rule_bundle: { compliance_guard: true, candidate_error_patterns: {} },
      model_version: "qwen3.6-27b",
      thresholds: { default: 0.73, compliance_risk: 0.86 },
      checksum: "demo-tagger-v33-candidate",
      status: "draft",
      created_at: "2026-07-24T09:00:00.000Z",
      updated_at: "2026-07-24T09:00:00.000Z",
    },
  ],
  "tag-jobs": [
    {
      id: 31,
      tenant_id: demoTenantId,
      job_type: "extract",
      status: "completed",
      scope: {
        reception_ids: [101],
        label_keys: [...canonicalTargetLabels],
      },
      tagger_version_id: 21,
      total_items: 8,
      completed_items: 8,
      failed_items: 0,
      attempt_count: 1,
      max_attempts: 3,
      revision: 1,
      lease_owner: null,
      lease_expires_at: null,
      next_attempt_at: null,
      last_error_code: null,
      last_error_message: null,
      created_at: "2026-07-24T08:00:00.000Z",
      updated_at: "2026-07-24T08:03:00.000Z",
      finished_at: "2026-07-24T08:03:00.000Z",
    },
  ],
  "tag-reviews": [
    {
      id: 41,
      tenant_id: demoTenantId,
      batch_id: "review-demo-01",
      subject_type: "dialogue_unit",
      subject_id: 1006,
      reception_id: 101,
      tag_key: "objection",
      proposed_value: "价格敏感",
      proposed_fact_id: 8012,
      schema_version_id: 11,
      tagger_version_id: 21,
      reason: "low_confidence",
      status: "pending",
      priority: 80,
      claimed_by: null,
      claimed_at: null,
      resolved_at: null,
      created_at: "2026-07-24T08:04:00.000Z",
      updated_at: "2026-07-24T08:04:00.000Z",
      confidence: 0.61,
      evidence_refs: [
        {
          recording_id: 5003,
          segment_id: 7012,
          start_sec: 278,
          end_sec: 286,
          text_excerpt: "这个价格确实超出我们的预算了，能不能再优惠一点？",
        },
      ],
    },
  ],
  "tag-gold-sets": [
    {
      id: 51,
      tenant_id: demoTenantId,
      key: "sales-gold",
      name: "销售接待金标集",
      description: "人工复核通过的销售接待样本。",
      schema_version_id: 11,
      status: "frozen",
      version: "2026.07",
      item_count: 386,
      created_at: "2026-07-19T08:00:00.000Z",
      updated_at: "2026-07-23T08:00:00.000Z",
    },
  ],
  "tag-evaluations": [
    {
      id: 61,
      tenant_id: demoTenantId,
      tagger_version_id: 21,
      baseline_tagger_version_id: 20,
      gold_set_version_id: 51,
      status: "completed",
      passed: true,
      metrics: {
        macro_f1: 0.91,
        critical_recall: 0.96,
        evidence_coverage: 0.98,
        error_rate: 0.004,
      },
      baseline_metrics: {
        macro_f1: 0.86,
        critical_recall: 0.9,
        evidence_coverage: 0.94,
        error_rate: 0.008,
      },
      supported_label_f1: { stage: 0.94, objection: 0.89 },
      baseline_label_f1: { stage: 0.9, objection: 0.81 },
      gates: [
        {
          code: "macro_f1",
          passed: true,
          actual: 0.91,
          threshold: 0.88,
          message: "宏平均 F1 达标",
        },
        {
          code: "critical_recall",
          passed: true,
          actual: 0.96,
          threshold: 0.94,
          message: "关键标签召回率达标",
        },
        {
          code: "error_rate",
          passed: true,
          actual: 0.004,
          threshold: 0.01,
          message: "错误率低于发布上限",
        },
      ],
      started_at: "2026-07-23T08:00:00.000Z",
      finished_at: "2026-07-23T08:06:00.000Z",
      created_by: 1,
      created_at: "2026-07-23T08:00:00.000Z",
      updated_at: "2026-07-23T08:06:00.000Z",
    },
  ],
  "tag-deployments": [
    {
      id: 71,
      tenant_id: demoTenantId,
      tagger_version_id: 21,
      evaluation_run_id: 61,
      baseline_tagger_version_id: 20,
      status: "canary_25",
      traffic_percent: 25,
      revision: 3,
      promotion_paused: false,
      pause_reason: null,
      created_by: 1,
      approved_by: null,
      approved_at: null,
      rolled_back_by: null,
      rolled_back_at: null,
      rollback_reason: null,
      created_at: "2026-07-23T09:00:00.000Z",
      updated_at: "2026-07-24T08:30:00.000Z",
    },
  ],
  "tag-deployment-observations": [
    {
      id: 7202,
      tenant_id: demoTenantId,
      deployment_id: 71,
      stage: "canary_25",
      window_start: "2026-07-24T08:25:00.000Z",
      window_end: "2026-07-24T08:30:00.000Z",
      sample_count: 386,
      metrics: {
        error_rate: 0.006,
        critical_recall: 0.958,
        evidence_coverage: 0.976,
        drift_max_jsd: 0.038,
        drift_paired_sample_count: 386,
        drift_min_paired_samples: 30,
        drift_jsd_threshold: 0.1,
        drift_eligible_tag_count: 1,
        drift_affected_tags: [],
        drift_by_tag: {
          "intent.purchase": {
            jsd: 0.038,
            sample_count: 386,
            eligible: true,
            breached: false,
          },
        },
      },
      breach_codes: [],
      action: "observe",
      is_demo: true,
      data_source: "demo",
      created_at: "2026-07-24T08:30:00.000Z",
      updated_at: "2026-07-24T08:30:00.000Z",
    },
    {
      id: 7201,
      tenant_id: demoTenantId,
      deployment_id: 71,
      stage: "canary_5",
      window_start: "2026-07-24T08:20:00.000Z",
      window_end: "2026-07-24T08:25:00.000Z",
      sample_count: 128,
      metrics: {
        error_rate: 0.008,
        critical_recall: 0.951,
        evidence_coverage: 0.969,
        drift_max_jsd: 0.052,
        drift_paired_sample_count: 128,
        drift_min_paired_samples: 30,
        drift_jsd_threshold: 0.1,
        drift_eligible_tag_count: 1,
        drift_affected_tags: [],
        drift_by_tag: {
          "intent.purchase": {
            jsd: 0.052,
            sample_count: 128,
            eligible: true,
            breached: false,
          },
        },
      },
      breach_codes: [],
      action: "observe",
      is_demo: true,
      data_source: "demo",
      created_at: "2026-07-24T08:25:00.000Z",
      updated_at: "2026-07-24T08:25:00.000Z",
    },
  ],
  "tag-badcases": [
    {
      id: 81,
      tenant_id: demoTenantId,
      subject_type: "dialogue_unit",
      subject_id: 1006,
      tag_key: "intent",
      failure_stage: "tag_reasoning",
      failure_mode: "购买意向被识别为随便看看",
      cluster_key: "intent.purchase.context",
      root_cause: {
        affected_slices: ["automotive / S1"],
        representative_excerpt: "今天价格合适就签",
      },
      status: "open",
      occurrence_count: 37,
      regression_result: { status: "pending" },
      fix_candidate_tagger_version_id: 22,
      first_seen_at: "2026-07-22T06:00:00.000Z",
      last_seen_at: "2026-07-24T08:30:00.000Z",
      resolved_at: null,
      updated_at: "2026-07-24T08:30:00.000Z",
      data_source: "demo",
    },
  ],
  /**
   * rendered_prompt 必须严格等于「header + 已采纳补丁（按 ordinal, patch_id 排序）
   * + 示例：+ 各示例」用 \n\n 拼接的结果——前端按同一规则逆向切块来做补丁归属，
   * 拼错会让演示站的归属重建返回 exact:false，差异页就看不到补丁标识。
   */
  "prompt-lab/artifacts": [
    {
      id: 301,
      tenant_id: demoTenantId,
      compilation_id: 9001,
      optimization_run_id: null,
      baseline_tagger_version_id: 21,
      gold_set_version_id: 51,
      parent_artifact_id: null,
      candidate_tagger_version_id: null,
      compiler: "builtin",
      compiler_version: "builtin-proposer-v1",
      metric_version: "prompt-lab-metric-v1",
      status: "draft",
      baseline_prompt: "基线规则：依据 schema 与 segments 判定有文本依据的标签。",
      header: "基线规则：依据 schema 与 segments 判定有文本依据的标签。",
      rendered_prompt: "基线规则：依据 schema 与 segments 判定有文本依据的标签。\n\n标签「price」在 7 个已复核样本上被漏判。若某个 segment 明确支持该标签，即使表述间接也应输出并引用该 segment。\n\n标签「compliance_risk」在 5 个已复核样本上证据引用不当。输出该标签时，evidence_segment_ids 必须且只能包含直接支持该判定的 segment。\n\n示例：\n\n示例：顾问说「这个月有活动，落地价能到十九万八」，应输出 price 并引用该 segment。",
      patches: [
        {
          patch_id: "a1b2c3d4e5f60718293a4b5c6d7e8f90",
          kind: "rule_clarification",
          origin: "builtin",
          ordinal: 1,
          body: "标签「price」在 7 个已复核样本上被漏判。若某个 segment 明确支持该标签，即使表述间接也应输出并引用该 segment。",
          rationale: "聚类 tag_reasoning:price:missed_label 共 7 例，复核理由 missed_label。",
          target_tag_keys: ["price"],
          gradient_text: null,
          source_badcase_ids: [71],
          source_gold_label_ids: [],
        },
        {
          patch_id: "b2c3d4e5f60718293a4b5c6d7e8f90a1",
          kind: "constraint_add",
          origin: "builtin",
          ordinal: 2,
          body: "标签「compliance_risk」在 5 个已复核样本上证据引用不当。输出该标签时，evidence_segment_ids 必须且只能包含直接支持该判定的 segment。",
          rationale: "聚类 evidence:compliance_risk:wrong_segment 共 5 例。",
          target_tag_keys: ["compliance_risk"],
          gradient_text: null,
          source_badcase_ids: [72],
          source_gold_label_ids: [],
        },
      ],
      demos: [
        {
          demo_id: "d4e5f60718293a4b5c6d7e8f90a1b2c3",
          gold_label_id: 501,
          subject_type: "dialogue_unit",
          subject_id: 4021,
          rendered_text: "示例：顾问说「这个月有活动，落地价能到十九万八」，应输出 price 并引用该 segment。",
          redaction_mode: "synthetic",
          source_checksum: "f0e1d2c3b4a5968778695a4b3c2d1e0f9a8b7c6d5e4f30211223344556677889",
          reception_id: 3001,
          segment_ids: [88011, 88012],
          recording_ids: [7001],
        },
      ],
      accepted_patch_ids: ["a1b2c3d4e5f60718293a4b5c6d7e8f90", "b2c3d4e5f60718293a4b5c6d7e8f90a1"],
      prompt_token_estimate: 268,
      input_budget_report: {
        prompt_tokens: 268,
        schema_tokens: 214,
        fixed_tokens: 482,
        usable_tokens: 10800,
        headroom_tokens: 10318,
        baseline_fixed_tokens: 246,
        baseline_headroom_tokens: 10554,
        headroom_delta: -236,
        headroom_shrink_ratio: 0.0224,
        fits: true,
      },
      redaction_report: { demo_count: 1, by_redaction_mode: { synthetic: 1 } },
      artifact_checksum: "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f809",
      created_at: "2026-08-02T09:15:00Z",
      updated_at: "2026-08-02T09:15:00Z",
    },
    {
      id: 302,
      tenant_id: demoTenantId,
      compilation_id: 9001,
      optimization_run_id: null,
      baseline_tagger_version_id: 21,
      gold_set_version_id: 51,
      parent_artifact_id: 301,
      candidate_tagger_version_id: 22,
      compiler: "builtin",
      compiler_version: "builtin-proposer-v1",
      metric_version: "prompt-lab-metric-v1",
      status: "review",
      baseline_prompt: "基线规则：依据 schema 与 segments 判定有文本依据的标签。",
      header: "基线规则：依据 schema 与 segments 判定有文本依据的标签。",
      rendered_prompt: "基线规则：依据 schema 与 segments 判定有文本依据的标签。\n\n标签「price」在 7 个已复核样本上被漏判。若某个 segment 明确支持该标签，即使表述间接也应输出并引用该 segment。\n\n示例：\n\n示例：顾问说「这个月有活动，落地价能到十九万八」，应输出 price 并引用该 segment。",
      patches: [
        {
          patch_id: "a1b2c3d4e5f60718293a4b5c6d7e8f90",
          kind: "rule_clarification",
          origin: "builtin",
          ordinal: 1,
          body: "标签「price」在 7 个已复核样本上被漏判。若某个 segment 明确支持该标签，即使表述间接也应输出并引用该 segment。",
          rationale: "聚类 tag_reasoning:price:missed_label 共 7 例。",
          target_tag_keys: ["price"],
          gradient_text: null,
          source_badcase_ids: [71],
          source_gold_label_ids: [],
        },
      ],
      demos: [
        {
          demo_id: "d4e5f60718293a4b5c6d7e8f90a1b2c3",
          gold_label_id: 501,
          subject_type: "dialogue_unit",
          subject_id: 4021,
          rendered_text: "示例：顾问说「这个月有活动，落地价能到十九万八」，应输出 price 并引用该 segment。",
          redaction_mode: "synthetic",
          source_checksum: "f0e1d2c3b4a5968778695a4b3c2d1e0f9a8b7c6d5e4f30211223344556677889",
          reception_id: 3001,
          segment_ids: [88011, 88012],
          recording_ids: [7001],
        },
      ],
      accepted_patch_ids: ["a1b2c3d4e5f60718293a4b5c6d7e8f90"],
      prompt_token_estimate: 208,
      input_budget_report: {
        prompt_tokens: 268,
        schema_tokens: 214,
        fixed_tokens: 482,
        usable_tokens: 10800,
        headroom_tokens: 10318,
        baseline_fixed_tokens: 246,
        baseline_headroom_tokens: 10554,
        headroom_delta: -236,
        headroom_shrink_ratio: 0.0224,
        fits: true,
      },
      redaction_report: { demo_count: 1, by_redaction_mode: { synthetic: 1 } },
      artifact_checksum: "2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f8091a",
      created_at: "2026-08-02T10:40:00Z",
      updated_at: "2026-08-02T10:40:00Z",
    },
  ],
  "prompt-lab/gradients": [
    {
      id: 401,
      tenant_id: demoTenantId,
      artifact_id: 301,
      patch_id: "a1b2c3d4e5f60718293a4b5c6d7e8f90",
      iteration: 1,
      source_badcase_id: 71,
      tag_key: "price",
      failure_stage: "tag_reasoning",
      failure_mode: "correct:missed_label",
      gradient_text: "当前规则只要求出现「优惠」字样，但这 7 例中金额与「优惠」分处不同 segment，模型未跨句关联。",
      proposed_edit: "标签「price」在 7 个已复核样本上被漏判。若某个 segment 明确支持该标签，即使表述间接也应输出并引用该 segment。",
      decision: "pending",
      decided_by: null,
      decided_at: null,
      decision_note: null,
      evaluation: { source_badcase_count: 7, support: 7 },
      created_at: "2026-08-02T09:15:00Z",
      updated_at: "2026-08-02T09:15:00Z",
    },
    {
      id: 402,
      tenant_id: demoTenantId,
      artifact_id: 301,
      patch_id: "b2c3d4e5f60718293a4b5c6d7e8f90a1",
      iteration: 1,
      source_badcase_id: 72,
      tag_key: "compliance_risk",
      failure_stage: "evidence",
      failure_mode: "correct:wrong_segment",
      gradient_text: "证据指向了顾问的总结句而非客户原话，导致合规判定无法复核。",
      proposed_edit: "标签「compliance_risk」在 5 个已复核样本上证据引用不当。输出该标签时，evidence_segment_ids 必须且只能包含直接支持该判定的 segment。",
      decision: "pending",
      decided_by: null,
      decided_at: null,
      decision_note: null,
      evaluation: { source_badcase_count: 5, support: 5 },
      created_at: "2026-08-02T09:15:00Z",
      updated_at: "2026-08-02T09:15:00Z",
    },
  ],
  "tag-optimization-runs": [
    {
      id: 91,
      tenant_id: demoTenantId,
      job_id: 32,
      status: "completed",
      phase: "completed",
      baseline_tagger_version_id: 21,
      baseline_version: "hybrid-v3.2",
      candidate_tagger_version_id: 22,
      winner_tagger_version_id: 22,
      candidate_version: "hybrid-v3.3-candidate",
      gold_set_version_id: 51,
      dataset_snapshot_hash: "demo-gold-2026-07",
      cohort: {
        source: "eligible_feedback",
        filters: { scenarios: ["automotive"] },
        conflict_only: true,
      },
      objective: { policy: "balanced" },
      search_budget: { max_trials: 24, sealed_holdout_queries: 1 },
      trigger: "scheduled",
      summary: {
        eligible_feedback_count: 236,
        completed_trials: 24,
        trial_count: 24,
        holdout_read: false,
        worker_completed: true,
        candidate_comparison: {
          dimensions: [
            {
              dimension: "orchestration",
              before: "weak_llm",
              after: "weak_then_strong_critic",
            },
          ],
          metric_deltas: {
            macro_f1: 0.024,
            review_rate: -0.031,
            p95_latency_ms: 84,
          },
          improved_badcase_count: 31,
          regressed_badcase_count: 2,
        },
      },
      next_actions: [
        "run_offline_evaluation",
        "review_candidate_before_shadow",
      ],
      artifacts: ["tagger_version:22", "gold_set_version:51"],
      trials: [
        {
          id: 9101,
          ordinal: 1,
          status: "completed",
          phase: "validation",
          mutation: { description: "baseline", dimension: "baseline" },
          reward_vector: { feasible: true, quality_delta: 0 },
          metrics: { macro_f1: 0.86 },
        },
        {
          id: 9102,
          ordinal: 2,
          status: "completed",
          phase: "validation",
          mutation: {
            description: "orchestration.route=weak_then_strong_critic",
            dimension: "orchestration",
          },
          reward_vector: { feasible: true, quality_delta: 0.024 },
          metrics: { macro_f1: 0.884 },
          candidate_tagger_version_id: 22,
        },
      ],
      started_at: "2026-07-24T05:00:00.000Z",
      finished_at: "2026-07-24T05:08:00.000Z",
      created_at: "2026-07-24T05:00:00.000Z",
      updated_at: "2026-07-24T05:08:00.000Z",
      is_demo: true,
      data_source: "demo",
    },
  ],
};

const demoAuditSeed = {
  id: 1,
  tenant_id: demoTenantId,
  action: "tagger.deployment.promoted",
  actor_user_id: 1,
  resource_type: "tag-deployments",
  resource_id: "71",
  detail: { from: "canary_5", to: "canary_25" },
  created_at: "2026-07-24T08:30:00.000Z",
};

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: jsonHeaders,
  });
}

function apiError(status: number, code: string, message: string): Response {
  return json({ error: { code, message, detail: {} } }, status);
}

const demoAudioSampleRate = 16_000;
const demoWavCache = new Map<number, Uint8Array>();

function pcmWav(durationMs: number): Uint8Array {
  const normalizedDurationMs = Math.max(1, Math.round(durationMs));
  const cached = demoWavCache.get(normalizedDurationMs);
  if (cached) return cached;

  const sampleCount = Math.round(
    (normalizedDurationMs * demoAudioSampleRate) / 1_000,
  );
  const dataByteLength = sampleCount * 2;
  const wav = new Uint8Array(44 + dataByteLength);
  const view = new DataView(wav.buffer);
  const writeAscii = (offset: number, value: string) => {
    for (let index = 0; index < value.length; index += 1) {
      wav[offset + index] = value.charCodeAt(index);
    }
  };
  writeAscii(0, "RIFF");
  view.setUint32(4, 36 + dataByteLength, true);
  writeAscii(8, "WAVE");
  writeAscii(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, demoAudioSampleRate, true);
  view.setUint32(28, demoAudioSampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeAscii(36, "data");
  view.setUint32(40, dataByteLength, true);

  // A short, low-amplitude tone proves that this is decodable PCM while the
  // remaining source stays silent and cheap to construct.
  const audibleSamples = Math.min(sampleCount, demoAudioSampleRate / 4);
  for (let index = 0; index < audibleSamples; index += 1) {
    view.setInt16(
      44 + index * 2,
      Math.round(Math.sin((2 * Math.PI * 440 * index) / demoAudioSampleRate) * 800),
      true,
    );
  }
  demoWavCache.set(normalizedDurationMs, wav);
  return wav;
}

function wavResponse(
  request: Request,
  durationMs: number,
  timeOriginMs: number,
  validSourceEndMs: number,
): Response {
  const wav = pcmWav(durationMs);
  const baseHeaders = new Headers({
    "Accept-Ranges": "bytes",
    "Cache-Control": "private, no-store",
    "Content-Type": "audio/wav",
    "X-Audio-Time-Origin-Ms": String(timeOriginMs),
    "X-Audio-Valid-Source-Range-Ms": `${timeOriginMs}-${validSourceEndMs}`,
  });
  const requestedRange = request.headers.get("Range");
  if (!requestedRange) {
    baseHeaders.set("Content-Length", String(wav.byteLength));
    return new Response(wav.buffer as ArrayBuffer, {
      status: 200,
      headers: baseHeaders,
    });
  }

  const match = /^bytes=(\d+)-(\d*)$/.exec(requestedRange.trim());
  if (!match) {
    baseHeaders.set("Content-Range", `bytes */${wav.byteLength}`);
    return new Response(null, { status: 416, headers: baseHeaders });
  }
  const start = Number(match[1]);
  const requestedEnd = match[2] ? Number(match[2]) : wav.byteLength - 1;
  if (
    !Number.isSafeInteger(start) ||
    !Number.isSafeInteger(requestedEnd) ||
    start < 0 ||
    start >= wav.byteLength ||
    requestedEnd < start
  ) {
    baseHeaders.set("Content-Range", `bytes */${wav.byteLength}`);
    return new Response(null, { status: 416, headers: baseHeaders });
  }
  const end = Math.min(requestedEnd, wav.byteLength - 1);
  const body = wav.slice(start, end + 1);
  baseHeaders.set("Content-Length", String(body.byteLength));
  baseHeaders.set("Content-Range", `bytes ${start}-${end}/${wav.byteLength}`);
  return new Response(body.buffer as ArrayBuffer, {
    status: 206,
    headers: baseHeaders,
  });
}

async function payloadHash(payload: unknown): Promise<string> {
  const bytes = new TextEncoder().encode(JSON.stringify(payload));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
}

function persistenceUnavailable(): Response {
  return apiError(
    503,
    "DEMO_PERSISTENCE_UNAVAILABLE",
    "演示站持久化服务暂不可用，本次写入未被接受。",
  );
}

async function requestBody(request: Request): Promise<Record<string, unknown>> {
  return (await request.json().catch(() => ({}))) as Record<string, unknown>;
}

async function governanceRecords(
  env: Env | undefined,
  namespace: string,
): Promise<DemoRecord[]> {
  const merged = new Map<number, DemoRecord>(
    (demoGovernanceSeeds[namespace] ?? []).map((item) => [item.id, item]),
  );
  if (env?.DB) {
    const persisted = await listRecords<DemoRecord>(
      env.DB,
      namespace,
      demoTenantId,
    );
    for (const item of persisted) merged.set(item.id, item);
  }
  return [...merged.values()].sort((left, right) => right.id - left.id);
}

async function governanceRecord(
  env: Env | undefined,
  namespace: string,
  id: number,
): Promise<DemoRecord | null> {
  if (env?.DB) {
    const persisted = await getRecord<DemoRecord>(
      env.DB,
      namespace,
      id,
      demoTenantId,
    );
    if (persisted) return persisted;
  }
  return (
    (demoGovernanceSeeds[namespace] ?? []).find((item) => item.id === id) ??
    null
  );
}

async function publishedTargetLabels(env: Env | undefined): Promise<string[]> {
  const schemas = await governanceRecords(env, "tag-schemas");
  const publishedVersions = schemas
    .flatMap((schema) =>
      Array.isArray(schema.versions)
        ? (schema.versions as Array<Record<string, unknown>>)
        : [],
    )
    .filter((version) => version.status === "published")
    .sort((left, right) => Number(right.id ?? 0) - Number(left.id ?? 0));
  const definitions = publishedVersions[0]?.definitions;
  if (!Array.isArray(definitions)) return [...canonicalTargetLabels];
  const keys = definitions
    .map((definition) =>
      typeof definition === "object" &&
      definition !== null &&
      "key" in definition
        ? String(definition.key)
        : "",
    )
    .filter(Boolean);
  return keys.length > 0 ? [...new Set(keys)] : [...canonicalTargetLabels];
}

async function persistGovernanceRecord(
  env: Env | undefined,
  namespace: string,
  record: DemoRecord,
  action: string,
  detail: Record<string, unknown> = {},
): Promise<Response | null> {
  if (!env?.DB) return persistenceUnavailable();
  await putRecordWithAudit(
    env.DB,
    namespace,
    record.id,
    record,
    action,
    detail,
    demoTenantId,
  );
  return null;
}

async function persistWorkflowAction(
  env: Env | undefined,
  receptionId: number,
  action: string,
  payload: unknown,
): Promise<Response | null> {
  if (!env?.DB) return persistenceUnavailable();
  const recordId = `${receptionId}:${action}`;
  await putRecordWithAudit(
    env.DB,
    "reception-actions",
    recordId,
    {
      reception_id: receptionId,
      action,
      payload,
      updated_at: new Date().toISOString(),
    },
    action,
    { reception_id: receptionId },
    demoTenantId,
  );
  return null;
}

function topicClustersForRequest(url: URL) {
  const requestedJobId = url.searchParams.get("job_id");
  if (
    requestedJobId !== null &&
    Number(requestedJobId) !== demoTopicClusters.job.id
  ) {
    return null;
  }
  const level = Number(url.searchParams.get("level") ?? 0);
  const clusters = demoTopicClusters.clusters.map((cluster) => ({
    ...cluster,
    level,
    community_id: cluster.community_id + level * 100,
  }));
  return {
    ...demoTopicClusters,
    level,
    clusters,
  };
}

function workspaceForReception(receptionId: number): typeof demoWorkspace {
  if (receptionId === demoWorkspace.reception.id) return demoWorkspace;
  return {
    ...demoWorkspace,
    reception: {
      ...demoWorkspace.reception,
      id: receptionId,
      external_session_id: `demo-reception-${receptionId}`,
    },
  };
}

async function persistedWorkspaceForReception(
  env: Env | undefined,
  receptionId: number,
): Promise<typeof demoWorkspace> {
  const persisted = env?.DB
    ? await getRecord<typeof demoWorkspace>(
        env.DB,
        "reception-workspaces",
        receptionId,
        demoTenantId,
      )
    : null;
  return persisted ?? workspaceForReception(receptionId);
}

function projectStageTransitions(
  workspace: typeof demoWorkspace,
  subjectId: number,
  stage: string,
  evidenceRefs: (typeof demoWorkspace.tag_assignments)[number]["evidence_refs"],
  now: string,
): void {
  const orderedUnits = [...workspace.dialogue_units].sort(
    (left, right) => left.unit_index - right.unit_index,
  );
  const unitIndex = orderedUnits.findIndex(
    (unit) => Number(unit.id) === subjectId,
  );
  if (unitIndex === -1) return;
  const target = workspace.state_transitions.find(
    (transition) => Number(transition.dialogue_unit_id) === subjectId,
  );
  const nextUnit = orderedUnits[unitIndex + 1];
  const successor = nextUnit
    ? workspace.state_transitions.find(
        (transition) =>
          Number(transition.dialogue_unit_id) === Number(nextUnit.id),
      )
    : undefined;
  const chainIsComplete =
    (unitIndex === 0 || Boolean(target)) &&
    (unitIndex === orderedUnits.length - 1 || Boolean(successor));

  if (chainIsComplete) {
    if (target) {
      target.to_state = stage;
      target.trigger = "manual_tag_correction";
      target.confidence = Math.min(
        1,
        Math.max(0, Number(orderedUnits[unitIndex].boundary_confidence ?? 1)),
      );
      target.evidence_refs = evidenceRefs;
      target.algorithm_version = "manual-tag-correction-v1";
      target.created_at = now;
    }
    if (successor) successor.from_state = stage;
    return;
  }

  const template = workspace.state_transitions[0];
  if (!template) return;
  workspace.state_transitions = orderedUnits.slice(1).map((unit, index) => {
    const previousUnit = orderedUnits[index];
    const existing = workspace.state_transitions.find(
      (transition) => Number(transition.dialogue_unit_id) === Number(unit.id),
    );
    const isTarget = Number(unit.id) === subjectId;
    return {
      ...(existing ?? template),
      id: existing?.id ?? 9801 + index,
      dialogue_unit_id: unit.id,
      sequence_no: index + 1,
      from_state: String(previousUnit.business_stage || "__unknown__"),
      to_state: String(unit.business_stage || "__unknown__"),
      trigger: isTarget
        ? "manual_tag_correction"
        : (existing?.trigger ?? "stage_projection"),
      confidence: isTarget
        ? Math.min(1, Math.max(0, Number(unit.boundary_confidence ?? 1)))
        : (existing?.confidence ?? template.confidence),
      evidence_refs: isTarget ? evidenceRefs : (existing?.evidence_refs ?? []),
      algorithm_version: isTarget
        ? "manual-tag-correction-v1"
        : (existing?.algorithm_version ?? "stage-projection-v1"),
      created_at: isTarget ? now : (existing?.created_at ?? now),
    };
  });
  workspace.window.state_transitions.total = workspace.state_transitions.length;
  workspace.window.state_transitions.returned =
    workspace.state_transitions.length;
}

interface GovernedWorkspaceProjection {
  workspace: typeof demoWorkspace;
  insights: MutableDemoTagInsights;
  assignment: (typeof demoWorkspace.tag_assignments)[number];
  previousAssignmentId: number | null;
}

async function governedWorkspaceProjection(
  env: Env,
  task: DemoRecord,
  factId: number,
  finalValue: unknown,
  evidenceInput: unknown,
  reason: string,
  now: string,
): Promise<GovernedWorkspaceProjection | null> {
  if (!env.DB || task.subject_type !== "dialogue_unit" || finalValue == null) {
    return null;
  }
  const receptionId = Number(task.reception_id);
  const subjectId = Number(task.subject_id);
  if (!Number.isInteger(receptionId) || !Number.isInteger(subjectId)) {
    return null;
  }
  const workspace = structuredClone(
    await persistedWorkspaceForReception(env, receptionId),
  );
  const unit = workspace.dialogue_units.find(
    (item) => Number(item.id) === subjectId,
  );
  if (!unit) return null;

  const labelKey = String(task.tag_key);
  const current =
    workspace.tag_assignments.find(
      (assignment) =>
        Number(assignment.dialogue_unit_id) === subjectId &&
        assignment.label_key === labelKey &&
        assignment.is_current,
    ) ?? null;
  const template = current ?? workspace.tag_assignments[0];
  if (!template) return null;

  const requestedEvidence = Array.isArray(evidenceInput)
    ? (evidenceInput as Array<Record<string, unknown>>)
    : [];
  const taskEvidence = Array.isArray(task.evidence_refs)
    ? (task.evidence_refs as Array<Record<string, unknown>>)
    : [];
  const evidenceSource =
    requestedEvidence.length > 0 ? requestedEvidence : taskEvidence;
  const currentEvidence = new Map(
    (current?.evidence_refs ?? []).map((evidence) => [
      String(evidence.ref_id),
      evidence,
    ]),
  );
  const evidenceRefs = evidenceSource.map((evidence, index) => {
    const refId = String(evidence.ref_id ?? `fact-${factId}-${index + 1}`);
    const existing = currentEvidence.get(refId);
    if (existing) return existing;
    const startMs = Math.round(
      Number(evidence.start_sec ?? evidence.timeline_start_sec ?? 0) * 1_000,
    );
    const endMs = Math.round(
      Number(
        evidence.end_sec ??
          evidence.timeline_end_sec ??
          evidence.start_sec ??
          0,
      ) * 1_000,
    );
    return {
      ref_id: refId,
      kind: evidence.kind === "audio" ? ("audio" as const) : ("text" as const),
      recording_id: Number(evidence.recording_id ?? unit.source_recording_id),
      coordinate_space: "both",
      source_start_ms: startMs,
      source_end_ms: endMs,
      timeline_start_ms: startMs,
      timeline_end_ms: endMs,
      text_excerpt:
        evidence.text_excerpt == null ? "" : String(evidence.text_excerpt),
    };
  });
  const retainedEvidence =
    evidenceRefs.length > 0
      ? evidenceRefs
      : (current?.evidence_refs ?? template.evidence_refs);

  for (const assignment of workspace.tag_assignments) {
    if (
      Number(assignment.dialogue_unit_id) === subjectId &&
      assignment.label_key === labelKey &&
      assignment.is_current
    ) {
      assignment.is_current = false;
    }
  }
  const schemaVersion = Number(task.schema_version_id);
  const taggerVersion = Number(task.tagger_version_id);
  const groupVersion = `schema:${
    Number.isInteger(schemaVersion) ? schemaVersion : "manual"
  }|tagger:${Number.isInteger(taggerVersion) ? taggerVersion : "manual"}`;
  const assignment = {
    ...template,
    id: factId,
    reception_id: receptionId,
    dialogue_unit_id: subjectId,
    group_key: "canonical",
    group_version: groupVersion,
    label_key: labelKey,
    label_value: String(finalValue),
    confidence: 1,
    source: "manual",
    priority: 1_000,
    evidence_refs: retainedEvidence,
    model_run_id: `fact:${factId}`,
    is_current: true,
    assigned_at: now,
  };
  workspace.tag_assignments.push(assignment);

  if (labelKey === "stage") {
    unit.business_stage = String(finalValue);
    unit.version += 1;
    unit.edit_status = "manual_edited";
    projectStageTransitions(
      workspace,
      subjectId,
      String(finalValue),
      retainedEvidence,
      now,
    );
  }

  workspace.reception.version += 1;
  workspace.reception.updated_at = now;
  workspace.window.tag_assignments.total += 1;
  workspace.window.tag_assignments.returned += 1;
  const provenance = workspace.provenance_events as unknown as Array<
    Record<string, unknown>
  >;
  if (current) {
    provenance.push({
      id: await nextRecordId(env.DB, "reception-workspace-provenance", 9901),
      reception_id: receptionId,
      object_type: "dialogue_tag_assignment",
      object_ref: String(current.id),
      event_type: "superseded",
      actor: "user:1",
      algorithm_version: groupVersion,
      parent_refs: [{ type: "tag_assignment_fact", id: factId }],
      evidence_refs: current.evidence_refs,
      payload: {
        reason,
        label_key: labelKey,
        previous_value: current.label_value,
        next_value: String(finalValue),
      },
      occurred_at: now,
    });
  }
  provenance.push({
    id: await nextRecordId(env.DB, "reception-workspace-provenance", 9901),
    reception_id: receptionId,
    object_type: "dialogue_tag_assignment",
    object_ref: String(factId),
    event_type: "edited",
    actor: "user:1",
    algorithm_version: groupVersion,
    parent_refs: current
      ? [{ type: "dialogue_tag_assignment", id: current.id }]
      : [],
    evidence_refs: retainedEvidence,
    payload: {
      reason,
      label_key: labelKey,
      previous_value: current?.label_value ?? null,
      next_value: String(finalValue),
      fact_id: factId,
    },
    occurred_at: now,
  });
  workspace.window.provenance_events.total += current ? 2 : 1;
  workspace.window.provenance_events.returned += current ? 2 : 1;

  const storedInsights = await getRecord<MutableDemoTagInsights>(
    env.DB,
    "reception-tag-insights",
    "current",
    demoTenantId,
  );
  const insights = structuredClone(
    storedInsights ?? (demoTagInsights as unknown as MutableDemoTagInsights),
  );
  const groupId = `canonical@${groupVersion}`;
  if (!insights.selected_group_ids.includes(groupId)) {
    insights.selected_group_ids.unshift(groupId);
  }
  if (!insights.insights.groups.some((group) => group.group_id === groupId)) {
    insights.insights.groups.unshift({
      group_key: "canonical",
      version: groupVersion,
      group_id: groupId,
      source: "manual",
      priority: 1_000,
    });
  }
  const insightEvidence = retainedEvidence.map((evidence) => ({
    ref_id: String(evidence.ref_id),
    kind: evidence.kind,
    recording_id: String(evidence.recording_id),
    start_ms: evidence.timeline_start_ms ?? evidence.source_start_ms ?? null,
    end_ms: evidence.timeline_end_ms ?? evidence.source_end_ms ?? null,
    text_excerpt: evidence.text_excerpt ?? null,
  }));
  insights.evidence_summary.unshift({
    reception_id: receptionId,
    dialogue_unit_id: subjectId,
    group_id: groupId,
    label_key: labelKey,
    label_value: String(finalValue),
    confidence: 1,
    evidence_count: insightEvidence.length,
    evidence_refs: insightEvidence,
  });
  insights.evidence_summary_count = insights.evidence_summary.length;
  insights.evidence_summary_total += 1;
  insights.total_assignments += 1;
  insights.assignment_count += 1;
  insights.insights.overview.assignment_count += 1;
  insights.insights.distributions.unshift({
    group_key: groupId,
    label_key: labelKey,
    value: String(finalValue),
    count: 1,
    proportion: 1,
  });
  insights.generated_at = now;

  return {
    workspace,
    insights,
    assignment,
    previousAssignmentId: current ? Number(current.id) : null,
  };
}

function automationForReception(
  receptionId: number,
  targetLabels: readonly string[] = canonicalTargetLabels,
) {
  return {
    id: 9000 + receptionId,
    reception_id: receptionId,
    status: "ready",
    stage: "ready",
    attempt_count: 1,
    checkpoints: {
      merge: "complete",
      segmentation: "complete",
      tagging: "complete",
    },
    segmentation_algorithm: "dialogue-hybrid-v1",
    tag_group_key: "reception-rules",
    tag_group_version: "rules-v1",
    target_labels: [...targetLabels],
    tag_priority: 0,
    last_error_code: null,
    last_error_message: null,
    created_at: "2026-07-24T01:08:00.000Z",
    updated_at: "2026-07-24T09:00:00.000Z",
    finished_at: "2026-07-24T09:00:00.000Z",
  };
}

function demoReceptionResponse(receptionId = demoWorkspace.reception.id) {
  const workspace = workspaceForReception(receptionId);
  return {
    ...workspace.reception,
    recordings: workspace.recordings,
  };
}

function demoDialogueEdit(receptionId: number) {
  const workspace = workspaceForReception(receptionId);
  return {
    reception_id: receptionId,
    reception_version: workspace.reception.version,
    dialogue_units: workspace.dialogue_units,
  };
}

function demoDiscovery(storeId: string) {
  return {
    items: [
      {
        candidate_type: "merge_group",
        recording_ids: demoWorkspace.recordings
          .slice(0, 2)
          .map((recording) => recording.recording_id),
        decision: "merge",
        confidence: 0.94,
        reasons: [
          {
            code: "temporal_continuity",
            contribution: 0.38,
            detail: "两段录音时间连续，间隔 1.2 秒",
            hard_constraint: false,
          },
          {
            code: "speaker_continuity",
            contribution: 0.32,
            detail: "销售与客户声纹连续",
            hard_constraint: false,
          },
          {
            code: "same_store",
            contribution: 0.24,
            detail: "录音来自同一门店",
            hard_constraint: true,
          },
        ],
        store_id: storeId || demoWorkspace.reception.store_id,
        started_at: demoWorkspace.reception.started_at,
        ended_at: demoWorkspace.reception.ended_at,
        duration_status: "available",
        split_at_sec: null,
        at_segment_id: null,
        proposal_token: null,
        proposal_expires_at: null,
      },
    ],
    total: 1,
    scanned_recordings: demoWorkspace.recordings.length,
    truncated: false,
  };
}

function safeGet(pathname: string, url: URL): Response | null {
  if (pathname === "/recordings") return json(demoRecordings);
  if (pathname === "/receptions") return json(demoReceptions);
  if (pathname === "/graph/explore" || pathname === "/graph/subgraph") {
    const rawEdgeLimit = url.searchParams.get("edge_limit");
    const requestedEdgeLimit =
      rawEdgeLimit === null ? 5_000 : Number(rawEdgeLimit);
    if (
      !Number.isInteger(requestedEdgeLimit) ||
      requestedEdgeLimit < 1 ||
      requestedEdgeLimit > 5_000
    ) {
      return apiError(
        422,
        "VALIDATION_ERROR",
        "edge_limit 必须是 1 到 5000 之间的整数。",
      );
    }
    const edges = demoExplore.edges.slice(0, requestedEdgeLimit);
    return json({
      ...demoExplore,
      edges,
      edge_window: {
        total: demoExplore.edges.length,
        returned: edges.length,
        truncated: edges.length < demoExplore.edges.length,
        render_budget: requestedEdgeLimit,
      },
    });
  }
  if (pathname === "/graph/topic-clusters") {
    const snapshot = topicClustersForRequest(url);
    return snapshot
      ? json(snapshot)
      : apiError(404, "LEIDEN_JOB_NOT_FOUND", "未找到指定的成功聚类任务。");
  }
  const topicClusterDetailMatch =
    /^\/graph\/topic-clusters\/(\d+)\/(\d+)\/(\d+)$/.exec(pathname);
  if (topicClusterDetailMatch) {
    const [, jobIdRaw, levelRaw, communityIdRaw] = topicClusterDetailMatch;
    const jobId = Number(jobIdRaw);
    const level = Number(levelRaw);
    const communityId = Number(communityIdRaw);
    if (jobId !== demoTopicClusters.job.id) {
      return apiError(
        404,
        "LEIDEN_JOB_NOT_FOUND",
        "未找到指定的成功聚类任务。",
      );
    }
    const snapshot = topicClustersForRequest(
      new URL(`https://demo.invalid/?job_id=${jobId}&level=${level}`),
    );
    const cluster = snapshot?.clusters.find(
      (candidate) => candidate.community_id === communityId,
    );
    if (!snapshot || !cluster) {
      return apiError(404, "TOPIC_CLUSTER_NOT_FOUND", "未找到指定主题社区。");
    }
    return json({
      job: snapshot.job,
      cluster,
      related_clusters: snapshot.clusters
        .filter((candidate) => candidate.community_id !== communityId)
        .slice(0, 3),
    });
  }
  if (pathname === "/tags/stats") return json(demoStats);
  if (pathname === "/prompts") return json({ items: [] });
  if (pathname === "/speakers") return json({ items: [], total: 0 });
  if (pathname === "/orchestration/topology") {
    // 演示态镜像后端形状:值取演示部署的 adapter 组合(全 mock),
    // 积压为 0——演示库里没有在跑的任务,编个数字就是假数据。
    const stage = (
      id: string,
      name: string,
      service: string,
      note: string,
      config: [string, string, string][],
      inSchema: string[],
      outSchema: string[],
      mode: string | null = "mock",
      queue = 0,
    ) => ({
      id,
      name,
      service,
      adapter_mode: mode,
      state: mode === "mock" ? "mock" : queue > 10 ? "busy" : "ok",
      queue,
      note,
      config,
      in_schema: inSchema,
      out_schema: outSchema,
    });
    return json({
      stages: [
        stage("ingest", "录音接入", "ingestion",
          "注册/开放接口上传写入 Recording,静态加密与指纹在同一事务内完成。",
          [["单文件上限", "512 MiB", "MAX_RECORDING_AUDIO_BYTES"],
           ["处理并发", "1", "PIPELINE_CONCURRENCY"],
           ["幂等键", "open-upload:<external_ref>", "—"]],
          ["audio: bytes", "store_id: str", "external_ref: str"],
          ["recording_id: int", "audio_sha256: str", "duration_ms: int"], null),
        stage("vad_asr_chunk", "VAD · 转写 · 切分", "silero-vad / funasr / chunker",
          "VAD 定边界,整段转写后按语义与 token 预算切块;mock 模式的切分与语音内容无关。",
          [["VAD 模式", "mock", "ADAPTER_VAD_MODE"],
           ["ASR 模式", "mock", "ADAPTER_ASR_MODE"],
           ["流式 VAD 起始阈值", "0.5", "STREAMING_VAD_ONSET_THRESHOLD"]],
          ["recording_id: int", "audio_uri: str"],
          ["segment_id: int", "start_ms/end_ms: int", "text: str", "chunk_id: int"]),
        stage("voiceprint", "声纹与说话人", "campplus / speaker_linker",
          "抽声纹向量并跨录音归并说话人;两阈值之间进人工待确认,不静默合并。",
          [["合并余弦阈值", "0.5", "VOICEPRINT_COSINE_THRESHOLD"],
           ["免歧义阈值", "0.7", "VOICEPRINT_AMBIGUOUS_THRESHOLD"],
           ["采样分段上限", "8", "VOICEPRINT_SAMPLE_MAX_SEGMENTS"]],
          ["segment_id: int", "audio_slice: bytes"],
          ["speaker_node_id: int", "cosine: float", "ambiguity_tag: str|null"]),
        stage("assemble", "接待组装", "reception_merge",
          "相邻录音组合成一次接待;逻辑合并不改写源文件,合并优先级 显式 > 人工 > 自动。",
          [["合并策略", "显式 > 人工 > 自动", "—"],
           ["声纹一致性", "余弦 ≥ 0.7", "VOICEPRINT_AMBIGUOUS_THRESHOLD"],
           ["候选窗口", "扫描时指定(门店 + 时间窗)", "—"]],
          ["recording_id: int", "speaker_node_id: int"],
          ["reception_id: int", "merge_mode: logical|physical", "merge_confidence: float"], null),
        stage("extract", "标签抽取", "tag_worker",
          "按已发布 Schema 抽取标签事实,证据引用必带;人工更正永不被模型覆写。",
          [["LLM 模式", "mock", "ADAPTER_LLM_MODE"],
           ["强模型", "qwen3.6-27b", "LLM_STRONG_MODEL"],
           ["弱模型", "qwen3.6-35b-a3b", "LLM_WEAK_MODEL"],
           ["强/弱并发", "4 / 8", "LLM_STRONG_CONCURRENCY"]],
          ["reception_id: int", "dialogue_unit_id: int"],
          ["tag_fact_id: int", "label_key/value: str", "evidence_refs: []"]),
        stage("graph", "图谱写入", "graph_networkx",
          "实体归一(别名 + 模糊匹配)后写图;跨进程写有文件锁,损坏文件拒载不清空。",
          [["实体模糊阈值", "0.85", "ENTITY_FUZZY_THRESHOLD"],
           ["Embedding 模式", "mock", "ADAPTER_EMBED_MODE"],
           ["边渲染预算", "5000", "GRAPH_EDGE_RENDER_BUDGET"]],
          ["tag_fact_id: int", "entity_candidates: []"],
          ["edge: (src, rel, dst)", "confidence: enum"]),
        stage("leiden", "社区检测", "leiden",
          "图谱快照上的 Leiden 聚类,结果绑定任务 ID;默认关闭,由 ENABLE_ADVANCED_GRAPH 启用。",
          [["启用", "False", "ENABLE_ADVANCED_GRAPH"],
           ["触发阈值", "30.0% 图变更", "LEIDEN_THRESHOLD_PERCENT"]],
          ["graph_snapshot: GraphML"],
          ["leiden_job_id: int", "community_id: int", "level: int"], null),
        stage("index", "向量索引", "mysql_vector",
          "文本块与实体入向量库,供问答双通道检索;音频嵌入(CLAP)单独开关。",
          [["文本 Embedding", "mock", "ADAPTER_EMBED_MODE"],
           ["音频 Embedding", "mock", "ADAPTER_AUDIO_EMBED_MODE"]],
          ["chunk_text: str", "entity_name: str"],
          ["vector_id: int", "namespace: str"]),
      ],
      links: [
        ["ingest", "vad_asr_chunk"], ["ingest", "voiceprint"],
        ["vad_asr_chunk", "voiceprint"], ["voiceprint", "assemble"],
        ["vad_asr_chunk", "assemble"], ["assemble", "extract"],
        ["extract", "graph"], ["graph", "leiden"],
        ["extract", "index"], ["graph", "index"],
      ],
    });
  }
  // ── Open API 密钥列表(演示;签发/吊销在异步主处理器里)──
  if (pathname === "/integration/api-keys") {
    return json({ items: demoApiKeys, total: demoApiKeys.length });
  }
  if (/^\/recordings\/\d+\/speakers$/.test(pathname)) {
    // 声纹归属解析:工作台时间线用它把 speaker_label 升级为「已确认身份」。
    // 缺了这个端点,前端会亮出「说话人身份加载失败」的降级横幅——那是给
    // 真实故障准备的,不该出现在演示里。
    const recordingId = Number(pathname.split("/")[2]);
    return json({
      recording_id: recordingId,
      items: [
        {
          source_speaker_label: "顾问小林",
          speaker_node_id: 9101,
          display_name: "顾问小林",
          speaker_role: "agent",
          ambiguity_tag: null,
          merge_confidence: 0.97,
          cosine_similarity: 0.93,
          strategy: "voiceprint",
        },
        {
          source_speaker_label: "客户",
          speaker_node_id: 9102,
          display_name: "客户",
          speaker_role: "customer",
          ambiguity_tag: null,
          merge_confidence: 0.95,
          cosine_similarity: 0.91,
          strategy: "voiceprint",
        },
      ],
    });
  }
  if (/^\/recordings\/\d+\/segments$/.test(pathname)) {
    return json({
      recording_id: Number(pathname.split("/")[2]),
      items: [],
      total: 0,
      page: 1,
      page_size: 20,
    });
  }
  if (/^\/recordings\/\d+\/tags$/.test(pathname)) {
    return json({
      recording_id: Number(pathname.split("/")[2]),
      view: "current",
      tags: [],
    });
  }
  if (/^\/provenance\/reception\/\d+$/.test(pathname)) {
    return json({
      object_type: "reception",
      object_ref: pathname.split("/").at(-1),
      items: demoWorkspace.provenance_events,
      total: demoWorkspace.provenance_events.length,
      page: 1,
      page_size: 100,
      truncated: false,
    });
  }
  return null;
}

async function handleGovernanceRequest(
  request: Request,
  pathname: string,
  url: URL,
  env: Env | undefined,
): Promise<Response | null> {
  const listNamespaces = new Set([
    "tag-schemas",
    "tagger-versions",
    "tag-jobs",
    "tag-reviews",
    "tag-gold-sets",
    "tag-evaluations",
    "tag-deployments",
    "tag-badcases",
    "tag-optimization-runs",
    // prompt-lab/gradients 刻意不在这里：它要求 artifact_id 必填，走下面的专门分支。
    "prompt-lab/artifacts",
  ]);
  const namespace = pathname.startsWith("/") ? pathname.slice(1) : pathname;

  if (request.method === "GET" && listNamespaces.has(namespace)) {
    let items = await governanceRecords(env, namespace);
    if (namespace === "tag-reviews") {
      const requestedStatus = url.searchParams.get("status");
      if (requestedStatus === "active") {
        items = items.filter(
          (item) => item.status === "pending" || item.status === "claimed",
        );
      } else if (requestedStatus) {
        items = items.filter((item) => item.status === requestedStatus);
      }
    }
    if (namespace === "prompt-lab/artifacts") {
      const status = url.searchParams.get("status");
      const rawLimit = url.searchParams.get("limit");
      const limit = rawLimit === null ? 50 : Number(rawLimit);
      if (!Number.isInteger(limit) || limit < 1 || limit > 200) {
        return apiError(422, "VALIDATION_ERROR", "limit 必须是 1 到 200 之间的整数。");
      }
      items = items
        .filter((item) => !status || item.status === status)
        .slice(0, limit)
        // 与真实接口一致：列表不返回 Prompt 正文。
        .map((item) => {
          const rest = { ...item };
          delete rest.baseline_prompt;
          delete rest.header;
          delete rest.rendered_prompt;
          delete rest.patches;
          delete rest.demos;
          return rest;
        });
      return json({ items, total: items.length });
    }
    if (namespace === "tag-badcases") {
      const status = url.searchParams.get("status");
      const failureStage = url.searchParams.get("failure_stage");
      const tagKey = url.searchParams.get("tag_key");
      items = items.filter(
        (item) =>
          (!status || item.status === status) &&
          (!failureStage || item.failure_stage === failureStage) &&
          (!tagKey || item.tag_key === tagKey),
      );
    }
    return json({ items, total: items.length });
  }

  if (request.method === "GET" && pathname === "/prompt-lab/readiness") {
    // 刻意留两个未达标组合，让覆盖矩阵的三档颜色都能出现。
    return json({
      tenant_id: demoTenantId,
      ready: false,
      gold_label_total: 412,
      silver_label_total: 1180,
      feedback_total: 412,
      feedback_threshold: 200,
      domain_threshold: 30,
      frozen_gold_set_versions: 2,
      pending_artifacts: 1,
      annotation_hours_remaining: 1.5,
      domains: [
        { domain: "dialogue_unit:stage", gold_count: 96, silver_count: 240, feedback_count: 96, meets_threshold: true },
        { domain: "dialogue_unit:intent", gold_count: 88, silver_count: 210, feedback_count: 88, meets_threshold: true },
        { domain: "dialogue_unit:objection", gold_count: 42, silver_count: 160, feedback_count: 42, meets_threshold: true },
        { domain: "dialogue_unit:next_step", gold_count: 34, silver_count: 150, feedback_count: 34, meets_threshold: true },
        { domain: "reception:compliance_risk", gold_count: 12, silver_count: 220, feedback_count: 12, meets_threshold: false },
        { domain: "reception:next_step", gold_count: 26, silver_count: 200, feedback_count: 26, meets_threshold: false },
      ],
      blockers: [
        "domain_support_below_30:reception:compliance_risk",
        "domain_support_below_30:reception:next_step",
      ],
      is_demo: true,
      data_source: "demo",
    });
  }

  if (request.method === "GET" && pathname === "/prompt-lab/gradients") {
    const rawId = url.searchParams.get("artifact_id");
    const artifactId = Number(rawId);
    if (!rawId || !Number.isInteger(artifactId) || artifactId <= 0) {
      return apiError(422, "VALIDATION_ERROR", "artifact_id 必须是正整数。");
    }
    const decision = url.searchParams.get("decision");
    const items = (await governanceRecords(env, "prompt-lab/gradients"))
      .filter((item) => item.artifact_id === artifactId)
      .filter((item) => !decision || item.decision === decision)
      .sort((left, right) => left.id - right.id);
    return json({ items, total: items.length });
  }

  const artifactDiffMatch = /^\/prompt-lab\/artifacts\/(\d+)\/diff$/.exec(pathname);
  if (request.method === "GET" && artifactDiffMatch) {
    const record = await governanceRecord(
      env,
      "prompt-lab/artifacts",
      Number(artifactDiffMatch[1]),
    );
    if (!record) {
      return apiError(404, "PROMPT_LAB_NOT_FOUND", "演示站未提供该产物。");
    }
    const budget = record.input_budget_report as Record<string, number>;
    const promptTokens = Number(record.prompt_token_estimate ?? 0);
    return json({
      artifact_id: record.id,
      status: record.status,
      baseline_prompt: record.baseline_prompt,
      candidate_prompt: record.rendered_prompt,
      patches: record.patches,
      demos: record.demos,
      accepted_patch_ids: record.accepted_patch_ids,
      prompt_token_estimate: promptTokens,
      // 与线上同口径：只有固定开销之间可相减；prompt_token_estimate 只是策略正文。
      fixed_token_delta: budget.fixed_tokens - budget.baseline_fixed_tokens,
      input_budget_report: budget,
      redaction_report: record.redaction_report,
    });
  }

  const artifactGetMatch = /^\/prompt-lab\/artifacts\/(\d+)$/.exec(pathname);
  if (request.method === "GET" && artifactGetMatch) {
    const record = await governanceRecord(
      env,
      "prompt-lab/artifacts",
      Number(artifactGetMatch[1]),
    );
    return record
      ? json(record)
      : apiError(404, "PROMPT_LAB_NOT_FOUND", "演示站未提供该产物。");
  }

  if (request.method === "POST" && pathname === "/prompt-lab/compilations") {
    const body = await requestBody(request);
    const baselineId = Number(body.baseline_tagger_version_id);
    if (!Number.isInteger(baselineId) || baselineId <= 0) {
      return apiError(400, "PROMPT_LAB_INVALID", "baseline_tagger_version_id 必须是正整数。");
    }
    const source = await governanceRecord(env, "prompt-lab/artifacts", 301);
    if (!source || !env?.DB) return persistenceUnavailable();
    const artifactId = await nextRecordId(env.DB, "prompt-lab/artifacts", 400);
    const compilationId = await nextRecordId(env.DB, "prompt-lab-compilations", 9100);
    // 演示站里 worker 是瞬时的：立刻落一份产物，让轮询能观察到它出现。
    const created = {
      ...source,
      id: artifactId,
      compilation_id: compilationId,
      parent_artifact_id: null,
      status: "draft",
      created_at: new Date(0).toISOString(),
      updated_at: new Date(0).toISOString(),
    };
    const failure = await persistGovernanceRecord(
      env,
      "prompt-lab/artifacts",
      created,
      "prompt_lab.compile",
      { baseline_tagger_version_id: baselineId },
    );
    if (failure) return failure;
    for (const gradient of await governanceRecords(env, "prompt-lab/gradients")) {
      if (gradient.artifact_id !== 301) continue;
      const gradientId = await nextRecordId(env.DB, "prompt-lab/gradients", 500);
      await persistGovernanceRecord(
        env,
        "prompt-lab/gradients",
        { ...gradient, id: gradientId, artifact_id: artifactId, decision: "pending" },
        "prompt_lab.gradient",
      );
    }
    return json({ compilation_id: compilationId, job_id: artifactId }, 202);
  }

  const decisionsMatch = /^\/prompt-lab\/artifacts\/(\d+)\/decisions$/.exec(pathname);
  if (request.method === "POST" && decisionsMatch) {
    const parent = await governanceRecord(
      env,
      "prompt-lab/artifacts",
      Number(decisionsMatch[1]),
    );
    if (!parent) {
      return apiError(404, "PROMPT_LAB_NOT_FOUND", "演示站未提供该产物。");
    }
    const body = await requestBody(request);
    const decisions = Array.isArray(body.decisions) ? body.decisions : [];
    if (decisions.length === 0) {
      return apiError(400, "PROMPT_LAB_INVALID", "decisions 不能为空。");
    }
    const patches = (parent.patches ?? []) as Array<Record<string, unknown>>;
    const known = new Set(patches.map((patch) => String(patch.patch_id)));
    const accepted = new Set<string>();
    for (const raw of decisions as Array<Record<string, unknown>>) {
      const patchId = String(raw.patch_id);
      if (!known.has(patchId)) {
        return apiError(400, "PROMPT_LAB_INVALID", `未知的 patch_id：${patchId}`);
      }
      if (raw.decision === "accepted") accepted.add(patchId);
    }
    const dropped = new Set(
      (Array.isArray(body.dropped_demo_ids) ? body.dropped_demo_ids : []).map(String),
    );
    const demos = ((parent.demos ?? []) as Array<Record<string, unknown>>).filter(
      (demo) => !dropped.has(String(demo.demo_id)),
    );
    // 与服务端 render() 同规则重算，否则前端的归属重建会对不上。
    const keptPatches = patches
      .filter((patch) => accepted.has(String(patch.patch_id)))
      .sort((left, right) => {
        const byOrdinal = Number(left.ordinal) - Number(right.ordinal);
        if (byOrdinal !== 0) return byOrdinal;
        return String(left.patch_id) < String(right.patch_id) ? -1 : 1;
      });
    const blocks = [String(parent.header ?? "").trim()]
      .filter(Boolean)
      .concat(keptPatches.map((patch) => String(patch.body).trim()).filter(Boolean));
    const demoTexts = demos
      .map((demo) => String(demo.rendered_text).trim())
      .filter(Boolean);
    if (demoTexts.length > 0) blocks.push("示例：", ...demoTexts);
    if (!env?.DB) return persistenceUnavailable();
    const childId = await nextRecordId(env.DB, "prompt-lab/artifacts", 400);
    const child = {
      ...parent,
      id: childId,
      parent_artifact_id: parent.id,
      status: "review",
      patches: keptPatches,
      demos,
      accepted_patch_ids: [...accepted],
      rendered_prompt: blocks.join("\n\n"),
      created_at: new Date(0).toISOString(),
      updated_at: new Date(0).toISOString(),
    };
    const failure = await persistGovernanceRecord(
      env,
      "prompt-lab/artifacts",
      child,
      "prompt_lab.decisions",
      { parent_artifact_id: parent.id, accepted: [...accepted] },
    );
    if (failure) return failure;
    await persistGovernanceRecord(
      env,
      "prompt-lab/artifacts",
      { ...parent, status: "superseded" },
      "prompt_lab.superseded",
    );
    return json(child, 201);
  }

  if (request.method === "GET" && pathname === "/tag-evolution/overview") {
    return json({
      production_harness: {
        id: 21,
        version: "hybrid-v3.2",
        status: "production",
        updated_at: "2026-07-24T08:30:00.000Z",
      },
      recommended_gold_set_version_id: 51,
      recommended_gold_set_label: "销售接待金标集 · 2026.07",
      quality: {
        unbiased_macro_f1: 0.884,
        critical_recall_lcb: 0.956,
        evidence_iou: 0.81,
        worst_slice_f1: 0.79,
        delta_vs_baseline: 0.024,
      },
      feedback: {
        eligible_count: 236,
        new_since_last_run: 68,
        representative_audit_count: 120,
        adjudicated_count: 44,
        coverage_rate: 0.92,
        next_run_eligible: true,
        blockers: [],
      },
      drift: {
        status: "watch",
        input_psi: 0.12,
        output_jsd: 0.046,
        affected_slices: ["automotive / S1"],
      },
      release: {
        stage: "canary_25",
        served_count: 5_480,
        paired_count: 1_260,
        audited_count: 516,
        adjudicated_count: 88,
        waiting_reasons: ["等待 48 小时时间门禁"],
        promotion_paused: false,
      },
      is_demo: true,
      data_source: "demo",
    });
  }

  if (request.method === "GET" && pathname === "/tag-audit-events") {
    const stored = env?.DB
      ? await listAuditEvents(
          env.DB,
          demoTenantId,
          Number(url.searchParams.get("limit") ?? 100),
        )
      : [];
    const items = [...stored, demoAuditSeed];
    return json({ items, total: items.length });
  }

  const schemaGetMatch = /^\/tag-schemas\/(\d+)$/.exec(pathname);
  if (request.method === "GET" && schemaGetMatch) {
    const record = await governanceRecord(
      env,
      "tag-schemas",
      Number(schemaGetMatch[1]),
    );
    return record
      ? json(record)
      : apiError(404, "TAG_GOVERNANCE_NOT_FOUND", "标签体系不存在。");
  }

  const jobGetMatch = /^\/tag-jobs\/(\d+)$/.exec(pathname);
  if (request.method === "GET" && jobGetMatch) {
    const record = await governanceRecord(
      env,
      "tag-jobs",
      Number(jobGetMatch[1]),
    );
    return record
      ? json(record)
      : apiError(404, "TAG_GOVERNANCE_NOT_FOUND", "标签任务不存在。");
  }

  const optimizationRunGetMatch = /^\/tag-optimization-runs\/(\d+)$/.exec(
    pathname,
  );
  if (request.method === "GET" && optimizationRunGetMatch) {
    const record = await governanceRecord(
      env,
      "tag-optimization-runs",
      Number(optimizationRunGetMatch[1]),
    );
    return record
      ? json(record)
      : apiError(404, "TAG_GOVERNANCE_NOT_FOUND", "优化运行不存在。");
  }

  const factLineageMatch = /^\/tag-facts\/(\d+)\/lineage$/.exec(pathname);
  if (request.method === "GET" && factLineageMatch) {
    const fact = await governanceRecord(
      env,
      "tag-assignment-facts",
      Number(factLineageMatch[1]),
    );
    if (!fact) {
      return apiError(404, "TAG_GOVERNANCE_NOT_FOUND", "标签事实不存在。");
    }
    const currentKey = `${String(fact.subject_type)}:${String(
      fact.subject_id,
    )}:${String(fact.tag_key)}`;
    const current = env?.DB
      ? await getRecord<Record<string, unknown>>(
          env.DB,
          "tag-assignment-currents",
          currentKey,
          demoTenantId,
        )
      : null;
    const schemaVersionId = Number(fact.schema_version_id);
    const schemaVersion =
      (await governanceRecords(env, "tag-schemas"))
        .flatMap((schema) =>
          Array.isArray(schema.versions)
            ? (schema.versions as Array<Record<string, unknown>>)
            : [],
        )
        .find((version) => Number(version.id) === schemaVersionId) ?? null;
    const taggerVersionId = Number(fact.tagger_version_id);
    const taggerVersion =
      (await governanceRecords(env, "tagger-versions")).find(
        (tagger) => Number(tagger.id) === taggerVersionId,
      ) ?? null;
    return json({
      fact,
      is_current: Number(current?.fact_id) === Number(fact.id),
      schema_version: schemaVersion,
      tagger_version: taggerVersion,
      model_version:
        taggerVersion?.model_version == null
          ? null
          : String(taggerVersion.model_version),
      extraction_run: null,
      job: null,
      deployment: null,
    });
  }

  const deploymentObservationsMatch =
    /^\/tag-deployments\/(\d+)\/observations$/.exec(pathname);
  if (request.method === "GET" && deploymentObservationsMatch) {
    const deploymentId = Number(deploymentObservationsMatch[1]);
    const deployment = await governanceRecord(
      env,
      "tag-deployments",
      deploymentId,
    );
    if (!deployment) {
      return apiError(404, "TAG_GOVERNANCE_NOT_FOUND", "部署记录不存在。");
    }
    const requestedLimit = Number(url.searchParams.get("limit") ?? 200);
    const limit = Number.isSafeInteger(requestedLimit)
      ? Math.min(Math.max(requestedLimit, 1), 1_000)
      : 200;
    const items = (await governanceRecords(env, "tag-deployment-observations"))
      .filter((item) => Number(item.deployment_id) === deploymentId)
      .slice(0, limit);
    return json({ items, total: items.length });
  }

  if (request.method !== "POST") return null;
  const isGovernanceMutation =
    pathname.startsWith("/tag-schemas") ||
    pathname.startsWith("/tagger-versions") ||
    pathname.startsWith("/tag-jobs") ||
    pathname.startsWith("/tag-reviews") ||
    pathname.startsWith("/tag-gold-sets") ||
    pathname.startsWith("/tag-evaluations") ||
    pathname.startsWith("/tag-deployments") ||
    pathname.startsWith("/tag-optimization-runs");
  if (!isGovernanceMutation) return null;
  if (!env?.DB) return persistenceUnavailable();

  const body = await requestBody(request);
  const now = new Date().toISOString();

  if (pathname === "/tag-schemas") {
    const id = await nextRecordId(env.DB, "tag-schemas", 100);
    const record: DemoRecord = {
      id,
      tenant_id: demoTenantId,
      key: String(body.key ?? `schema-${id}`),
      name: String(body.name ?? "未命名标签体系"),
      description:
        body.description === undefined ? null : String(body.description),
      created_at: now,
      updated_at: now,
      versions: [],
    };
    await persistGovernanceRecord(
      env,
      "tag-schemas",
      record,
      "tag-schema.created",
    );
    return json(record, 201);
  }

  const schemaVersionMatch = /^\/tag-schemas\/(\d+)\/versions$/.exec(pathname);
  if (schemaVersionMatch) {
    const schemaId = Number(schemaVersionMatch[1]);
    const schema = await governanceRecord(env, "tag-schemas", schemaId);
    if (!schema) {
      return apiError(404, "TAG_GOVERNANCE_NOT_FOUND", "标签体系不存在。");
    }
    const id = await nextRecordId(env.DB, "tag-schema-versions", 200);
    const version = {
      id,
      schema_id: schemaId,
      version: String(body.version ?? `v${id}`),
      status: "draft",
      checksum: `demo-${schemaId}-${id}`,
      definitions: Array.isArray(body.definitions) ? body.definitions : [],
      created_at: now,
      updated_at: now,
      published_at: null,
    };
    const versions = Array.isArray(schema.versions)
      ? [...schema.versions, version]
      : [version];
    const updated = { ...schema, versions, updated_at: now };
    await persistGovernanceRecord(
      env,
      "tag-schemas",
      updated,
      "tag-schema-version.created",
      { version_id: id },
    );
    return json(version, 201);
  }

  const schemaPublishMatch =
    /^\/tag-schemas\/(\d+)\/versions\/(\d+)\/publish$/.exec(pathname);
  if (schemaPublishMatch) {
    const schemaId = Number(schemaPublishMatch[1]);
    const versionId = Number(schemaPublishMatch[2]);
    const schema = await governanceRecord(env, "tag-schemas", schemaId);
    const versions = Array.isArray(schema?.versions)
      ? (schema.versions as Array<Record<string, unknown>>)
      : [];
    const target = versions.find((version) => Number(version.id) === versionId);
    if (!schema || !target) {
      return apiError(404, "TAG_GOVERNANCE_NOT_FOUND", "标签体系版本不存在。");
    }
    const published = {
      ...target,
      status: "published",
      published_at: now,
      updated_at: now,
    };
    const updated = {
      ...schema,
      updated_at: now,
      versions: versions.map((version) =>
        Number(version.id) === versionId ? published : version,
      ),
    };
    await persistGovernanceRecord(
      env,
      "tag-schemas",
      updated,
      "tag-schema-version.published",
      { version_id: versionId },
    );
    return json(published);
  }

  if (pathname === "/tagger-versions") {
    const id = await nextRecordId(env.DB, "tagger-versions", 300);
    const record: DemoRecord = {
      id,
      tenant_id: demoTenantId,
      schema_version_id: Number(body.schema_version_id ?? 11),
      version: String(body.version ?? `candidate-${id}`),
      engine: String(body.engine ?? "hybrid"),
      prompt_content: String(body.prompt_content ?? ""),
      rule_bundle:
        typeof body.rule_bundle === "object" && body.rule_bundle !== null
          ? body.rule_bundle
          : {},
      model_version: String(body.model_version ?? "demo-model"),
      thresholds:
        typeof body.thresholds === "object" && body.thresholds !== null
          ? body.thresholds
          : {},
      checksum: `demo-tagger-${id}`,
      status: "draft",
      created_at: now,
      updated_at: now,
    };
    await persistGovernanceRecord(
      env,
      "tagger-versions",
      record,
      "tagger-version.created",
    );
    return json(record, 201);
  }

  if (pathname === "/tagger-versions/optimize") {
    if (body.error_samples !== undefined) {
      return apiError(
        422,
        "TAG_OPTIMIZATION_CLIENT_SAMPLES_FORBIDDEN",
        "错误样本由冻结金标与复核事实在服务端派生，客户端不得提交。",
      );
    }
    const id = await nextRecordId(env.DB, "tagger-versions", 300);
    const productionVersionId = Number(body.production_tagger_version_id ?? 21);
    const goldSetVersionId = Number(body.gold_set_version_id);
    const [production, goldSet] = await Promise.all([
      governanceRecord(env, "tagger-versions", productionVersionId),
      governanceRecord(env, "tag-gold-sets", goldSetVersionId),
    ]);
    if (!production || !goldSet) {
      return apiError(
        404,
        "TAG_GOVERNANCE_NOT_FOUND",
        "生产 Tagger 或冻结金标版本不存在。",
      );
    }
    if (
      production.status !== "qualified" ||
      goldSet.status !== "frozen" ||
      Number(production.schema_version_id) !== Number(goldSet.schema_version_id)
    ) {
      return apiError(
        409,
        "TAG_OPTIMIZATION_SCOPE_CONFLICT",
        "生产 Tagger 与冻结金标必须达标且属于同一标签体系。",
      );
    }
    const derivedSampleCount = Number(goldSet.item_count ?? 0);
    const candidate: DemoRecord = {
      ...production,
      id,
      version: `${String(production.version)}-candidate-${id}`,
      status: "draft",
      checksum: `demo-optimized-${id}`,
      created_at: now,
      updated_at: now,
    };
    await persistGovernanceRecord(
      env,
      "tagger-versions",
      candidate,
      "tagger-version.optimized",
      {
        production_tagger_version_id: productionVersionId,
        gold_set_version_id: goldSetVersionId,
        derived_sample_count: derivedSampleCount,
        hidden_test_read: false,
      },
    );
    return json(
      {
        candidate,
        optimization: {
          source_tagger_version_id: productionVersionId,
          gold_set_version_id: goldSetVersionId,
          derived_sample_count: derivedSampleCount,
          train_error_summary: {},
          threshold_search: {},
          holdout_read: false,
          data_source: "demo",
          strategy: "server-derived-review-facts-demo",
        },
      },
      201,
    );
  }

  if (pathname === "/tag-optimization-runs") {
    const unexpectedField = Object.keys(body).find(
      (key) => !["cohort", "target_policy", "search_budget"].includes(key),
    );
    if (unexpectedField) {
      return apiError(
        422,
        "TAG_OPTIMIZATION_CLIENT_BINDING_FORBIDDEN",
        `优化创建请求不接受字段 ${unexpectedField}。`,
      );
    }
    const searchBudget =
      typeof body.search_budget === "object" && body.search_budget !== null
        ? (body.search_budget as Record<string, unknown>)
        : {};
    const maxTrials = Number(searchBudget.max_trials ?? 32);
    const sealedHoldoutQueries = Number(
      searchBudget.sealed_holdout_queries ?? 1,
    );
    const cohort =
      typeof body.cohort === "object" && body.cohort !== null
        ? (body.cohort as Record<string, unknown>)
        : {};
    const targetPolicy =
      typeof body.target_policy === "object" && body.target_policy !== null
        ? (body.target_policy as Record<string, unknown>)
        : {};
    if (
      !Number.isSafeInteger(maxTrials) ||
      maxTrials < 1 ||
      maxTrials > 32 ||
      sealedHoldoutQueries !== 1 ||
      typeof cohort.source !== "string" ||
      !cohort.source ||
      !["balanced", "quality_first", "efficiency_guarded"].includes(
        String(targetPolicy.policy ?? ""),
      )
    ) {
      return apiError(
        422,
        "TAG_OPTIMIZATION_REQUEST_INVALID",
        "优化运行需要服务端样本队列、1～32 次有界试验，且隐藏集只允许读取一次。",
      );
    }
    const [goldSets, taggers] = await Promise.all([
      governanceRecords(env, "tag-gold-sets"),
      governanceRecords(env, "tagger-versions"),
    ]);
    const goldSet = [...goldSets]
      .filter((item) => item.status === "frozen")
      .sort((left, right) => Number(right.id) - Number(left.id))
      .find((item) =>
        taggers.some(
          (tagger) =>
            tagger.status === "qualified" &&
            Number(tagger.schema_version_id) ===
              Number(item.schema_version_id),
        ),
      );
    const goldSetVersionId = Number(goldSet?.id);
    const production = [...taggers]
      .filter(
        (tagger) =>
          tagger.status === "qualified" &&
          Number(tagger.schema_version_id) ===
            Number(goldSet?.schema_version_id),
      )
      .sort((left, right) => Number(right.id) - Number(left.id))[0];
    if (!goldSet || goldSet.status !== "frozen" || !production) {
      return apiError(
        409,
        "TAG_OPTIMIZATION_BASELINE_UNAVAILABLE",
        "没有与冻结金标同体系的生产 Harness。",
      );
    }

    const [runId, jobId, candidateId] = await Promise.all([
      nextRecordId(env.DB, "tag-optimization-runs", 1000),
      nextRecordId(env.DB, "tag-jobs", 1000),
      nextRecordId(env.DB, "tagger-versions", 1000),
    ]);
    const candidate: DemoRecord = {
      ...production,
      id: candidateId,
      version: `${String(production.version)}-evolved-${candidateId}`,
      status: "draft",
      origin: "optimizer",
      parent_version_id: production.id,
      optimization_run_id: runId,
      harness_spec_version: "1.0",
      harness_spec: {
        context: {
          neighbor_units: 1,
          example_policy: "hard_negative",
          example_top_k: 3,
        },
        tools: {
          registered_tools: ["rule_engine", "weak_llm", "strong_llm"],
          primary_model: "weak",
          critic_model: "strong",
        },
        generation: {
          temperature: 0,
          max_tokens: 2048,
          response_format: "strict_json",
          prompt_template: String(production.prompt_content ?? ""),
        },
        orchestration: {
          route: "weak_then_strong_critic",
          fusion_policy: "conflict_to_review",
          critic_enabled: true,
          rule_bundle: production.rule_bundle ?? {},
        },
        memory: { policy: "approved_cases", top_k: 3 },
        output: {
          thresholds: production.thresholds ?? {},
          fallback: "review",
          schema_validation: true,
          evidence_validation: true,
          abstain_threshold: 0.4,
          review_threshold: 0.72,
        },
      },
      checksum: `demo-evolved-${runId}`,
      created_at: now,
      updated_at: now,
      is_demo: true,
      data_source: "demo",
    };
    const comparison = {
      dimensions: [
        {
          dimension: "context",
          before: { neighbor_units: 0 },
          after: {
            neighbor_units: 1,
            example_policy: "hard_negative",
            example_top_k: 3,
          },
        },
        {
          dimension: "orchestration",
          before: "rule_llm_fusion",
          after: "weak_then_strong_critic",
        },
      ],
      metric_deltas: {
        macro_f1: 0.021,
        critical_recall_lcb: 0.008,
        evidence_iou: 0.016,
        review_rate: -0.024,
        p95_latency_ms: 76,
        cost_per_1k: 0.14,
      },
      improved_badcase_count: 19,
      regressed_badcase_count: 1,
    };
    const trials = [
      {
        id: runId * 100 + 1,
        ordinal: 1,
        status: "completed",
        phase: "validation",
        mutation: { description: "baseline", dimension: "baseline" },
        reward_vector: { feasible: true, quality_delta: 0 },
        metrics: { macro_f1: 0.863 },
      },
      {
        id: runId * 100 + 2,
        ordinal: 2,
        status: "completed",
        phase: "validation",
        mutation: {
          description: "orchestration.route=weak_then_strong_critic",
          dimension: "orchestration",
        },
        reward_vector: { feasible: true, quality_delta: 0.021 },
        metrics: { macro_f1: 0.884 },
        candidate_tagger_version_id: candidateId,
      },
    ];
    const job: DemoRecord = {
      id: jobId,
      tenant_id: demoTenantId,
      job_type: "optimize",
      status: "completed",
      scope: { optimization_run_id: runId },
      tagger_version_id: production.id,
      total_items: 1,
      completed_items: 1,
      failed_items: 0,
      attempt_count: 1,
      max_attempts: 3,
      revision: 3,
      lease_owner: null,
      lease_expires_at: null,
      next_attempt_at: null,
      last_error_code: null,
      last_error_message: null,
      created_at: now,
      updated_at: now,
      finished_at: now,
      is_demo: true,
      data_source: "demo",
    };
    const run: DemoRecord = {
      id: runId,
      tenant_id: demoTenantId,
      job_id: jobId,
      status: "completed",
      phase: "completed",
      baseline_tagger_version_id: production.id,
      baseline_version: production.version,
      candidate_tagger_version_id: candidateId,
      winner_tagger_version_id: candidateId,
      candidate_version: candidate.version,
      gold_set_version_id: goldSetVersionId,
      dataset_snapshot_hash: `demo-gold-${goldSetVersionId}`,
      cohort,
      objective: targetPolicy,
      search_budget: {
        max_trials: maxTrials,
        sealed_holdout_queries: 1,
      },
      trigger:
        cohort.source === "tag_insights"
          ? "insight"
          : cohort.source === "scheduled"
            ? "scheduled"
            : cohort.source === "feedback_threshold"
              ? "feedback_threshold"
              : "manual",
      summary: {
        eligible_feedback_count: Number(goldSet.item_count ?? 0),
        completed_trials: trials.length,
        trial_count: trials.length,
        holdout_read: false,
        worker_completed: true,
        candidate_comparison: comparison,
        execution_mode: "deterministic_demo",
      },
      next_actions: [
        "run_offline_evaluation",
        "review_candidate_before_shadow",
      ],
      artifacts: [
        `tagger_version:${candidateId}`,
        `gold_set_version:${goldSetVersionId}`,
      ],
      trials,
      candidate_comparison: comparison,
      started_at: now,
      finished_at: now,
      created_at: now,
      updated_at: now,
      is_demo: true,
      data_source: "demo",
    };
    await persistGovernanceRecord(
      env,
      "tagger-versions",
      candidate,
      "tagger-version.evolved-demo",
      { optimization_run_id: runId },
    );
    await persistGovernanceRecord(
      env,
      "tag-jobs",
      job,
      "tag-job.completed-demo",
      { optimization_run_id: runId },
    );
    await persistGovernanceRecord(
      env,
      "tag-optimization-runs",
      run,
      "tag-optimization-run.completed-demo",
      {
        candidate_tagger_version_id: candidateId,
        job_id: jobId,
        holdout_read: false,
      },
    );
    return json(run, 202);
  }

  if (pathname === "/tag-jobs") {
    const allowedBodyKeys = new Set(["job_type", "scope"]);
    const reservedScopeKeys = new Set([
      "blind_mode",
      "deployment_id",
      "evaluation_run_id",
      "holdout_only",
      "optimization_run_id",
      "reason",
      "release_service",
      "review_bundle_id",
      "sampling_probability",
      "sealed_holdout_query",
      "selection_policy",
      "selection_policy_version",
      "source_deployment_id",
      "source_extraction_run_id",
      "source_harness_execution_id",
      "subjects",
      "tagger_version_id",
      "trusted_observation_id",
    ]);
    const jobType = String(body.job_type ?? "");
    const scope =
      typeof body.scope === "object" &&
      body.scope !== null &&
      !Array.isArray(body.scope)
        ? (body.scope as Record<string, unknown>)
        : null;
    const selectedSubjects = [
      Array.isArray(scope?.dialogue_unit_ids) &&
      scope.dialogue_unit_ids.length > 0,
      Array.isArray(scope?.reception_ids) && scope.reception_ids.length > 0,
    ].filter(Boolean).length;
    if (
      !["extract", "recompute"].includes(jobType) ||
      Object.keys(body).some((key) => !allowedBodyKeys.has(key)) ||
      scope === null ||
      Object.keys(scope).some((key) => reservedScopeKeys.has(key)) ||
      selectedSubjects !== 1
    ) {
      return apiError(
        422,
        "TAG_JOB_REQUEST_INVALID",
        "公开标签任务只接受服务端路由的 extract/recompute 范围。",
      );
    }
    const id = await nextRecordId(env.DB, "tag-jobs", 400);
    const totalItems = Object.values(scope).find(Array.isArray)?.length ?? 0;
    const record: DemoRecord = {
      id,
      tenant_id: demoTenantId,
      job_type: jobType,
      status: "queued",
      scope,
      tagger_version_id: null,
      origin: "manual",
      total_items: totalItems,
      completed_items: 0,
      failed_items: 0,
      attempt_count: 0,
      max_attempts: 3,
      revision: 1,
      lease_owner: null,
      lease_expires_at: null,
      next_attempt_at: null,
      last_error_code: null,
      last_error_message: null,
      created_at: now,
      updated_at: now,
      finished_at: null,
    };
    await persistGovernanceRecord(env, "tag-jobs", record, "tag-job.created");
    return json(record, 202);
  }

  const jobActionMatch = /^\/tag-jobs\/(\d+)\/(retry|cancel)$/.exec(pathname);
  if (jobActionMatch) {
    const id = Number(jobActionMatch[1]);
    const action = jobActionMatch[2];
    const record = await governanceRecord(env, "tag-jobs", id);
    if (!record) {
      return apiError(404, "TAG_GOVERNANCE_NOT_FOUND", "标签任务不存在。");
    }
    const updated: DemoRecord = {
      ...record,
      status: action === "retry" ? "queued" : "cancelled",
      attempt_count:
        action === "retry"
          ? Number(record.attempt_count ?? 0) + 1
          : record.attempt_count,
      revision: Number(record.revision ?? 0) + 1,
      updated_at: now,
      finished_at: action === "cancel" ? now : null,
    };
    await persistGovernanceRecord(
      env,
      "tag-jobs",
      updated,
      `tag-job.${action === "retry" ? "retried" : "cancelled"}`,
    );
    return json(updated);
  }

  if (pathname === "/tag-reviews/create-batch") {
    const subjects = Array.isArray(body.subjects)
      ? (body.subjects as Array<Record<string, unknown>>)
      : [];
    const batchId = `review-${Date.now()}`;
    const items: DemoRecord[] = [];
    for (const subject of subjects) {
      const id = await nextRecordId(env.DB, "tag-reviews", 500);
      const subjectType = String(subject.subject_type ?? "dialogue_unit");
      const subjectId = Number(subject.subject_id ?? 0);
      const tagKey = String(subject.tag_key ?? "unknown");
      const currentKey = `${subjectType}:${subjectId}:${tagKey}`;
      const current = await getRecord<Record<string, unknown>>(
        env.DB,
        "tag-assignment-currents",
        currentKey,
        demoTenantId,
      );
      const requestedFactId =
        subject.proposed_fact_id === undefined ||
        subject.proposed_fact_id === null
          ? null
          : Number(subject.proposed_fact_id);
      if (
        requestedFactId !== null &&
        (!Number.isSafeInteger(requestedFactId) || requestedFactId <= 0)
      ) {
        return apiError(
          422,
          "TAG_REVIEW_FACT_INVALID",
          "待复核事实 ID 必须是正整数。",
        );
      }
      const proposedFactId =
        requestedFactId ??
        (current?.fact_id === undefined || current.fact_id === null
          ? null
          : Number(current.fact_id));
      const record: DemoRecord = {
        id,
        tenant_id: demoTenantId,
        batch_id: batchId,
        subject_type: subjectType,
        subject_id: subjectId,
        reception_id:
          subject.reception_id === undefined
            ? null
            : Number(subject.reception_id),
        tag_key: tagKey,
        proposed_value:
          subject.proposed_value === undefined
            ? null
            : String(subject.proposed_value),
        proposed_fact_id: proposedFactId,
        cas_bound: true,
        schema_version_id:
          subject.schema_version_id === undefined
            ? null
            : Number(subject.schema_version_id),
        tagger_version_id:
          subject.tagger_version_id === undefined
            ? null
            : Number(subject.tagger_version_id),
        reason: String(body.reason ?? "random"),
        status: "pending",
        priority: Number(subject.priority ?? 0),
        claimed_by: null,
        claimed_at: null,
        resolved_at: null,
        created_at: now,
        updated_at: now,
        confidence: subject.confidence ?? null,
        evidence_refs: Array.isArray(subject.evidence_refs)
          ? subject.evidence_refs
          : [],
      };
      await persistGovernanceRecord(
        env,
        "tag-reviews",
        record,
        "tag-review.created",
        { batch_id: batchId },
      );
      items.push(record);
    }
    return json({ batch_id: batchId, created_count: items.length, items }, 201);
  }

  const reviewActionMatch =
    /^\/tag-reviews\/(\d+)\/(claim|decide|adjudicate)$/.exec(
    pathname,
  );
  if (reviewActionMatch) {
    const id = Number(reviewActionMatch[1]);
    const action = reviewActionMatch[2];
    const record = await governanceRecord(env, "tag-reviews", id);
    if (!record) {
      return apiError(404, "TAG_GOVERNANCE_NOT_FOUND", "复核任务不存在。");
    }
    if (
      action !== "claim" &&
      (body.truth_tier !== undefined ||
        body.annotator_round !== undefined ||
        body.adjudication !== undefined)
    ) {
      return apiError(
        422,
        "TAG_REVIEW_SERVER_DERIVED_FIELDS_FORBIDDEN",
        "truth_tier、annotator_round 与 adjudication 由任务及复核端点派生。",
      );
    }
    const updated: DemoRecord = {
      ...record,
      status: action === "claim" ? "claimed" : "resolved",
      claimed_by: 1,
      claimed_at: record.claimed_at ?? now,
      resolved_at: action === "claim" ? null : now,
      updated_at: now,
    };
    if (action === "claim") {
      await persistGovernanceRecord(
        env,
        "tag-reviews",
        updated,
        "tag-review.claimed",
      );
      return json(updated);
    }
    if (record.status === "resolved") {
      return apiError(
        409,
        "TAG_REVIEW_ALREADY_RESOLVED",
        "复核任务已经处理，不能重复写入事实。",
      );
    }
    const finalValue =
      body.action === "correct"
        ? body.corrected_value
        : body.action === "reject"
          ? null
          : record.proposed_value;
    if (body.action === "correct" && finalValue === undefined) {
      return apiError(
        422,
        "TAG_REVIEW_VALUE_REQUIRED",
        "人工更正必须提供 corrected_value。",
      );
    }
    const currentKey = `${String(record.subject_type)}:${String(
      record.subject_id,
    )}:${String(record.tag_key)}`;
    const previousCurrent = await getRecord<Record<string, unknown>>(
      env.DB,
      "tag-assignment-currents",
      currentKey,
      demoTenantId,
    );
    const expectedFactId =
      record.proposed_fact_id === undefined || record.proposed_fact_id === null
        ? null
        : Number(record.proposed_fact_id);
    const actualFactId =
      previousCurrent?.fact_id === undefined || previousCurrent.fact_id === null
        ? null
        : Number(previousCurrent.fact_id);
    if (record.cas_bound === true && actualFactId !== expectedFactId) {
      return apiError(
        409,
        "TAG_REVIEW_VERSION_CONFLICT",
        "待复核标签已被其他操作更新，请刷新后重试。",
      );
    }
    const factId = await nextRecordId(env.DB, "tag-assignment-facts", 900);
    const decisionId = await nextRecordId(env.DB, "tag-review-decisions", 1000);
    const decision = {
      id: decisionId,
      tenant_id: demoTenantId,
      task_id: id,
      action: String(body.action ?? "accept"),
      corrected_value:
        body.corrected_value === undefined ? null : body.corrected_value,
      reason_code: String(body.reason_code ?? "demo-review"),
      note: body.note === undefined ? null : String(body.note),
      evidence_refs: Array.isArray(body.evidence_refs)
        ? body.evidence_refs
        : [],
      resulting_fact_id: factId,
      reviewer_user_id: 1,
      truth_tier:
        record.truth_tier ?? (action === "adjudicate" ? "t3" : "t1"),
      annotator_round:
        record.reviewer_round ?? (action === "adjudicate" ? 3 : 1),
      adjudication: action === "adjudicate",
      decided_at: now,
      created_at: now,
      updated_at: now,
    };
    const revision = Number(previousCurrent?.revision ?? 0) + 1;
    const factEvidence = Array.isArray(body.evidence_refs)
      ? body.evidence_refs
      : Array.isArray(record.evidence_refs)
        ? record.evidence_refs
        : [];
    const fact = {
      id: factId,
      tenant_id: demoTenantId,
      reception_id:
        record.reception_id === undefined ? null : record.reception_id,
      subject_type: record.subject_type,
      subject_id: record.subject_id,
      source: "manual",
      tag_key: record.tag_key,
      tag_value: finalValue,
      confidence: 1,
      revision,
      schema_version_id: record.schema_version_id,
      tagger_version_id: record.tagger_version_id,
      extraction_run_id: null,
      deployment_id: null,
      input_hash: await payloadHash({
        task_id: id,
        decision_id: decisionId,
        tag_value: finalValue,
        evidence_refs: factEvidence,
      }),
      evidence_refs: factEvidence,
      supersedes_fact_id: previousCurrent?.fact_id ?? null,
      review_decision_id: decisionId,
      created_by: 1,
      created_at: now,
      updated_at: now,
    };
    const current = {
      id: currentKey,
      tenant_id: demoTenantId,
      subject_type: record.subject_type,
      subject_id: record.subject_id,
      tag_key: record.tag_key,
      fact_id: factId,
      revision,
      updated_at: now,
    };
    const projection = await governedWorkspaceProjection(
      env,
      record,
      factId,
      finalValue,
      factEvidence,
      String(body.note ?? body.reason_code ?? "人工复核"),
      now,
    );
    const durableRecords: Array<{
      namespace: string;
      recordId: string | number;
      payload: unknown;
    }> = [
      { namespace: "tag-reviews", recordId: id, payload: updated },
      {
        namespace: "tag-review-decisions",
        recordId: decisionId,
        payload: decision,
      },
      {
        namespace: "tag-assignment-facts",
        recordId: factId,
        payload: fact,
      },
      {
        namespace: "tag-assignment-currents",
        recordId: currentKey,
        payload: current,
      },
    ];
    if (projection && record.reception_id != null) {
      durableRecords.push(
        {
          namespace: "reception-workspaces",
          recordId: Number(record.reception_id),
          payload: projection.workspace,
        },
        {
          namespace: "reception-tag-insights",
          recordId: "current",
          payload: projection.insights,
        },
      );
    }
    await putRecordsWithAudit(
      env.DB,
      durableRecords,
      "tag-review.decided",
      {
        task_id: id,
        decision_id: decisionId,
        resulting_fact_id: factId,
        reception_id: record.reception_id ?? null,
        previous_assignment_id: projection?.previousAssignmentId ?? null,
        assignment_id: projection?.assignment.id ?? null,
      },
      demoTenantId,
    );
    return json({
      task: updated,
      decision,
      fact,
    });
  }

  if (pathname === "/tag-gold-sets") {
    const id = await nextRecordId(env.DB, "tag-gold-sets", 600);
    const record: DemoRecord = {
      id,
      tenant_id: demoTenantId,
      key: String(body.key ?? `gold-set-${id}`),
      name: String(body.name ?? "未命名金标集"),
      description:
        body.description === undefined ? null : String(body.description),
      schema_version_id: Number(body.schema_version_id ?? 11),
      status: "draft",
      item_count: Array.isArray(body.items) ? body.items.length : 0,
      created_at: now,
      updated_at: now,
    };
    await persistGovernanceRecord(
      env,
      "tag-gold-sets",
      record,
      "tag-gold-set.created",
    );
    return json(record, 201);
  }

  const goldFreezeMatch = /^\/tag-gold-sets\/(\d+)\/freeze$/.exec(pathname);
  if (goldFreezeMatch) {
    const id = Number(goldFreezeMatch[1]);
    const record = await governanceRecord(env, "tag-gold-sets", id);
    if (!record) {
      return apiError(404, "TAG_GOVERNANCE_NOT_FOUND", "金标集不存在。");
    }
    const cohort =
      typeof body.cohort === "object" && body.cohort !== null
        ? (body.cohort as Record<string, unknown>)
        : {};
    const checklist =
      typeof body.completeness_checklist === "object" &&
      body.completeness_checklist !== null
        ? (body.completeness_checklist as Record<string, unknown>)
        : {};
    const reviewBundleIds = Array.isArray(cohort.review_bundle_ids)
      ? cohort.review_bundle_ids
      : [];
    const truthTiers = Array.isArray(cohort.truth_tiers)
      ? cohort.truth_tiers
      : [];
    const subjectTypes = Array.isArray(cohort.subject_types)
      ? cohort.subject_types
      : [];
    const completeChecklist =
      checklist.full_applicable_matrix === true &&
      checklist.frozen_input_snapshots === true &&
      checklist.reception_level_isolation === true &&
      checklist.t2_t3_truth_only === true;
    if (
      body.decision_ids !== undefined ||
      typeof body.version !== "string" ||
      !/^[\w.-]+$/.test(body.version) ||
      reviewBundleIds.length < 1 ||
      reviewBundleIds.length > 1_000 ||
      reviewBundleIds.some(
        (value) => typeof value !== "string" || !value.trim(),
      ) ||
      new Set(reviewBundleIds).size !== reviewBundleIds.length ||
      truthTiers.length < 1 ||
      truthTiers.some((value) => value !== "t2" && value !== "t3") ||
      subjectTypes.length < 1 ||
      subjectTypes.some(
        (value) => value !== "dialogue_unit" && value !== "reception",
      ) ||
      !completeChecklist
    ) {
      return apiError(
        422,
        "TAG_GOLD_FREEZE_INVALID",
        "冻结金标只能提交服务端可解析的复核 Cohort，且四项完整性检查必须全部确认。",
      );
    }
    const updated: DemoRecord = {
      ...record,
      status: "frozen",
      version: body.version,
      cohort: {
        review_bundle_ids: reviewBundleIds,
        truth_tiers: truthTiers,
        subject_types: subjectTypes,
      },
      completeness_manifest: {
        complete: true,
        review_bundle_ids: reviewBundleIds,
        truth_tiers: truthTiers,
        subject_types: subjectTypes,
        ...checklist,
      },
      item_count: Number(record.item_count ?? 0),
      updated_at: now,
    };
    await persistGovernanceRecord(
      env,
      "tag-gold-sets",
      updated,
      "tag-gold-set.frozen",
    );
    return json(updated, 201);
  }

  if (pathname === "/tag-evaluations") {
    const taggerVersionId = Number(body.tagger_version_id);
    const goldSetVersionId = Number(body.gold_set_version_id);
    const baselineTaggerVersionId = Number(body.baseline_tagger_version_id);
    if (
      !Number.isSafeInteger(taggerVersionId) ||
      taggerVersionId <= 0 ||
      !Number.isSafeInteger(goldSetVersionId) ||
      goldSetVersionId <= 0 ||
      !Number.isSafeInteger(baselineTaggerVersionId) ||
      baselineTaggerVersionId <= 0
    ) {
      return apiError(
        422,
        "TAG_EVALUATION_INVALID",
        "候选、基线抽取版本和冻结金标集版本均为必填正整数。",
      );
    }
    if (taggerVersionId === baselineTaggerVersionId) {
      return apiError(
        409,
        "TAG_EVALUATION_CONFLICT",
        "候选抽取版本不能与基线版本相同。",
      );
    }
    const idempotencyKey =
      request.headers.get("Idempotency-Key")?.trim() ||
      `evaluation-${await payloadHash(body)}`;
    const existing = await getRecord<Record<string, unknown>>(
      env.DB,
      "tag-evaluation-idempotency",
      idempotencyKey,
      demoTenantId,
    );
    if (existing) {
      const prior = existing.evaluation as Record<string, unknown> | undefined;
      if (
        Number(prior?.tagger_version_id) !== taggerVersionId ||
        Number(prior?.gold_set_version_id) !== goldSetVersionId ||
        Number(prior?.baseline_tagger_version_id) !== baselineTaggerVersionId
      ) {
        return apiError(
          409,
          "IDEMPOTENCY_KEY_CONFLICT",
          "该 Idempotency-Key 已用于另一组评估输入。",
        );
      }
      return json(existing, 202);
    }

    const [candidate, baseline, goldSet] = await Promise.all([
      governanceRecord(env, "tagger-versions", taggerVersionId),
      governanceRecord(env, "tagger-versions", baselineTaggerVersionId),
      governanceRecord(env, "tag-gold-sets", goldSetVersionId),
    ]);
    if (!candidate || !baseline || !goldSet) {
      return apiError(
        404,
        "TAG_GOVERNANCE_NOT_FOUND",
        "候选、基线抽取版本或冻结金标集不存在。",
      );
    }
    if (!["draft", "validating"].includes(String(candidate.status))) {
      return apiError(
        409,
        "TAG_EVALUATION_CONFLICT",
        "只有草稿或验证中的候选版本可以进入评估。",
      );
    }
    if (baseline.status !== "qualified") {
      return apiError(
        409,
        "TAG_EVALUATION_CONFLICT",
        "基线抽取版本必须已通过质量门禁。",
      );
    }
    if (
      Number(candidate.schema_version_id) !==
        Number(baseline.schema_version_id) ||
      Number(candidate.schema_version_id) !== Number(goldSet.schema_version_id)
    ) {
      return apiError(
        409,
        "TAG_EVALUATION_CONFLICT",
        "候选、基线与冻结金标集必须绑定同一 Schema 版本。",
      );
    }
    if (goldSet.status !== "frozen") {
      return apiError(
        409,
        "TAG_EVALUATION_CONFLICT",
        "评估必须使用已冻结的金标集版本。",
      );
    }

    const evaluationId = await nextRecordId(env.DB, "tag-evaluations", 700);
    const evaluation: DemoRecord = {
      id: evaluationId,
      tenant_id: demoTenantId,
      tagger_version_id: taggerVersionId,
      gold_set_version_id: goldSetVersionId,
      baseline_tagger_version_id: baselineTaggerVersionId,
      status: "queued",
      passed: false,
      metrics: {},
      baseline_metrics: {},
      supported_label_f1: {},
      baseline_label_f1: {},
      gates: [],
      started_at: now,
      finished_at: null,
      created_by: 1,
      created_at: now,
      updated_at: now,
    };
    await persistGovernanceRecord(
      env,
      "tagger-versions",
      { ...candidate, status: "validating", updated_at: now },
      "tagger-version.validating",
      { evaluation_run_id: evaluationId },
    );
    const jobId = await nextRecordId(env.DB, "tag-jobs", 400);
    const job: DemoRecord = {
      id: jobId,
      tenant_id: demoTenantId,
      job_type: "evaluate",
      status: "queued",
      scope: {
        evaluation_run_ids: [evaluationId],
        gold_set_version_ids: [evaluation.gold_set_version_id],
      },
      tagger_version_id: evaluation.tagger_version_id,
      total_items: 1,
      completed_items: 0,
      failed_items: 0,
      attempt_count: 0,
      max_attempts: 3,
      revision: 1,
      lease_owner: null,
      lease_expires_at: null,
      next_attempt_at: null,
      last_error_code: null,
      last_error_message: null,
      created_at: now,
      updated_at: now,
      finished_at: null,
    };
    await persistGovernanceRecord(
      env,
      "tag-evaluations",
      evaluation,
      "tag-evaluation.queued",
      { job_id: jobId },
    );
    await persistGovernanceRecord(env, "tag-jobs", job, "tag-job.created", {
      evaluation_run_id: evaluationId,
    });
    const response = { job_id: jobId, evaluation };
    await putRecordWithAudit(
      env.DB,
      "tag-evaluation-idempotency",
      idempotencyKey,
      response,
      "tag-evaluation.idempotency-bound",
      { evaluation_run_id: evaluationId, job_id: jobId },
      demoTenantId,
    );
    return json(response, 202);
  }

  if (pathname === "/tag-deployments") {
    if (body.override_reason !== undefined) {
      return apiError(
        422,
        "TAG_DEPLOYMENT_GATE_OVERRIDE_FORBIDDEN",
        "部署质量门禁不可人工覆盖。",
      );
    }
    const taggerVersionId = Number(body.tagger_version_id);
    const evaluationRunId = Number(body.evaluation_run_id);
    const baselineTaggerVersionId = Number(body.baseline_tagger_version_id);
    if (
      !Number.isSafeInteger(taggerVersionId) ||
      taggerVersionId <= 0 ||
      !Number.isSafeInteger(evaluationRunId) ||
      evaluationRunId <= 0 ||
      !Number.isSafeInteger(baselineTaggerVersionId) ||
      baselineTaggerVersionId <= 0
    ) {
      return apiError(
        422,
        "TAG_DEPLOYMENT_INPUT_INVALID",
        "候选、评估和回滚基线 ID 均为必填正整数。",
      );
    }
    if (taggerVersionId === baselineTaggerVersionId) {
      return apiError(
        409,
        "TAG_DEPLOYMENT_BASELINE_CONFLICT",
        "回滚基线不能与候选抽取版本相同。",
      );
    }
    const [taggerVersions, evaluation] = await Promise.all([
      governanceRecords(env, "tagger-versions"),
      governanceRecord(env, "tag-evaluations", evaluationRunId),
    ]);
    const candidate = taggerVersions.find(
      (item) => item.id === taggerVersionId,
    );
    const baseline = taggerVersions.find(
      (item) => item.id === baselineTaggerVersionId,
    );
    if (!candidate || !baseline || !evaluation) {
      return apiError(
        404,
        "TAG_GOVERNANCE_NOT_FOUND",
        "候选、评估或回滚基线不存在。",
      );
    }
    if (
      Number(candidate.schema_version_id) !== Number(baseline.schema_version_id)
    ) {
      return apiError(
        409,
        "TAG_DEPLOYMENT_SCHEMA_CONFLICT",
        "回滚基线与候选抽取版本必须使用同一标签体系版本。",
      );
    }
    if (
      candidate.status !== "qualified" ||
      baseline.status !== "qualified" ||
      Number(evaluation.tagger_version_id) !== taggerVersionId ||
      evaluation.passed !== true
    ) {
      return apiError(
        409,
        "TAG_DEPLOYMENT_GATE_FAILED",
        "演示部署要求候选、基线已达标，且评估已通过并绑定当前候选。",
      );
    }
    const id = await nextRecordId(env.DB, "tag-deployments", 800);
    const record: DemoRecord = {
      id,
      tenant_id: demoTenantId,
      tagger_version_id: taggerVersionId,
      evaluation_run_id: evaluationRunId,
      baseline_tagger_version_id: baselineTaggerVersionId,
      status: "shadow",
      traffic_percent: 0,
      revision: 1,
      promotion_paused: false,
      pause_reason: null,
      created_by: 1,
      approved_by: null,
      approved_at: null,
      rolled_back_by: null,
      rolled_back_at: null,
      rollback_reason: null,
      created_at: now,
      updated_at: now,
    };
    await persistGovernanceRecord(
      env,
      "tag-deployments",
      record,
      "tag-deployment.created",
    );
    return json(record, 201);
  }

  if (/^\/tag-deployments\/\d+\/promote$/.test(pathname)) {
    return apiError(
      403,
      "TAG_DEPLOYMENT_PROMOTION_MONITOR_ONLY",
      "Shadow 与 Canary 仅允许可信 Monitor 自动晋级。",
    );
  }

  const deploymentActionMatch =
    /^\/tag-deployments\/(\d+)\/(approve|rollback|resume)$/.exec(pathname);
  if (deploymentActionMatch) {
    const id = Number(deploymentActionMatch[1]);
    const action = deploymentActionMatch[2];
    const record = await governanceRecord(env, "tag-deployments", id);
    if (!record) {
      return apiError(404, "TAG_GOVERNANCE_NOT_FOUND", "部署记录不存在。");
    }
    const ifMatch = request.headers.get("If-Match");
    if (!ifMatch) {
      return apiError(
        428,
        "TAG_DEPLOYMENT_PRECONDITION_REQUIRED",
        "发布动作必须携带当前 If-Match revision。",
      );
    }
    const normalizedRevision = ifMatch
      .trim()
      .replace(/^W\//, "")
      .replace(/^"|"$/g, "");
    if (!/^[1-9]\d*$/.test(normalizedRevision)) {
      return apiError(
        400,
        "TAG_DEPLOYMENT_REVISION_INVALID",
        "If-Match 必须是正整数 revision。",
      );
    }
    const expectedRevision = Number(normalizedRevision);
    const currentRevision = Number(record.revision);
    if (expectedRevision !== currentRevision) {
      return apiError(
        409,
        "TAG_DEPLOYMENT_REVISION_CONFLICT",
        "部署已被其他操作更新，请刷新后重试。",
      );
    }
    const currentStatus = String(record.status);
    if (
      (action === "approve" && currentStatus !== "awaiting_admin") ||
      (action === "rollback" &&
        ["rolled_back", "retired"].includes(currentStatus)) ||
      (action === "resume" &&
        (!record.promotion_paused ||
          record.pause_reason !== "distribution drift requires review" ||
          !["shadow", "canary_5", "canary_25", "awaiting_admin"].includes(
            currentStatus,
          )))
    ) {
      return apiError(
        409,
        "TAG_DEPLOYMENT_TRANSITION_CONFLICT",
        "当前发布状态不允许执行该动作。",
      );
    }
    const rollbackReason =
      action === "rollback" ? String(body.reason ?? "").trim() : "";
    const resumeReason =
      action === "resume" ? String(body.reason ?? "").trim() : "";
    if (action === "rollback" && !rollbackReason) {
      return apiError(
        422,
        "TAG_DEPLOYMENT_ROLLBACK_REASON_REQUIRED",
        "回滚原因不能为空。",
      );
    }
    if (action === "resume" && !resumeReason) {
      return apiError(
        422,
        "TAG_DEPLOYMENT_RESUME_REASON_REQUIRED",
        "管理员复核结论不能为空。",
      );
    }
    const status =
      action === "rollback"
        ? "rolled_back"
        : action === "approve"
          ? "production"
          : currentStatus;
    const trafficByStatus: Record<string, number> = {
      shadow: 0,
      canary_5: 5,
      canary_25: 25,
      awaiting_admin: 25,
      production: 100,
      rolled_back: 0,
    };
    const updated: DemoRecord = {
      ...record,
      status,
      traffic_percent: trafficByStatus[status] ?? 0,
      revision: currentRevision + 1,
      ...(action === "approve" ? { approved_by: 1, approved_at: now } : {}),
      ...(action === "rollback"
        ? {
            rolled_back_by: 1,
            rolled_back_at: now,
            rollback_reason: rollbackReason,
          }
        : {}),
      ...(action === "resume"
        ? {
            promotion_paused: false,
            pause_reason: null,
          }
        : {}),
      updated_at: now,
    };
    await persistGovernanceRecord(
      env,
      "tag-deployments",
      updated,
      `tag-deployment.${action}`,
      action === "rollback" || action === "resume" ? body : {},
    );
    return json(updated);
  }

  return null;
}

interface DemoAudioPlanSource {
  mapping_id: number;
  recording_id: number;
  sequence_no: number;
  source_start_ms: number;
  source_end_ms: number;
  gap_before_ms: number;
  timeline_start_ms: number;
  timeline_end_ms: number;
}

interface DemoAudioPlan {
  plan_token: string;
  reception_id: number;
  expected_version: number;
  timeline_revision: number;
  total_duration_ms: number;
  physical_eligible: boolean;
  warnings: string[];
  sources: DemoAudioPlanSource[];
}

interface DemoAudioOperation {
  id: number;
  reception_id: number;
  status: string;
  mode: string;
  progress: number;
  error: string | null;
  error_code: string | null;
  error_message: string | null;
  plan_token: string;
  created_at: string;
  updated_at: string;
}

async function createDemoAudioPlan(
  request: Request,
  env: Env | undefined,
  receptionId: number,
): Promise<Response> {
  if (!env?.DB) return persistenceUnavailable();
  const body = await requestBody(request);
  const workspace = await persistedWorkspaceForReception(env, receptionId);
  const expectedVersion = Number(body.expected_version);
  if (
    !Number.isSafeInteger(expectedVersion) ||
    expectedVersion !== workspace.reception.version
  ) {
    return apiError(
      409,
      "RECEPTION_VERSION_CONFLICT",
      "接待版本已变化，请保留草稿并刷新后重放。",
    );
  }
  if (!Array.isArray(body.sources) || body.sources.length === 0) {
    return apiError(
      422,
      "AUDIO_PLAN_SOURCES_INVALID",
      "音频计划必须包含至少一个来源映射。",
    );
  }

  const seenMappings = new Set<number>();
  const plannedSources: DemoAudioPlanSource[] = [];
  let timelineCursorMs = 0;
  for (const [index, rawSource] of body.sources.entries()) {
    if (
      typeof rawSource !== "object" ||
      rawSource === null ||
      Array.isArray(rawSource)
    ) {
      return apiError(
        422,
        "AUDIO_PLAN_SOURCE_INVALID",
        "来源映射格式无效。",
      );
    }
    const sourceInput = rawSource as Record<string, unknown>;
    const mappingId = Number(sourceInput.mapping_id);
    const gapBeforeMs = Number(sourceInput.gap_before_ms);
    if (
      !Number.isSafeInteger(mappingId) ||
      seenMappings.has(mappingId) ||
      !Number.isSafeInteger(gapBeforeMs) ||
      gapBeforeMs < 0 ||
      (index === 0 && gapBeforeMs !== 0)
    ) {
      return apiError(
        422,
        "AUDIO_PLAN_GEOMETRY_INVALID",
        "映射必须唯一、空档必须为非负整数毫秒且首段空档为 0。",
      );
    }
    const mapping = workspace.recordings.find(
      (candidate) => candidate.id === mappingId,
    );
    if (!mapping || mapping.source_end_sec === null) {
      return apiError(
        422,
        "AUDIO_PLAN_MAPPING_INVALID",
        "来源映射不存在或尚未完成媒体探测。",
      );
    }
    const sourceStartMs = Math.round(mapping.source_start_sec * 1_000);
    const sourceEndMs = Math.round(mapping.source_end_sec * 1_000);
    if (
      !Number.isSafeInteger(sourceStartMs) ||
      !Number.isSafeInteger(sourceEndMs) ||
      sourceStartMs < 0 ||
      sourceEndMs <= sourceStartMs
    ) {
      return apiError(
        422,
        "AUDIO_PLAN_SOURCE_RANGE_INVALID",
        "来源切片不在已验证媒体范围内。",
      );
    }
    const timelineStartMs = timelineCursorMs + gapBeforeMs;
    const timelineEndMs = timelineStartMs + sourceEndMs - sourceStartMs;
    plannedSources.push({
      mapping_id: mappingId,
      recording_id: mapping.recording_id,
      sequence_no: index,
      source_start_ms: sourceStartMs,
      source_end_ms: sourceEndMs,
      gap_before_ms: gapBeforeMs,
      timeline_start_ms: timelineStartMs,
      timeline_end_ms: timelineEndMs,
    });
    seenMappings.add(mappingId);
    timelineCursorMs = timelineEndMs;
  }

  const unsignedPlan = {
    reception_id: receptionId,
    expected_version: expectedVersion,
    timeline_revision: workspace.reception.version + 1,
    total_duration_ms: timelineCursorMs,
    physical_eligible: true,
    warnings: [] as string[],
    sources: plannedSources,
  };
  const planToken = `demo-plan-${(
    await payloadHash(unsignedPlan)
  ).slice(0, 32)}`;
  const plan: DemoAudioPlan = { plan_token: planToken, ...unsignedPlan };
  await putRecordWithAudit(
    env.DB,
    "reception-audio-plans",
    planToken,
    plan,
    "reception.audio-plan.created",
    {
      reception_id: receptionId,
      expected_version: expectedVersion,
      total_duration_ms: timelineCursorMs,
    },
    demoTenantId,
  );
  return json(plan);
}

async function createDemoAudioOperation(
  request: Request,
  env: Env | undefined,
  receptionId: number,
): Promise<Response> {
  if (!env?.DB) return persistenceUnavailable();
  const idempotencyKey = request.headers.get("Idempotency-Key")?.trim();
  if (!idempotencyKey) {
    return apiError(
      428,
      "IDEMPOTENCY_KEY_REQUIRED",
      "创建音频任务必须提供 Idempotency-Key。",
    );
  }
  const replay = await getRecord<DemoAudioOperation>(
    env.DB,
    "reception-audio-operation-idempotency",
    idempotencyKey,
    demoTenantId,
  );
  if (replay) return json(replay, 202);

  const body = await requestBody(request);
  const planToken = String(body.plan_token ?? "");
  const mode = String(body.mode ?? "");
  const expectedVersion = Number(body.expected_version);
  const workspace = await persistedWorkspaceForReception(env, receptionId);
  const plan = await getRecord<DemoAudioPlan>(
    env.DB,
    "reception-audio-plans",
    planToken,
    demoTenantId,
  );
  if (!plan || plan.reception_id !== receptionId) {
    return apiError(422, "AUDIO_PLAN_INVALID", "音频计划不存在或已过期。");
  }
  if (
    !["logical", "physical", "both"].includes(mode) ||
    !Number.isSafeInteger(expectedVersion)
  ) {
    return apiError(422, "AUDIO_OPERATION_INVALID", "音频任务参数无效。");
  }
  if (
    expectedVersion !== workspace.reception.version ||
    expectedVersion !== plan.expected_version
  ) {
    return apiError(
      409,
      "RECEPTION_VERSION_CONFLICT",
      "音频计划基于旧接待版本，请刷新后重新预览。",
    );
  }

  const now = new Date().toISOString();
  const operation: DemoAudioOperation = {
    id: await nextRecordId(env.DB, "reception-audio-operations", 7001),
    reception_id: receptionId,
    status: "queued",
    mode,
    progress: 0,
    error: null,
    error_code: null,
    error_message: null,
    plan_token: planToken,
    created_at: now,
    updated_at: now,
  };
  await putRecordsWithAudit(
    env.DB,
    [
      {
        namespace: "reception-audio-operations",
        recordId: operation.id,
        payload: operation,
      },
      {
        namespace: "reception-audio-operation-idempotency",
        recordId: idempotencyKey,
        payload: operation,
      },
    ],
    "reception.audio-operation.queued",
    { reception_id: receptionId, operation_id: operation.id, mode },
    demoTenantId,
  );
  return json(operation, 202);
}

async function completeDemoAudioOperation(
  env: Env | undefined,
  receptionId: number,
  operationId: number,
): Promise<Response> {
  if (!env?.DB) return persistenceUnavailable();
  const operation = await getRecord<DemoAudioOperation>(
    env.DB,
    "reception-audio-operations",
    operationId,
    demoTenantId,
  );
  if (!operation || operation.reception_id !== receptionId) {
    return apiError(404, "AUDIO_OPERATION_NOT_FOUND", "音频任务不存在。");
  }
  if (operation.status !== "queued") return json(operation);

  const plan = await getRecord<DemoAudioPlan>(
    env.DB,
    "reception-audio-plans",
    operation.plan_token,
    demoTenantId,
  );
  if (!plan) {
    return apiError(409, "AUDIO_PLAN_MISSING", "音频计划已失效。");
  }
  const workspace = structuredClone(
    await persistedWorkspaceForReception(env, receptionId),
  );
  if (operation.mode !== "physical") {
    workspace.recordings = plan.sources.map((source) => {
      const mapping = workspace.recordings.find(
        (candidate) => candidate.id === source.mapping_id,
      );
      if (!mapping) {
        throw new Error(`Missing demo audio mapping ${source.mapping_id}`);
      }
      return {
        ...mapping,
        sequence_no: source.sequence_no,
        timeline_start_sec: source.timeline_start_ms / 1_000,
        timeline_end_sec: source.timeline_end_ms / 1_000,
        source_start_sec: source.source_start_ms / 1_000,
        source_end_sec: source.source_end_ms / 1_000,
        gap_before_sec: source.gap_before_ms / 1_000,
        decision_source: "manual" as const,
      };
    });
    workspace.window.reception_duration_sec = plan.total_duration_ms / 1_000;
    workspace.window.end_sec = Math.min(
      workspace.window.start_sec + workspace.window.size_sec,
      plan.total_duration_ms / 1_000,
    );
  }
  workspace.reception.version += 1;
  workspace.reception.ended_at = new Date(
    Date.parse(workspace.reception.started_at) + plan.total_duration_ms,
  ).toISOString();
  workspace.reception.updated_at = new Date().toISOString();
  if (operation.mode === "physical" || operation.mode === "both") {
    workspace.reception.audio_url =
      `/api/v1/receptions/${receptionId}/audio?artifact=op-${operationId}`;
  }

  const completed: DemoAudioOperation = {
    ...operation,
    status: "succeeded",
    progress: 1,
    updated_at: workspace.reception.updated_at,
  };
  await putRecordsWithAudit(
    env.DB,
    [
      {
        namespace: "reception-audio-operations",
        recordId: operationId,
        payload: completed,
      },
      {
        namespace: "reception-workspaces",
        recordId: receptionId,
        payload: workspace,
      },
    ],
    "reception.audio-operation.succeeded",
    {
      reception_id: receptionId,
      operation_id: operationId,
      timeline_revision: plan.timeline_revision,
    },
    demoTenantId,
  );
  return json(completed);
}

async function cancelDemoAudioOperation(
  env: Env | undefined,
  receptionId: number,
  operationId: number,
): Promise<Response> {
  if (!env?.DB) return persistenceUnavailable();
  const operation = await getRecord<DemoAudioOperation>(
    env.DB,
    "reception-audio-operations",
    operationId,
    demoTenantId,
  );
  if (!operation || operation.reception_id !== receptionId) {
    return apiError(404, "AUDIO_OPERATION_NOT_FOUND", "音频任务不存在。");
  }
  if (operation.status !== "queued") {
    return apiError(
      409,
      "AUDIO_OPERATION_NOT_CANCELLABLE",
      "任务已提交或结束，无法取消。",
    );
  }
  const cancelled: DemoAudioOperation = {
    ...operation,
    status: "cancelled",
    updated_at: new Date().toISOString(),
  };
  await putRecordWithAudit(
    env.DB,
    "reception-audio-operations",
    operationId,
    cancelled,
    "reception.audio-operation.cancelled",
    { reception_id: receptionId, operation_id: operationId },
    demoTenantId,
  );
  return json(cancelled);
}

export default {
  async fetch(request: Request, env?: Env): Promise<Response> {
    const url = new URL(request.url);
    if (!url.pathname.startsWith(`${apiPrefix}/`)) {
      return env?.ASSETS?.fetch(request) ?? new Response(null, { status: 404 });
    }

    const pathname = url.pathname.slice(apiPrefix.length);
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204 });
    }
    if (env?.DB) {
      try {
        await ensureDemoSchema(env.DB);
      } catch {
        return persistenceUnavailable();
      }
    }

    if (request.method === "POST" && pathname === "/integration/api-keys") {
      // 演示态只在内存:刷新 worker 即复位,不落 D1。
      const body = (await request.json().catch(() => ({}))) as { name?: string };
      const id = demoApiKeys.length + 1;
      const key = {
        id,
        name: body.name ?? `demo-key-${id}`,
        active: true,
        created_at: "2026-08-05T02:00:00Z",
        last_used_at: null,
      };
      demoApiKeys.push(key);
      return json(
        {
          key,
          api_key: `agk_demo${String(id).padStart(4, "0")}${"0".repeat(32)}`,
          webhook_secret: `${"d".repeat(8)}${String(id).padStart(4, "0")}${"e".repeat(52)}`,
        },
        201,
      );
    }
    {
      const revokeMatch = /^\/integration\/api-keys\/(\d+)\/revoke$/.exec(pathname);
      if (revokeMatch && request.method === "POST") {
        const key = demoApiKeys.find((item) => item.id === Number(revokeMatch[1]));
        if (!key) {
          return json(
            { error: { code: "API_KEY_NOT_FOUND", message: "密钥不存在", detail: {} } },
            404,
          );
        }
        key.active = false;
        return json({ key });
      }
    }
    if (request.method === "POST" && pathname === "/auth/login") {
      return json({
        access_token: "demo-access-token",
        refresh_token: "demo-refresh-token",
        token_type: "bearer",
        expires_in: 86_400,
        user: demoUser,
      });
    }
    if (request.method === "GET" && pathname === "/auth/me") {
      return json(demoUser);
    }
    if (request.method === "GET" && pathname === "/reception-state-insights") {
      return json(demoStateInsights);
    }
    if (request.method === "GET" && pathname === "/reception-tag-insights") {
      const persisted = env?.DB
        ? await getRecord<Record<string, unknown>>(
            env.DB,
            "reception-tag-insights",
            "current",
            demoTenantId,
          )
        : null;
      return json(persisted ?? demoTagInsights);
    }
    if (request.method === "POST" && pathname === "/tag-insights/analyze") {
      return json(demoTagAnalysis);
    }

    const governanceResponse = await handleGovernanceRequest(
      request,
      pathname,
      url,
      env,
    );
    if (governanceResponse) return governanceResponse;

    if (
      request.method === "POST" &&
      pathname === "/receptions/proposals/discover"
    ) {
      const body = (await request.json().catch(() => ({}))) as {
        store_id?: unknown;
      };
      return json(
        demoDiscovery(
          typeof body.store_id === "string" ? body.store_id.trim() : "",
        ),
      );
    }
    if (
      request.method === "POST" &&
      pathname === "/receptions/proposals/accept"
    ) {
      const response = demoReceptionResponse();
      const persistenceError = await persistWorkflowAction(
        env,
        response.id,
        "reception.accepted",
        response,
      );
      return persistenceError ?? json(response);
    }

    const sourceAudioMatch =
      /^\/receptions\/(\d+)\/recordings\/(\d+)\/audio$/.exec(pathname);
    if (request.method === "GET" && sourceAudioMatch) {
      const receptionId = Number(sourceAudioMatch[1]);
      const recordingId = Number(sourceAudioMatch[2]);
      if (url.searchParams.get("grant") !== `demo-source-${recordingId}`) {
        return apiError(
          403,
          "AUDIO_PLAYBACK_GRANT_INVALID",
          "音频播放授权不存在或已过期。",
        );
      }
      const workspace = await persistedWorkspaceForReception(env, receptionId);
      const mapping = workspace.recordings.find(
        (candidate) => candidate.recording_id === recordingId,
      );
      if (!mapping || mapping.source_end_sec === null) {
        return apiError(404, "AUDIO_SOURCE_NOT_FOUND", "授权切片不存在。");
      }
      const sourceStartMs = Math.round(mapping.source_start_sec * 1_000);
      const sourceEndMs = Math.round(mapping.source_end_sec * 1_000);
      const response = wavResponse(
        request,
        sourceEndMs - sourceStartMs,
        sourceStartMs,
        sourceEndMs,
      );
      response.headers.set(
        "X-Audio-Grant-Expires-At",
        "2026-12-31T23:59:59Z",
      );
      return response;
    }

    const mergedAudioMatch = /^\/receptions\/(\d+)\/audio$/.exec(pathname);
    if (request.method === "GET" && mergedAudioMatch) {
      if (!env?.DB) return persistenceUnavailable();
      const receptionId = Number(mergedAudioMatch[1]);
      const artifactMatch = /^op-(\d+)$/.exec(
        url.searchParams.get("artifact") ?? "",
      );
      if (!artifactMatch) {
        return apiError(403, "AUDIO_ARTIFACT_GRANT_INVALID", "物理音轨授权无效。");
      }
      const operation = await getRecord<DemoAudioOperation>(
        env.DB,
        "reception-audio-operations",
        Number(artifactMatch[1]),
        demoTenantId,
      );
      if (
        !operation ||
        operation.reception_id !== receptionId ||
        operation.status !== "succeeded"
      ) {
        return apiError(
          404,
          "AUDIO_ARTIFACT_NOT_READY",
          "物理音轨尚未发布或已回收。",
        );
      }
      const plan = await getRecord<DemoAudioPlan>(
        env.DB,
        "reception-audio-plans",
        operation.plan_token,
        demoTenantId,
      );
      if (!plan) {
        return apiError(404, "AUDIO_PLAN_MISSING", "物理音轨清单不存在。");
      }
      return wavResponse(
        request,
        plan.total_duration_ms,
        0,
        plan.total_duration_ms,
      );
    }

    const audioPlanMatch = /^\/receptions\/(\d+)\/audio-plans$/.exec(pathname);
    if (request.method === "POST" && audioPlanMatch) {
      return createDemoAudioPlan(request, env, Number(audioPlanMatch[1]));
    }

    const audioOperationsMatch =
      /^\/receptions\/(\d+)\/audio-operations$/.exec(pathname);
    if (request.method === "POST" && audioOperationsMatch) {
      return createDemoAudioOperation(
        request,
        env,
        Number(audioOperationsMatch[1]),
      );
    }

    const audioOperationCancelMatch =
      /^\/receptions\/(\d+)\/audio-operations\/(\d+)\/cancel$/.exec(pathname);
    if (request.method === "POST" && audioOperationCancelMatch) {
      return cancelDemoAudioOperation(
        env,
        Number(audioOperationCancelMatch[1]),
        Number(audioOperationCancelMatch[2]),
      );
    }

    const audioOperationMatch =
      /^\/receptions\/(\d+)\/audio-operations\/(\d+)$/.exec(pathname);
    if (request.method === "GET" && audioOperationMatch) {
      return completeDemoAudioOperation(
        env,
        Number(audioOperationMatch[1]),
        Number(audioOperationMatch[2]),
      );
    }

    const workspaceMatch = /^\/receptions\/(\d+)\/workspace$/.exec(pathname);
    if (request.method === "GET" && workspaceMatch) {
      return json(
        await persistedWorkspaceForReception(env, Number(workspaceMatch[1])),
      );
    }

    const automationMatch = /^\/receptions\/(\d+)\/automation(?:\/run)?$/.exec(
      pathname,
    );
    if (
      automationMatch &&
      (request.method === "GET" || request.method === "POST")
    ) {
      const receptionId = Number(automationMatch[1]);
      const targetLabels = await publishedTargetLabels(env);
      const current = env?.DB
        ? await getRecord<ReturnType<typeof automationForReception>>(
            env.DB,
            "reception-automation",
            receptionId,
            demoTenantId,
          )
        : null;
      if (request.method === "GET") {
        return json(
          current ?? automationForReception(receptionId, targetLabels),
        );
      }
      if (!env?.DB) return persistenceUnavailable();
      const now = new Date().toISOString();
      const automation = {
        ...(current ?? automationForReception(receptionId, targetLabels)),
        status: "ready",
        stage: "ready",
        attempt_count: (current?.attempt_count ?? 0) + 1,
        target_labels: targetLabels,
        updated_at: now,
        finished_at: now,
      };
      await putRecordWithAudit(
        env.DB,
        "reception-automation",
        receptionId,
        automation,
        "reception.automation.run",
        { attempt_count: automation.attempt_count },
        demoTenantId,
      );
      return json(automation);
    }

    const mergeMatch = /^\/receptions\/(\d+)\/merge$/.exec(pathname);
    if (request.method === "POST" && mergeMatch) {
      const receptionId = Number(mergeMatch[1]);
      const response = demoReceptionResponse(receptionId);
      const persistenceError = await persistWorkflowAction(
        env,
        receptionId,
        "reception.recordings.merged",
        response,
      );
      return persistenceError ?? json(response);
    }

    const segmentMatch = /^\/receptions\/(\d+)\/segment$/.exec(pathname);
    if (request.method === "POST" && segmentMatch) {
      const receptionId = Number(segmentMatch[1]);
      const response = demoDialogueEdit(receptionId);
      const persistenceError = await persistWorkflowAction(
        env,
        receptionId,
        "reception.dialogue.segmented",
        response,
      );
      return persistenceError ?? json(response);
    }

    const unitEditMatch =
      /^\/receptions\/(\d+)\/dialogue-units\/[^/]+\/(?:split|merge)$/.exec(
        pathname,
      );
    if (request.method === "POST" && unitEditMatch) {
      const receptionId = Number(unitEditMatch[1]);
      const response = demoDialogueEdit(receptionId);
      const persistenceError = await persistWorkflowAction(
        env,
        receptionId,
        pathname.endsWith("/split")
          ? "dialogue-unit.split"
          : "dialogue-unit.merge",
        response,
      );
      return persistenceError ?? json(response);
    }

    const tagCorrectionMatch =
      /^\/receptions\/(\d+)\/dialogue-tags\/(\d+)$/.exec(pathname);
    if (request.method === "PATCH" && tagCorrectionMatch) {
      if (!env?.DB) return persistenceUnavailable();
      const receptionId = Number(tagCorrectionMatch[1]);
      const assignmentId = Number(tagCorrectionMatch[2]);
      const body = await requestBody(request);
      const workspace = structuredClone(
        await persistedWorkspaceForReception(env, receptionId),
      );
      const expectedReceptionVersion = Number(body.expected_reception_version);
      const expectedGroupVersion = String(body.expected_group_version ?? "");
      const labelValue = String(body.label_value ?? "").trim();
      const reason = String(body.reason ?? "").trim();
      const evidenceRefIds = Array.isArray(body.evidence_ref_ids)
        ? [...new Set(body.evidence_ref_ids.map(String).filter(Boolean))]
        : [];
      const current = workspace.tag_assignments.find(
        (assignment) =>
          Number(assignment.id) === assignmentId && assignment.is_current,
      );

      if (
        !Number.isInteger(expectedReceptionVersion) ||
        expectedReceptionVersion !== workspace.reception.version
      ) {
        return apiError(
          409,
          "RECEPTION_VERSION_CONFLICT",
          "接待已被其他操作更新，请刷新后重试。",
        );
      }
      if (!current || current.group_version !== expectedGroupVersion) {
        return apiError(
          409,
          "TAG_ASSIGNMENT_VERSION_CONFLICT",
          "标签已被其他操作更新，请刷新后重试。",
        );
      }
      if (!labelValue || labelValue.length > 255 || !reason) {
        return apiError(
          422,
          "TAG_CORRECTION_INVALID",
          "标签值与人工更正原因不能为空。",
        );
      }
      const evidenceById = new Map(
        current.evidence_refs.map((evidence) => [
          String(evidence.ref_id),
          evidence,
        ]),
      );
      if (
        evidenceRefIds.length === 0 ||
        evidenceRefIds.some((refId) => !evidenceById.has(refId))
      ) {
        return apiError(
          422,
          "TAG_EVIDENCE_INVALID",
          "人工更正必须保留至少一条属于原标签的证据。",
        );
      }

      const now = new Date().toISOString();
      const nextVersion = workspace.reception.version + 1;
      const nextAssignmentId = await nextRecordId(
        env.DB,
        "reception-workspace-tags",
        9001,
      );
      current.is_current = false;
      const assignment = {
        ...current,
        id: nextAssignmentId,
        group_version: `manual-r${nextVersion}`,
        label_value: labelValue,
        confidence: 1,
        source: "manual",
        priority: 1_000,
        evidence_refs: evidenceRefIds.map((refId) => evidenceById.get(refId)!),
        model_run_id: `manual-demo-user-1`,
        is_current: true,
        assigned_at: now,
      };
      workspace.tag_assignments.push(assignment);
      const unit = workspace.dialogue_units.find(
        (item) => Number(item.id) === Number(current.dialogue_unit_id),
      );
      if (unit) {
        if (current.label_key === "stage") {
          unit.business_stage = labelValue;
          unit.version += 1;
          unit.edit_status = "manual_edited";
          projectStageTransitions(
            workspace,
            Number(assignment.dialogue_unit_id),
            labelValue,
            assignment.evidence_refs,
            now,
          );
        }
      }
      workspace.reception.version = nextVersion;
      workspace.reception.updated_at = now;
      workspace.window.tag_assignments.total += 1;
      workspace.window.tag_assignments.returned += 1;
      const supersededProvenanceId = await nextRecordId(
        env.DB,
        "reception-workspace-provenance",
        9901,
      );
      const editedProvenanceId = await nextRecordId(
        env.DB,
        "reception-workspace-provenance",
        9901,
      );
      (
        workspace.provenance_events as unknown as Array<Record<string, unknown>>
      ).push(
        {
          id: supersededProvenanceId,
          reception_id: receptionId,
          object_type: "dialogue_tag_assignment",
          object_ref: String(current.id),
          event_type: "superseded",
          actor: "user:1",
          algorithm_version: assignment.group_version,
          parent_refs: [
            {
              type: "dialogue_tag_assignment",
              id: assignment.id,
            },
          ],
          evidence_refs: current.evidence_refs,
          payload: {
            reason,
            label_key: current.label_key,
            previous_value: current.label_value,
            next_value: labelValue,
          },
          occurred_at: now,
        },
        {
          id: editedProvenanceId,
          reception_id: receptionId,
          object_type: "dialogue_tag_assignment",
          object_ref: String(assignment.id),
          event_type: "edited",
          actor: "user:1",
          algorithm_version: assignment.group_version,
          parent_refs: [
            {
              type: "dialogue_tag_assignment",
              id: current.id,
            },
          ],
          evidence_refs: assignment.evidence_refs,
          payload: {
            reason,
            label_key: assignment.label_key,
            previous_value: current.label_value,
            next_value: assignment.label_value,
          },
          occurred_at: now,
        },
      );
      workspace.window.provenance_events.total += 2;
      workspace.window.provenance_events.returned += 2;
      const persistedInsights = structuredClone(
        demoTagInsights,
      ) as unknown as MutableDemoTagInsights;
      const insightGroupId = `${assignment.group_key}@${assignment.group_version}`;
      if (!persistedInsights.selected_group_ids.includes(insightGroupId)) {
        persistedInsights.selected_group_ids.unshift(insightGroupId);
      }
      if (
        !persistedInsights.insights.groups.some(
          (group) => group.group_id === insightGroupId,
        )
      ) {
        persistedInsights.insights.groups.unshift({
          group_key: assignment.group_key,
          version: assignment.group_version,
          group_id: insightGroupId,
          source: "manual",
          priority: 1_000,
        });
      }
      const insightEvidence = assignment.evidence_refs.map((evidence) => ({
        ref_id: String(evidence.ref_id),
        kind: evidence.kind === "text" ? "text" : "audio",
        recording_id: String(evidence.recording_id),
        start_ms:
          evidence.timeline_start_ms ?? evidence.source_start_ms ?? null,
        end_ms: evidence.timeline_end_ms ?? evidence.source_end_ms ?? null,
        text_excerpt: evidence.text_excerpt ?? null,
      }));
      persistedInsights.evidence_summary.unshift({
        reception_id: receptionId,
        dialogue_unit_id: Number(assignment.dialogue_unit_id),
        group_id: insightGroupId,
        label_key: assignment.label_key,
        label_value: assignment.label_value,
        confidence: assignment.confidence,
        evidence_count: insightEvidence.length,
        evidence_refs: insightEvidence,
      });
      persistedInsights.evidence_summary_count =
        persistedInsights.evidence_summary.length;
      persistedInsights.evidence_summary_total += 1;
      persistedInsights.total_assignments += 1;
      persistedInsights.assignment_count += 1;
      persistedInsights.insights.overview.assignment_count += 1;
      persistedInsights.insights.distributions.unshift({
        group_key: insightGroupId,
        label_key: assignment.label_key,
        value: assignment.label_value,
        count: 1,
        proportion: 1,
      });
      persistedInsights.generated_at = now;
      await putRecordsWithAudit(
        env.DB,
        [
          {
            namespace: "reception-workspaces",
            recordId: receptionId,
            payload: workspace,
          },
          {
            namespace: "reception-tag-insights",
            recordId: "current",
            payload: persistedInsights,
          },
        ],
        "dialogue-tag.corrected",
        {
          reception_id: receptionId,
          previous_assignment_id: assignmentId,
          assignment_id: assignment.id,
          reason,
        },
        demoTenantId,
      );
      return json({
        reception_id: receptionId,
        reception_version: nextVersion,
        superseded_assignment_id: assignmentId,
        assignment,
      });
    }

    const deriveMatch = /^\/receptions\/(\d+)\/dialogue-tags\/derive$/.exec(
      pathname,
    );
    if (request.method === "POST" && deriveMatch) {
      if (!env?.DB) return persistenceUnavailable();
      const receptionId = Number(deriveMatch[1]);
      const body = await requestBody(request);
      const publishedLabels = await publishedTargetLabels(env);
      const requestedLabels = Array.isArray(body.target_labels)
        ? [
            ...new Set(
              body.target_labels
                .map(String)
                .filter((label) => publishedLabels.includes(label)),
            ),
          ]
        : publishedLabels;
      const normalizedInput = {
        reception_id: receptionId,
        group_key: String(body.group_key ?? "reception-rules"),
        group_version: String(body.group_version ?? "rules-v1"),
        target_labels: requestedLabels,
        priority: Number(body.priority ?? 0),
        model_run_id:
          body.model_run_id === undefined ? null : String(body.model_run_id),
      };
      const inputHash = await payloadHash(normalizedInput);
      const persisted = await getRecord<Record<string, unknown>>(
        env.DB,
        "reception-tag-derivations",
        `${receptionId}:${inputHash}`,
        demoTenantId,
      );
      if (persisted) {
        return json({ ...persisted, no_op: true });
      }

      const assignments = demoWorkspace.tag_assignments.filter((assignment) =>
        requestedLabels.includes(assignment.label_key),
      );
      const missing = requestedLabels
        .filter(
          (label) =>
            !assignments.some((assignment) => assignment.label_key === label),
        )
        .map((label) => ({
          dialogue_unit_id: demoWorkspace.dialogue_units[0].id,
          unit_index: 0,
          label_key: label,
          reason: "no_rule_match",
        }));
      const response = {
        reception_id: receptionId,
        group_key: normalizedInput.group_key,
        group_version: normalizedInput.group_version,
        requested_labels: requestedLabels,
        assignment_count: assignments.length,
        superseded_count: 0,
        no_op: false,
        assignments,
        missing,
      };
      await putRecordWithAudit(
        env.DB,
        "reception-tag-derivations",
        `${receptionId}:${inputHash}`,
        response,
        "dialogue-tags.derived",
        { reception_id: receptionId, input_hash: inputHash },
        demoTenantId,
      );
      return json(response);
    }

    if (request.method === "GET") {
      const response = safeGet(pathname, url);
      if (response) return response;
    }

    return apiError(404, "DEMO_ROUTE_NOT_FOUND", "演示站未提供该接口。");
  },
};

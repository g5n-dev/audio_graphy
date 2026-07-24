const generatedAt = "2026-07-24T09:00:00.000Z";

export const demoUser = {
  id: 1,
  name: "演示管理员",
  email: "demo@audiography.cn",
  role: "admin",
  tenant_id: "tenant-demo",
};

export const demoStateInsights = {
  tenant_id: "tenant-demo",
  total_receptions: 386,
  // Must equal the cardinality represented by the complete edge aggregation.
  // Keeping this invariant prevents path-share KPIs from exceeding 100%.
  total_transitions: 1_158,
  returned_stages: 8,
  stage_limit: 64,
  returned_transitions: 10,
  transition_limit: 100,
  truncated: false,
  stages: [
    ["初次接触", 312, 312, 0, 286, 0.94],
    ["需求发现", 286, 286, 335, 228, 0.9],
    ["方案推荐", 228, 228, 228, 178, 0.87],
    ["产品体验", 178, 178, 178, 146, 0.85],
    ["价格沟通", 146, 146, 146, 137, 0.81],
    ["异议处理", 112, 112, 112, 94, 0.78],
    ["成交确认", 76, 76, 76, 58, 0.91],
    ["后续跟进", 83, 83, 83, 0, 0.88],
  ].map(
    ([
      state,
      count,
      reception_count,
      incoming_count,
      outgoing_count,
      average_confidence,
    ]) => ({
      state,
      count,
      reception_count,
      incoming_count,
      outgoing_count,
      average_confidence,
    }),
  ),
  transitions: [
    ["初次接触", "需求发现", 286, 0.93, 262, "开放式提问", [101, 102, 103]],
    ["需求发现", "方案推荐", 228, 0.89, 205, "确认预算与偏好", [101, 105]],
    ["方案推荐", "产品体验", 178, 0.87, 154, "邀请试戴/试乘", [101, 106]],
    ["产品体验", "价格沟通", 146, 0.84, 126, "客户询价", [101, 108]],
    ["价格沟通", "异议处理", 112, 0.8, 93, "价格异议", [101, 111]],
    ["异议处理", "成交确认", 76, 0.86, 69, "异议已化解", [101, 115]],
    ["成交确认", "后续跟进", 58, 0.92, 52, "预约交付", [101, 119]],
    ["异议处理", "需求发现", 18, 0.54, 12, "需求重新确认", [123, 129]],
    ["方案推荐", "需求发现", 31, 0.66, 22, "方案不匹配", [131, 142]],
    ["价格沟通", "后续跟进", 25, 0.42, 16, "异常跳过成交确认", [151, 168]],
  ].map(
    ([
      from_state,
      to_state,
      count,
      average_confidence,
      evidence_count,
      trigger,
      sample_reception_ids,
    ]) => ({
      from_state,
      to_state,
      count,
      average_confidence,
      evidence_count,
      top_triggers: [
        { trigger, count: Math.max(1, Math.round(Number(count) * 0.58)) },
        {
          trigger: "客户主动反馈",
          count: Math.max(1, Math.round(Number(count) * 0.24)),
        },
      ],
      sample_reception_ids,
    }),
  ),
  generated_at: generatedAt,
};

const evidence = (
  refId: string,
  recordingId: number,
  startMs: number,
  text: string,
) => ({
  ref_id: refId,
  kind: "text",
  recording_id: recordingId,
  coordinate_space: "both",
  source_start_ms: startMs,
  source_end_ms: startMs + 8_000,
  timeline_start_ms: startMs,
  timeline_end_ms: startMs + 8_000,
  text_excerpt: text,
});

const dialogueUnits = [
  ["迎宾开场", "初次接触", "销售主动迎宾并建立信任", 0, 38],
  ["需求探索", "需求发现", "确认用途、预算和款式偏好", 38, 92],
  ["方案呈现", "方案推荐", "根据需求推荐两套产品组合", 92, 154],
  ["产品试戴", "产品体验", "客户试戴并比较材质与工艺", 154, 218],
  ["报价沟通", "价格沟通", "解释工费、活动和保值政策", 218, 274],
  ["异议化解", "异议处理", "回应价格与售后服务顾虑", 274, 332],
  ["成交推进", "成交确认", "确认款式、尺码与支付方式", 332, 382],
  ["复访安排", "后续跟进", "约定取货时间与复访事项", 382, 420],
].map(([topic, business_stage, summary, start_sec, end_sec], index) => ({
  id: 1001 + index,
  source_recording_id: 5001 + Math.floor(index / 2),
  unit_index: index,
  version: 1,
  start_sec,
  end_sec,
  topic,
  business_stage,
  summary,
  boundary_confidence: 0.86 + (index % 3) * 0.03,
  boundary_reasons: [
    { code: "semantic_shift", detail: "话题与业务阶段发生变化" },
  ],
  segment_refs: [{ segment_id: 7001 + index * 2 }, { segment_id: 7002 + index * 2 }],
  speaker_refs: ["顾问小林", "客户"],
  edit_status: index === 5 ? "manual_edited" : "auto",
  tag_assignments: [],
}));

const transcriptLines = [
  ["顾问小林", "您好，欢迎光临。今天是想看日常佩戴还是重要场合的款式？"],
  ["客户", "想给家人挑一件生日礼物，预算两万元左右。"],
  ["顾问小林", "明白，更偏简约耐看，还是希望设计感更强一些？"],
  ["客户", "简约一些，最好日常也能戴。"],
  ["顾问小林", "这两款都在预算内，我先从材质、工艺和佩戴效果帮您比较。"],
  ["客户", "这款看起来不错，可以试一下吗？"],
  ["顾问小林", "当然可以。您看这个弧度贴合度更好，日常不容易勾衣服。"],
  ["客户", "价格还能再优惠一点吗？"],
  ["顾问小林", "今天会员活动减免部分工费，也包含一次免费保养和尺寸调整。"],
  ["客户", "我主要担心以后保值和售后。"],
  ["顾问小林", "证书、克重和售后项目都会写入订单，我逐项给您确认。"],
  ["客户", "那就选刚才试戴的这一款。"],
  ["顾问小林", "好的，我再核对尺码与刻字内容，预计周日下午可以取货。"],
  ["客户", "可以，到时候微信提醒我。"],
  ["顾问小林", "已经为您备注，取货前一天我会再次确认。"],
  ["客户", "好的，谢谢。"],
];

const transcriptItems = transcriptLines.map(([speaker, text], index) => {
  const unit = dialogueUnits[Math.floor(index / 2)];
  const unitStart = Number(unit.start_sec);
  const unitEnd = Number(unit.end_sec);
  const half = (unitEnd - unitStart) / 2;
  const start = unitStart + (index % 2) * half;
  return {
    segment_id: 7001 + index,
    recording_id: 5001 + Math.floor(index / 4),
    segment_index: index,
    source_start_sec: start,
    source_end_sec: Math.min(start + half - 1, unitEnd),
    timeline_start_sec: start,
    timeline_end_sec: Math.min(start + half - 1, unitEnd),
    speaker,
    text,
    vad_confidence: 0.95,
  };
});

const workspaceTags = dialogueUnits.flatMap((unit, index) => {
  const recordingId = Number(unit.source_recording_id);
  const startMs = Number(unit.start_sec) * 1_000;
  const text = transcriptLines[index * 2]?.[1] ?? String(unit.summary);
  return [
    {
      id: 8001 + index * 2,
      reception_id: 101,
      dialogue_unit_id: unit.id,
      group_key: "sales-model",
      group_version: "v3.2",
      label_key: "stage",
      label_value: unit.business_stage,
      confidence: 0.9 - index * 0.01,
      source: "llm",
      priority: 10,
      evidence_refs: [evidence(`stage-${index}`, recordingId, startMs, text)],
      model_run_id: "run-demo-20260724",
      is_current: true,
      assigned_at: generatedAt,
    },
    {
      id: 8002 + index * 2,
      reception_id: 101,
      dialogue_unit_id: unit.id,
      group_key: "sales-model",
      group_version: "v3.2",
      label_key: index < 5 ? "intent" : "next_step",
      label_value:
        index < 3
          ? "兴趣建立"
          : index < 5
            ? "高意向"
            : index < 7
              ? "成交推进"
              : "预约复访",
      confidence: 0.82 + (index % 3) * 0.04,
      source: "llm",
      priority: 10,
      evidence_refs: [evidence(`intent-${index}`, recordingId, startMs, text)],
      model_run_id: "run-demo-20260724",
      is_current: true,
      assigned_at: generatedAt,
    },
  ];
});

const workspaceTransitions = demoStateInsights.transitions
  .slice(0, 7)
  .map((transition, index) => ({
    id: 9001 + index,
    dialogue_unit_id: 1002 + index,
    sequence_no: index + 1,
    from_state: transition.from_state,
    to_state: transition.to_state,
    trigger: transition.top_triggers[0].trigger,
    confidence: transition.average_confidence,
    evidence_refs: [
      evidence(
        `transition-${index}`,
        5001 + Math.floor(index / 2),
        Number(dialogueUnits[index + 1].start_sec) * 1_000,
        transcriptLines[(index + 1) * 2]?.[1] ?? "阶段转移证据",
      ),
    ],
    algorithm_version: "state-flow-v2.4",
    created_at: generatedAt,
  }));

const provenanceEvents = dialogueUnits.map((unit, index) => ({
  id: 9501 + index,
  reception_id: 101,
  object_type: index === 0 ? "reception" : "dialogue_unit",
  object_ref: String(index === 0 ? 101 : unit.id),
  event_type:
    index === 0
      ? "recordings_merged"
      : index === 5
        ? "manual_boundary_adjusted"
        : "dialogue_unit_segmented",
  actor: index === 5 ? "质检员王敏" : "automation",
  algorithm_version: index === 5 ? null : "segmenter-v4.1",
  parent_refs:
    index === 0
      ? [{ object_type: "recording", object_ref: "5001" }]
      : [{ object_type: "reception", object_ref: "101" }],
  evidence_refs: [
    evidence(
      `provenance-${index}`,
      Number(unit.source_recording_id),
      Number(unit.start_sec) * 1_000,
      String(unit.summary),
    ),
  ],
  payload: {
    summary: unit.summary,
    confidence: unit.boundary_confidence,
  },
  occurred_at: `2026-07-24T0${Math.min(index + 1, 9)}:12:00Z`,
}));

export const demoWorkspace = {
  reception: {
    id: 101,
    tenant_id: "tenant-demo",
    external_session_id: "gold-20260724-001",
    scenario: "gold",
    store_id: "上海静安旗舰店",
    agent_name: "顾问小林",
    agent_user_id: 17,
    customer_hash: "customer_demo_7f31",
    status: "ready",
    merge_mode: "both",
    merge_confidence: 0.94,
    started_at: "2026-07-24T01:00:00Z",
    ended_at: "2026-07-24T01:07:00Z",
    audio_url: null,
    version: 4,
    created_at: "2026-07-24T01:08:00Z",
    updated_at: generatedAt,
  },
  recordings: [0, 1, 2, 3].map((index) => ({
    id: 6001 + index,
    recording_id: 5001 + index,
    sequence_no: index,
    timeline_start_sec: index * 105,
    timeline_end_sec: (index + 1) * 105,
    source_start_sec: 0,
    source_end_sec: 105,
    gap_before_sec: index === 0 ? 0 : 1.2,
    decision_source: index === 0 ? "explicit" : "auto",
    merge_confidence: 0.91 + index * 0.01,
    merge_reasons: {
      same_store: true,
      temporal_gap_sec: 1.2,
      speaker_continuity: 0.93,
    },
    source_recorded_at: `2026-07-24T01:0${index}:00Z`,
    audio_url: "",
  })),
  dialogue_units: dialogueUnits,
  state_transitions: workspaceTransitions,
  tag_assignments: workspaceTags,
  transcript_items: transcriptItems,
  provenance_events: provenanceEvents,
  window: {
    start_sec: 0,
    end_sec: 420,
    size_sec: 600,
    reception_duration_sec: 420,
    truncated: false,
    has_previous: false,
    has_next: false,
    previous_start_sec: null,
    next_start_sec: null,
    total_dialogue_units: dialogueUnits.length,
    protected_dialogue_units: dialogueUnits.length,
    dialogue_units: {
      total: dialogueUnits.length,
      returned: dialogueUnits.length,
      limit: 100,
      truncated: false,
    },
    tag_assignments: {
      total: workspaceTags.length,
      returned: workspaceTags.length,
      limit: 200,
      truncated: false,
    },
    state_transitions: {
      total: workspaceTransitions.length,
      returned: workspaceTransitions.length,
      limit: 100,
      truncated: false,
    },
    transcript_items: {
      total: transcriptItems.length,
      returned: transcriptItems.length,
      limit: 300,
      truncated: false,
    },
    provenance_events: {
      total: provenanceEvents.length,
      returned: provenanceEvents.length,
      limit: 100,
      truncated: false,
    },
  },
};

const groups = [
  {
    group_key: "销售模型",
    version: "v3.2",
    group_id: "sales-model@v3.2",
    source: "llm",
    priority: 10,
  },
  {
    group_key: "风险模型",
    version: "v2.1",
    group_id: "risk-model@v2.1",
    source: "llm",
    priority: 20,
  },
  {
    group_key: "人工复核",
    version: "2026.07",
    group_id: "human-review@2026.07",
    source: "manual",
    priority: 100,
  },
];

const tagDefinitions = [
  ["stage.greeting", "完成", "完成", "完成", "您好，欢迎光临。"],
  ["stage.requirement", "深入", "一般", "深入", "预算两万元，更偏简约耐看。"],
  ["stage.presentation", "匹配", "匹配", "匹配", "我从材质和工艺帮您比较。"],
  ["stage.experience", "充分", "充分", "充分", "这个弧度贴合度更好。"],
  ["objection.price", "已化解", "存在风险", "已化解", "会员活动减免部分工费。"],
  ["intent.level", "高意向", "中意向", "高意向", "那就选刚才试戴的这一款。"],
  ["next_step", "预约取货", "待跟进", "预约取货", "预计周日下午可以取货。"],
  ["compliance.risk", "无风险", "承诺风险", "已复核无风险", "证书和售后项目都会写入订单。"],
] as const;

const matrix = tagDefinitions.map((definition, rowIndex) => {
  const [labelKey, ...rest] = definition;
  const values = rest.slice(0, 3);
  const excerpt = rest[3];
  const targetId = `reception:${101 + (rowIndex % 4)}/unit:${1001 + rowIndex}`;
  const rowEvidence = evidence(
    `tag-evidence-${rowIndex}`,
    5001 + Math.floor(rowIndex / 2),
    rowIndex * 48_000,
    excerpt,
  );
  const cells = groups.map((group, groupIndex) => {
    const value = values[groupIndex];
    return {
      group,
      assignments: [
        {
          group_key: group.group_key,
          group_version: group.version,
          group_id: group.group_id,
          target_id: targetId,
          window: {
            start_ms: rowIndex * 48_000,
            end_ms: rowIndex * 48_000 + 42_000,
          },
          label_key: labelKey,
          value,
          confidence: groupIndex === 2 ? 1 : 0.82 + groupIndex * 0.06,
          evidence_refs: [rowEvidence],
          is_manual: groupIndex === 2,
          occurred_at: `2026-07-${17 + (rowIndex % 7)}T08:00:00Z`,
          store_id: rowIndex % 2 === 0 ? "上海静安旗舰店" : "上海浦东体验店",
          agent_id: rowIndex % 2 === 0 ? "顾问小林" : "顾问陈悦",
        },
      ],
      missing: false,
    };
  });
  const conflict = new Set(values).size > 1;
  return {
    target_id: targetId,
    window: {
      start_ms: rowIndex * 48_000,
      end_ms: rowIndex * 48_000 + 42_000,
    },
    label_key: labelKey,
    store_ids: [rowIndex % 2 === 0 ? "上海静安旗舰店" : "上海浦东体验店"],
    agent_ids: [rowIndex % 2 === 0 ? "顾问小林" : "顾问陈悦"],
    cells,
    merged: {
      strategy: "manual_wins",
      values: [values[2]],
      selected_group_keys: [groups[2].group_id],
      confidence: 1,
      evidence_refs: [rowEvidence],
    },
    conflict,
    missing_group_keys: [],
  };
});

const distributions = matrix.flatMap((row, rowIndex) =>
  row.cells.map((cell, groupIndex) => ({
    group_key: cell.group.group_id,
    label_key: row.label_key,
    value: cell.assignments[0].value,
    count: Math.max(22, 148 - rowIndex * 11 - groupIndex * 7),
    proportion: Math.max(0.18, 0.64 - groupIndex * 0.12),
  })),
);

const trendSeries = [
  ["sales-model@v3.2", "stage.greeting", "完成"],
  ["sales-model@v3.2", "stage.requirement", "深入"],
  ["risk-model@v2.1", "objection.price", "存在风险"],
  ["human-review@2026.07", "intent.level", "高意向"],
  ["human-review@2026.07", "next_step", "预约取货"],
] as const;

export const demoTagAnalysis = {
  tenant_id: "tenant-demo",
  merge_strategy: "manual_wins",
  groups,
  truncated: false,
  matrix_truncated: false,
  difference_truncated: false,
  evidence_truncated: false,
  output_budget: {
    matrix_limit: 96,
    matrix_total_rows: matrix.length,
    matrix_returned_rows: matrix.length,
    difference_limit: 128,
    difference_total_items: 12,
    difference_returned_items: 12,
    distribution_limit: 512,
    distribution_total_items: distributions.length,
    distribution_returned_items: distributions.length,
    trend_limit: 512,
    trend_total_items: trendSeries.length * 7,
    trend_returned_items: trendSeries.length * 7,
    dimension_limit: 256,
    dimension_total_items: 12,
    dimension_returned_items: 12,
    evidence_ref_limit: 512,
    evidence_ref_count: matrix.length * 4,
    evidence_text_byte_limit: 32_768,
    evidence_text_bytes: 824,
  },
  overview: {
    group_count: groups.length,
    assignment_count: matrix.length * groups.length,
    total_cells: matrix.length,
    complete_cells: matrix.length,
    incomplete_cells: 0,
    conflict_cells: matrix.filter((row) => row.conflict).length,
    conflict_rate:
      matrix.filter((row) => row.conflict).length / matrix.length,
  },
  matrix,
  coverage: groups.map((group) => ({
    group_key: group.group_id,
    assigned_cells: matrix.length,
    missing_cells: 0,
    coverage_rate: 1,
  })),
  pairwise: [
    {
      left_group_key: groups[0].group_id,
      right_group_key: groups[1].group_id,
      comparable_cells: 8,
      agreements: 4,
      differences: 4,
      agreement_rate: 0.5,
      left_only_cells: 0,
      right_only_cells: 0,
      overlap_rate: 1,
      difference_items: [],
      difference_items_truncated: false,
    },
    {
      left_group_key: groups[0].group_id,
      right_group_key: groups[2].group_id,
      comparable_cells: 8,
      agreements: 7,
      differences: 1,
      agreement_rate: 0.875,
      left_only_cells: 0,
      right_only_cells: 0,
      overlap_rate: 1,
      difference_items: [],
      difference_items_truncated: false,
    },
    {
      left_group_key: groups[1].group_id,
      right_group_key: groups[2].group_id,
      comparable_cells: 8,
      agreements: 3,
      differences: 5,
      agreement_rate: 0.375,
      left_only_cells: 0,
      right_only_cells: 0,
      overlap_rate: 1,
      difference_items: [],
      difference_items_truncated: false,
    },
  ],
  distributions,
  trends: trendSeries.flatMap(([group_key, label_key, value], seriesIndex) =>
    Array.from({ length: 7 }, (_, dayIndex) => ({
      bucket_key: `2026-07-${String(18 + dayIndex).padStart(2, "0")}`,
      group_key,
      label_key,
      value,
      count: 24 + dayIndex * (seriesIndex + 2) + seriesIndex * 5,
    })),
  ),
  co_occurrences: [
    {
      group_key: groups[0].group_id,
      left_label: "stage.greeting=完成",
      right_label: "intent.level=高意向",
      count: 118,
    },
    {
      group_key: groups[2].group_id,
      left_label: "stage.requirement=深入",
      right_label: "objection.price=已化解",
      count: 76,
    },
    {
      group_key: groups[2].group_id,
      left_label: "intent.level=高意向",
      right_label: "next_step=预约取货",
      count: 63,
    },
  ],
  confidence: groups.flatMap((group, groupIndex) =>
    ["0.6-0.7", "0.7-0.8", "0.8-0.9", "0.9-1.0"].map(
      (bucket, bucketIndex) => ({
        group_key: group.group_id,
        bucket,
        count: 10 + bucketIndex * 15 + groupIndex * 4,
        average_confidence: 0.65 + bucketIndex * 0.1,
      }),
    ),
  ),
  dimension_comparisons: ["上海静安旗舰店", "上海浦东体验店"].flatMap(
    (dimensionValue, dimensionIndex) =>
      groups.map((group, groupIndex) => ({
        dimension: "store",
        dimension_value: dimensionValue,
        group_key: group.group_id,
        total_cells: 186 - dimensionIndex * 28,
        assignment_count: 178 - groupIndex * 8,
        missing_cells: 8 + groupIndex * 4,
        coverage_rate: 0.96 - groupIndex * 0.04,
        unique_targets: 172 - dimensionIndex * 20,
        average_confidence: 0.91 - groupIndex * 0.05,
        conflict_assignments: 12 + groupIndex * 4,
        conflict_rate: 0.06 + groupIndex * 0.03,
      })),
  ),
};

export const demoTagInsights = {
  tenant_id: "tenant-demo",
  page: 1,
  page_size: 20,
  total_receptions: 386,
  returned_reception_ids: [101, 102, 103, 104],
  total_assignments: 1_842,
  assignment_count: 24,
  assignment_limit: 1_000,
  truncated: false,
  assignment_truncated: false,
  group_truncated: false,
  difference_truncated: false,
  evidence_truncated: false,
  evidence_ref_limit: 1_024,
  evidence_ref_count: 32,
  evidence_summary_total: matrix.length,
  evidence_summary_count: matrix.length,
  evidence_summary_limit: 256,
  evidence_summary_truncated: false,
  selection_mode: "current",
  selected_group_ids: groups.map((group) => group.group_id),
  merge_strategy: "manual_wins",
  trend_granularity: "day",
  insights: demoTagAnalysis,
  evidence_summary: matrix.map((row, index) => ({
    reception_id: 101 + (index % 4),
    dialogue_unit_id: 1001 + index,
    group_id: groups[2].group_id,
    label_key: row.label_key,
    label_value: row.merged.values[0],
    confidence: 1,
    evidence_count: 1,
    evidence_refs: row.merged.evidence_refs,
  })),
  generated_at: generatedAt,
};

export const demoRecordings = {
  items: [
    {
      id: 5001,
      store_id: "上海静安旗舰店",
      agent_name: "顾问小林",
      status: "indexed",
      pipeline_state: "ready",
      recorded_at: "2026-07-24T01:00:00Z",
      indexed_at: generatedAt,
      prompt_version: "sales-v3.2",
    },
    {
      id: 5002,
      store_id: "上海静安旗舰店",
      agent_name: "顾问小林",
      status: "indexed",
      pipeline_state: "ready",
      recorded_at: "2026-07-24T01:01:45Z",
      indexed_at: generatedAt,
      prompt_version: "sales-v3.2",
    },
  ],
  total: 2,
  page: 1,
  page_size: 20,
};

export const demoReceptions = {
  items: [demoWorkspace.reception],
  total: 1,
  page: 1,
  page_size: 20,
};

export const demoExplore = {
  nodes: demoStateInsights.stages.map((stage, index) => ({
    id: `state-${index}`,
    label: stage.state,
    type: "dialogue_state",
    description: `${stage.reception_count} 个接待到达该阶段`,
    degree: Number(stage.incoming_count) + Number(stage.outgoing_count),
    source_ids: [`state:${stage.state}`],
    recording_ids: [5001, 5002],
  })),
  edges: demoStateInsights.transitions.map((transition, index) => ({
    source: `state-${demoStateInsights.stages.findIndex(
      (stage) => stage.state === transition.from_state,
    )}`,
    target: `state-${demoStateInsights.stages.findIndex(
      (stage) => stage.state === transition.to_state,
    )}`,
    relation: transition.top_triggers[0].trigger,
    weight: transition.count,
    confidence:
      Number(transition.average_confidence) >= 0.8 ? "high" : "medium",
    confidence_score: transition.average_confidence,
    source_ids: [`transition-${index}`],
  })),
  total_nodes: demoStateInsights.stages.length,
  total_edges: demoStateInsights.transitions.length,
};

export const demoStats = {
  dimensions: ["store_id"],
  items: [
    {
      group_key: "上海静安旗舰店",
      tag_count: 1_248,
      pass_count: 1_073,
      fail_count: 175,
      pass_rate: 0.86,
    },
    {
      group_key: "上海浦东体验店",
      tag_count: 986,
      pass_count: 789,
      fail_count: 197,
      pass_rate: 0.8,
    },
  ],
  total_records: 386,
};

import { useMutation, useQuery } from "@tanstack/react-query";
import {
  ChangeEvent,
  FormEvent,
  type KeyboardEvent,
  useMemo,
  useRef,
  useState,
} from "react";
import { Link } from "react-router-dom";
import { analyzeTagInsights, getReceptionTagInsights } from "@/api/services";
import { formatPercent } from "@/components/dialogue/format";
import { InsightContextTabs } from "@/components/navigation/ContextNavigation";
import { useAuthStore } from "@/stores/auth";
import type {
  AnalyzeTagInsightsRequest,
  AnalyzeTagInsightsResponse,
  ReceptionScenario,
  ReceptionTagEvidenceSummary,
  ReceptionTagInsightsRequest,
  ReceptionTagInsightsResponse,
  TagInsightAssignment,
  TagInsightEvidenceRef,
  TagInsightGroup,
  TagMergeStrategy,
  TrendGranularity,
} from "@/types/api";
import { GovernanceActions } from "./GovernanceActions";
import { InsightVisuals } from "./InsightVisuals";
import { TagInsightGraph } from "./TagInsightGraph";
import { TagMatrix } from "./TagMatrix";
import "./TagInsightsPage.css";

const MERGE_STRATEGIES: Array<{
  key: TagMergeStrategy;
  label: string;
  help: string;
}> = [
  {
    key: "manual_wins",
    label: "人工优先",
    help: "存在人工结果时覆盖模型结果，否则按优先级选择。",
  },
  {
    key: "priority",
    label: "组优先级",
    help: "采用 priority 最高的标签组结果。",
  },
  {
    key: "union",
    label: "并集",
    help: "保留所有标签组出现过的值及其证据。",
  },
  {
    key: "intersection",
    label: "交集",
    help: "只保留全部标签组共同认可的值。",
  },
];

const INSIGHT_SECTIONS = [
  { id: "tag-relationship-graph", label: "关系图谱" },
  { id: "tag-comparison-matrix", label: "对比矩阵" },
  { id: "tag-chart-insights", label: "图表分析" },
] as const;
type InsightSectionId = (typeof INSIGHT_SECTIONS)[number]["id"];

function insightTabId(sectionId: InsightSectionId): string {
  return `${sectionId}-tab`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requiredString(value: unknown, field: string, index?: number): string {
  if (typeof value === "string" && value.trim()) return value.trim();
  throw new Error(
    `${field}${index === undefined ? "" : ` #${index + 1}`} 不能为空`,
  );
}

function normalizeGroup(value: unknown, index: number): TagInsightGroup {
  if (!isRecord(value)) throw new Error(`标签组 #${index + 1} 格式错误`);
  return {
    group_key: requiredString(value.group_key, "group_key", index),
    version: requiredString(value.version, "version", index),
    group_id:
      value.group_id === undefined || value.group_id === null
        ? null
        : requiredString(value.group_id, "group_id", index),
    source: requiredString(value.source, "source", index),
    priority:
      typeof value.priority === "number" && Number.isFinite(value.priority)
        ? value.priority
        : 0,
  };
}

function normalizeEvidence(
  value: unknown,
  index: number,
): TagInsightEvidenceRef {
  if (!isRecord(value)) throw new Error(`证据 #${index + 1} 格式错误`);
  const kind = value.kind;
  if (kind !== "audio" && kind !== "text") {
    throw new Error(`证据 #${index + 1} 的 kind 必须是 audio 或 text`);
  }
  return {
    ref_id: requiredString(value.ref_id, "ref_id", index),
    kind,
    recording_id: requiredString(value.recording_id, "recording_id", index),
    start_ms: typeof value.start_ms === "number" ? value.start_ms : null,
    end_ms: typeof value.end_ms === "number" ? value.end_ms : null,
    text_excerpt:
      typeof value.text_excerpt === "string" ? value.text_excerpt : null,
  };
}

function normalizeAssignment(
  value: unknown,
  index: number,
): TagInsightAssignment {
  if (!isRecord(value)) throw new Error(`标签赋值 #${index + 1} 格式错误`);
  if (!isRecord(value.window)) {
    throw new Error(`标签赋值 #${index + 1} 缺少 window`);
  }
  const startMs = value.window.start_ms;
  const endMs = value.window.end_ms;
  if (
    typeof startMs !== "number" ||
    typeof endMs !== "number" ||
    startMs < 0 ||
    endMs <= startMs
  ) {
    throw new Error(`标签赋值 #${index + 1} 的时间窗无效`);
  }
  return {
    group_key: requiredString(value.group_key, "group_key", index),
    group_version:
      value.group_version === undefined || value.group_version === null
        ? null
        : requiredString(value.group_version, "group_version", index),
    group_id:
      value.group_id === undefined || value.group_id === null
        ? null
        : requiredString(value.group_id, "group_id", index),
    target_id: requiredString(value.target_id, "target_id", index),
    window: { start_ms: startMs, end_ms: endMs },
    label_key: requiredString(value.label_key, "label_key", index),
    value: requiredString(value.value, "value", index),
    confidence: typeof value.confidence === "number" ? value.confidence : null,
    evidence_refs: Array.isArray(value.evidence_refs)
      ? value.evidence_refs.map(normalizeEvidence)
      : [],
    is_manual: value.is_manual === true,
    occurred_at:
      typeof value.occurred_at === "string" ? value.occurred_at : null,
    store_id: typeof value.store_id === "string" ? value.store_id : null,
    agent_id: typeof value.agent_id === "string" ? value.agent_id : null,
  };
}

function groupIdentity(group: TagInsightGroup): string {
  return group.group_id ?? group.group_key;
}

function parseTagSnapshot(raw: string): AnalyzeTagInsightsRequest {
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    throw new Error("JSON 解析失败，请检查逗号、引号和括号。");
  }
  if (!isRecord(value)) throw new Error("标签快照必须是 JSON 对象。");
  if (!Array.isArray(value.groups) || value.groups.length === 0) {
    throw new Error("标签快照至少需要一个 groups 项。");
  }
  if (value.groups.length > 8) {
    throw new Error("标签快照最多支持 8 个标签组/版本列。");
  }
  if (!Array.isArray(value.assignments) || value.assignments.length === 0) {
    throw new Error("标签快照至少需要一个 assignments 项。");
  }
  if (value.assignments.length > 5_000) {
    throw new Error("标签快照最多支持 5,000 条标签赋值。");
  }
  const strategy = value.merge_strategy;
  const granularity = value.trend_granularity;
  const parsedGroups = value.groups.map(normalizeGroup);
  const groups = parsedGroups.map((group) => ({
    ...group,
    group_id: group.group_id ?? `${group.group_key}@${group.version}`,
  }));
  const groupIds = groups.map(groupIdentity);
  if (new Set(groupIds).size !== groupIds.length) {
    throw new Error("group_id 必须唯一，请检查重复的标签组版本声明。");
  }
  const parsedAssignments = value.assignments.map(normalizeAssignment);
  const assignments = parsedAssignments.map((assignment, index) => {
    const candidates = groups.filter(
      (group) => group.group_key === assignment.group_key,
    );
    const selected = assignment.group_id
      ? candidates.find((group) => groupIdentity(group) === assignment.group_id)
      : assignment.group_version
        ? candidates.find((group) => group.version === assignment.group_version)
        : candidates.length === 1
          ? candidates[0]
          : undefined;
    if (!selected) {
      throw new Error(
        `标签赋值 #${index + 1} 无法定位唯一版本，请提供 group_version 或 group_id。`,
      );
    }
    if (
      assignment.group_version &&
      assignment.group_version !== selected.version
    ) {
      throw new Error(
        `标签赋值 #${index + 1} 的 group_version 与 group_id 不一致。`,
      );
    }
    return {
      ...assignment,
      group_version: selected.version,
      group_id: groupIdentity(selected),
    };
  });
  return {
    tenant_id: requiredString(value.tenant_id, "tenant_id"),
    merge_strategy:
      strategy === "union" ||
      strategy === "intersection" ||
      strategy === "priority" ||
      strategy === "manual_wins"
        ? strategy
        : "manual_wins",
    groups,
    assignments,
    trend_granularity:
      granularity === "week" || granularity === "month" ? granularity : "day",
    top_n_co_occurrences:
      typeof value.top_n_co_occurrences === "number"
        ? value.top_n_co_occurrences
        : 50,
  };
}

function ErrorText({ children }: { children: string }) {
  return (
    <p className="ag-form-error" role="alert">
      {children}
    </p>
  );
}

interface DatabaseFilterDraft {
  storeIds: string;
  agentNames: string;
  scenario: "" | ReceptionScenario;
  startedFrom: string;
  startedTo: string;
  receptionIds: string;
  groupKeys: string;
  groupIds: string;
  mergeStrategy: TagMergeStrategy;
  trendGranularity: TrendGranularity;
}

const DEFAULT_DATABASE_REQUEST: ReceptionTagInsightsRequest = {
  page: 1,
  page_size: 20,
  assignment_limit: 1_000,
  matrix_limit: 96,
  difference_limit: 128,
  evidence_summary_limit: 256,
  merge_strategy: "manual_wins",
  trend_granularity: "day",
  top_n_co_occurrences: 50,
};

const DEFAULT_DATABASE_FILTERS: DatabaseFilterDraft = {
  storeIds: "",
  agentNames: "",
  scenario: "",
  startedFrom: "",
  startedTo: "",
  receptionIds: "",
  groupKeys: "",
  groupIds: "",
  mergeStrategy: "manual_wins",
  trendGranularity: "day",
};

function splitFilterValues(value: string): string[] {
  return [
    ...new Set(
      value
        .split(/[,，\n]/)
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  ];
}

function parseReceptionIds(value: string): number[] {
  const values = splitFilterValues(value);
  const ids = values.map(Number);
  if (ids.some((id) => !Number.isSafeInteger(id) || id <= 0)) {
    throw new Error("接待 ID 必须是以逗号分隔的正整数。");
  }
  return ids;
}

function assertFilterLimit(
  label: string,
  values: readonly unknown[],
  limit: number,
): void {
  if (values.length > limit) {
    throw new Error(`${label}最多支持 ${limit} 项。`);
  }
}

function parseExactGroupIds(value: string): string[] {
  const values = splitFilterValues(value);
  assertFilterLimit("精确标签组版本", values, 8);
  const componentPattern = /^[\p{L}\p{N}_.-]+$/u;
  values.forEach((groupId) => {
    const parts = groupId.split("@");
    if (
      parts.length !== 2 ||
      parts.some(
        (part) =>
          !part ||
          part.length > 64 ||
          !componentPattern.test(part),
      )
    ) {
      throw new Error(
        `精确标签组版本“${groupId}”无效，请使用 key@version。`,
      );
    }
  });
  return values;
}

function optionalIsoDate(value: string): string | undefined {
  if (!value) return undefined;
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) {
    throw new Error("时间筛选格式无效。");
  }
  return new Date(timestamp).toISOString();
}

function buildDatabaseRequest(
  draft: DatabaseFilterDraft,
): ReceptionTagInsightsRequest {
  const storeIds = splitFilterValues(draft.storeIds);
  const agentNames = splitFilterValues(draft.agentNames);
  const receptionIds = parseReceptionIds(draft.receptionIds);
  const groupKeys = splitFilterValues(draft.groupKeys);
  const groupIds = parseExactGroupIds(draft.groupIds);
  assertFilterLimit("门店 ID", storeIds, 50);
  assertFilterLimit("销售姓名", agentNames, 50);
  assertFilterLimit("接待 ID", receptionIds, 100);
  assertFilterLimit("标签组 key", groupKeys, 20);
  if (groupKeys.length && groupIds.length) {
    throw new Error("标签组 key 与精确 key@version 不能同时填写。");
  }
  const startedFrom = optionalIsoDate(draft.startedFrom);
  const startedTo = optionalIsoDate(draft.startedTo);
  if (
    startedFrom &&
    startedTo &&
    Date.parse(startedTo) <= Date.parse(startedFrom)
  ) {
    throw new Error("结束时间必须晚于开始时间。");
  }
  return {
    ...(storeIds.length ? { store_id: storeIds } : {}),
    ...(agentNames.length ? { agent_name: agentNames } : {}),
    ...(draft.scenario ? { scenario: [draft.scenario] } : {}),
    ...(startedFrom ? { started_from: startedFrom } : {}),
    ...(startedTo ? { started_to: startedTo } : {}),
    ...(receptionIds.length ? { reception_id: receptionIds } : {}),
    ...(groupKeys.length ? { group_key: groupKeys } : {}),
    ...(groupIds.length ? { group_id: groupIds } : {}),
    page: 1,
    page_size: 20,
    assignment_limit: 1_000,
    matrix_limit: 96,
    difference_limit: 128,
    evidence_summary_limit: 256,
    merge_strategy: draft.mergeStrategy,
    trend_granularity: draft.trendGranularity,
    top_n_co_occurrences: 50,
  };
}

function evidenceString(
  evidence: Record<string, unknown>,
  key: string,
): string | null {
  const value = evidence[key];
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function evidenceIdentifier(
  evidence: Record<string, unknown>,
  key: string,
): string | null {
  const value = evidence[key];
  return typeof value === "string" || typeof value === "number"
    ? String(value)
    : null;
}

function evidenceMilliseconds(
  evidence: Record<string, unknown>,
): number | null {
  for (const key of ["timeline_start_ms", "start_ms", "source_start_ms"]) {
    const value = evidence[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  for (const key of ["timeline_start_sec", "start_sec", "source_start_sec"]) {
    const value = evidence[key];
    if (typeof value === "number" && Number.isFinite(value)) {
      return value * 1_000;
    }
  }
  return null;
}

function PersistedEvidence({
  items,
}: {
  items: ReceptionTagEvidenceSummary[];
}) {
  if (items.length === 0) return null;
  return (
    <section
      className="ag-insight-section ag-persisted-evidence"
      aria-labelledby="persisted-evidence-title"
    >
      <div className="ag-insight-section__heading">
        <div>
          <h2 id="persisted-evidence-title">持久化证据摘要</h2>
          <p>直接来自所选 key@version 标签赋值，可回到接待工作台定位原音与文本。</p>
        </div>
        <span>{items.length} 条赋值</span>
      </div>
      <ol>
        {items.slice(0, 40).map((item) => (
          <li
            key={`${item.reception_id}-${item.dialogue_unit_id}-${item.group_id}-${item.label_key}`}
          >
            <div>
              <strong>
                {item.label_key} = {item.label_value}
              </strong>
              <span>
                {item.group_id} · 接待 {item.reception_id} / 对话{" "}
                {item.dialogue_unit_id}
              </span>
              <small>
                置信度 {formatPercent(item.confidence)} · {item.evidence_count}{" "}
                条证据
              </small>
            </div>
            <ul>
              {item.evidence_refs.map((evidence, index) => {
                const recordingId = evidenceIdentifier(
                  evidence,
                  "recording_id",
                );
                const milliseconds = evidenceMilliseconds(evidence);
                const excerpt =
                  evidenceString(evidence, "text_excerpt") ??
                  evidenceString(evidence, "text");
                const refId =
                  evidenceIdentifier(evidence, "ref_id") ??
                  `${item.dialogue_unit_id}-${index}`;
                return (
                  <li key={refId}>
                    <span>
                      {recordingId ? `录音 ${recordingId}` : "对话时间窗证据"}
                    </span>
                    {excerpt && <blockquote>{excerpt}</blockquote>}
                    <Link
                      to={
                        recordingId
                          ? `/receptions/${item.reception_id}/workspace?recording=${encodeURIComponent(recordingId)}&at=${Math.max(milliseconds ?? 0, 0)}`
                          : `/receptions/${item.reception_id}/workspace`
                      }
                    >
                      到接待调听定位
                    </Link>
                  </li>
                );
              })}
            </ul>
          </li>
        ))}
      </ol>
    </section>
  );
}

function InsightResult({
  result,
  persisted,
}: {
  result: AnalyzeTagInsightsResponse;
  persisted?: ReceptionTagInsightsResponse;
}) {
  const [activeSection, setActiveSection] = useState<InsightSectionId>(
    INSIGHT_SECTIONS[0].id,
  );
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);

  const selectTab = (index: number): void => {
    const nextIndex =
      (index + INSIGHT_SECTIONS.length) % INSIGHT_SECTIONS.length;
    setActiveSection(INSIGHT_SECTIONS[nextIndex].id);
    tabRefs.current[nextIndex]?.focus();
  };

  const handleTabKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
    index: number,
  ): void => {
    let nextIndex: number | null = null;
    switch (event.key) {
      case "ArrowLeft":
        nextIndex = index - 1;
        break;
      case "ArrowRight":
        nextIndex = index + 1;
        break;
      case "Home":
        nextIndex = 0;
        break;
      case "End":
        nextIndex = INSIGHT_SECTIONS.length - 1;
        break;
      default:
        return;
    }
    event.preventDefault();
    selectTab(nextIndex);
  };

  return (
    <div className="ag-insight-results" aria-live="polite">
      {result.truncated && (
        <section className="ag-output-budget-notice" role="status">
          <strong>洞察输出已按预算截断</strong>
          <span>
            总览仍基于全部 {result.overview.assignment_count} 条标签；矩阵返回{" "}
            {result.output_budget.matrix_returned_rows} /{" "}
            {result.output_budget.matrix_total_rows} 行，差异明细返回{" "}
            {result.output_budget.difference_returned_items} /{" "}
            {result.output_budget.difference_total_items} 条。
          </span>
          <small>
            证据指针 {result.output_budget.evidence_ref_count} /{" "}
            {result.output_budget.evidence_ref_limit}
            {persisted?.evidence_summary_truncated
              ? ` · 证据摘要 ${persisted.evidence_summary_count} / ${persisted.evidence_summary_total}`
              : ""}
          </small>
        </section>
      )}
      {persisted && (
        <div className="ag-database-result-meta">
          <span>
            命中 {persisted.total_receptions} 个接待，本页{" "}
            {persisted.returned_reception_ids.length} 个
          </span>
          <span>
            已载入 {persisted.assignment_count} / 本页匹配{" "}
            {persisted.total_assignments} 条标签赋值
          </span>
          <span>
            {persisted.selection_mode === "exact_versions"
              ? "历史版本精确对比"
              : "仅当前版本"}
            {persisted.selected_group_ids.length > 0
              ? ` · ${persisted.selected_group_ids.join(", ")}`
              : ""}
          </span>
          <span>
            生成于 {new Date(persisted.generated_at).toLocaleString()}
          </span>
          {(persisted.truncated || persisted.group_truncated) && (
            <strong>
              <span>结果已截断</span>
              {persisted.group_truncated ? " · 仅展示最近 8 组/版本" : ""}
            </strong>
          )}
        </div>
      )}
      <nav
        className="ag-insight-view-nav"
        role="tablist"
        aria-label="标签洞察视图"
      >
        {INSIGHT_SECTIONS.map((section, index) => (
          <button
            type="button"
            role="tab"
            key={section.id}
            id={insightTabId(section.id)}
            ref={(element) => {
              tabRefs.current[index] = element;
            }}
            className={
              activeSection === section.id ? "is-active" : undefined
            }
            aria-controls={section.id}
            aria-selected={activeSection === section.id}
            tabIndex={activeSection === section.id ? 0 : -1}
            onClick={() => setActiveSection(section.id)}
            onKeyDown={(event) => handleTabKeyDown(event, index)}
          >
            {section.label}
          </button>
        ))}
        <span>图谱看关系 · 矩阵看证据 · 图表看趋势</span>
      </nav>

      {activeSection === "tag-relationship-graph" && (
        <section
          id="tag-relationship-graph"
          className="ag-insight-tab-panel ag-tag-graph-slot"
          role="tabpanel"
          aria-labelledby={insightTabId("tag-relationship-graph")}
          tabIndex={0}
        >
          <TagInsightGraph
            result={result}
            receptionId={
              persisted?.returned_reception_ids.length === 1
                ? persisted.returned_reception_ids[0]
                : undefined
            }
          />
        </section>
      )}

      {activeSection === "tag-comparison-matrix" && (
        <section
          id="tag-comparison-matrix"
          className="ag-insight-tab-panel ag-insight-matrix-panel"
          role="tabpanel"
          aria-labelledby={insightTabId("tag-comparison-matrix")}
          tabIndex={0}
        >
          <section className="ag-kpi-grid" aria-label="标签洞察概览">
            <article>
              <span>标签组</span>
              <strong>{result.overview.group_count}</strong>
              <small>{result.merge_strategy}</small>
            </article>
            <article>
              <span>对齐单元</span>
              <strong>{result.overview.total_cells}</strong>
              <small>完整 {result.overview.complete_cells}</small>
            </article>
            <article
              className={
                result.overview.conflict_cells > 0 ? "is-warning" : undefined
              }
            >
              <span>冲突单元</span>
              <strong>{result.overview.conflict_cells}</strong>
              <small>{formatPercent(result.overview.conflict_rate)}</small>
            </article>
            <article
              className={
                result.overview.incomplete_cells > 0
                  ? "is-warning"
                  : undefined
              }
            >
              <span>缺失单元</span>
              <strong>{result.overview.incomplete_cells}</strong>
              <small>需复核或补标</small>
            </article>
          </section>

          <section className="ag-coverage-strip" aria-label="标签组覆盖率">
            {result.coverage.map((item) => (
              <div key={item.group_key}>
                <span>
                  <strong>{item.group_key}</strong>
                  <small>
                    覆盖 {item.assigned_cells} / 缺失 {item.missing_cells}
                  </small>
                </span>
                <i>
                  <b style={{ width: `${item.coverage_rate * 100}%` }} />
                </i>
                <strong>{formatPercent(item.coverage_rate)}</strong>
              </div>
            ))}
          </section>
          <TagMatrix groups={result.groups} rows={result.matrix} />
          {persisted && (
            <PersistedEvidence items={persisted.evidence_summary} />
          )}
        </section>
      )}

      {activeSection === "tag-chart-insights" && (
        <section
          id="tag-chart-insights"
          className="ag-insight-tab-panel ag-insight-charts-panel"
          role="tabpanel"
          aria-labelledby={insightTabId("tag-chart-insights")}
          tabIndex={0}
        >
          <InsightVisuals result={result} />
        </section>
      )}
    </div>
  );
}

export default function TagInsightsPage() {
  const user = useAuthStore((state) => state.user);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [databaseFilters, setDatabaseFilters] = useState<DatabaseFilterDraft>(
    DEFAULT_DATABASE_FILTERS,
  );
  const [databaseRequest, setDatabaseRequest] =
    useState<ReceptionTagInsightsRequest>(DEFAULT_DATABASE_REQUEST);
  const [databaseRevision, setDatabaseRevision] = useState(0);
  const [databaseFilterError, setDatabaseFilterError] = useState<string | null>(
    null,
  );
  const [activeSource, setActiveSource] = useState<"database" | "snapshot">(
    "database",
  );
  const [rawSnapshot, setRawSnapshot] = useState("");
  const [snapshot, setSnapshot] = useState<AnalyzeTagInsightsRequest | null>(
    null,
  );
  const [selectedGroups, setSelectedGroups] = useState<Set<string>>(new Set());
  const [mergeStrategy, setMergeStrategy] =
    useState<TagMergeStrategy>("manual_wins");
  const [trendGranularity, setTrendGranularity] =
    useState<TrendGranularity>("day");
  const [importError, setImportError] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const persistedQuery = useQuery({
    queryKey: ["reception-tag-insights", databaseRequest, databaseRevision],
    queryFn: () => getReceptionTagInsights(databaseRequest),
    retry: false,
  });

  const analysisMutation = useMutation({
    mutationFn: (body: AnalyzeTagInsightsRequest) => analyzeTagInsights(body),
    onSuccess: () => setActiveSource("snapshot"),
    onError: (error) => {
      setSubmitError(
        error instanceof Error ? error.message : "标签分析接口暂不可用",
      );
    },
  });

  const submitDatabaseFilters = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setDatabaseFilterError(null);
    try {
      setDatabaseRequest(buildDatabaseRequest(databaseFilters));
      setDatabaseRevision((value) => value + 1);
      setActiveSource("database");
    } catch (error) {
      setDatabaseFilterError(
        error instanceof Error ? error.message : "筛选条件无效",
      );
    }
  };

  const goToPage = (page: number) => {
    setDatabaseRequest((current) => ({
      ...current,
      page: Math.max(1, page),
    }));
    setDatabaseRevision((value) => value + 1);
    setActiveSource("database");
  };

  const selectedStrategy = MERGE_STRATEGIES.find(
    (item) => item.key === mergeStrategy,
  )!;
  const selectedAssignments = useMemo(
    () =>
      snapshot?.assignments.filter((assignment) =>
        selectedGroups.has(assignment.group_id ?? assignment.group_key),
      ) ?? [],
    [selectedGroups, snapshot],
  );

  const loadSnapshot = (raw: string) => {
    setImportError(null);
    setSubmitError(null);
    analysisMutation.reset();
    try {
      const parsed = parseTagSnapshot(raw);
      if (user?.tenant_id && parsed.tenant_id !== user.tenant_id) {
        throw new Error(
          `快照租户 ${parsed.tenant_id} 与当前租户 ${user.tenant_id} 不一致。`,
        );
      }
      setSnapshot(parsed);
      setSelectedGroups(new Set(parsed.groups.map(groupIdentity)));
      setMergeStrategy(parsed.merge_strategy);
      setTrendGranularity(parsed.trend_granularity);
    } catch (error) {
      setSnapshot(null);
      setSelectedGroups(new Set());
      setImportError(
        error instanceof Error ? error.message : "标签快照格式错误",
      );
    }
  };

  const handleFile = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    if (file.size > 5 * 1024 * 1024) {
      setImportError("快照文件不能超过 5 MiB。");
      return;
    }
    const text = await file.text();
    setRawSnapshot(text);
    loadSnapshot(text);
    event.target.value = "";
  };

  const toggleGroup = (groupKey: string) => {
    setSelectedGroups((current) => {
      const next = new Set(current);
      if (next.has(groupKey)) next.delete(groupKey);
      else next.add(groupKey);
      return next;
    });
    analysisMutation.reset();
  };

  const submitAnalysis = () => {
    setSubmitError(null);
    if (!snapshot) {
      setSubmitError("请先载入真实标签快照。");
      return;
    }
    const groups = snapshot.groups.filter((group) =>
      selectedGroups.has(groupIdentity(group)),
    );
    if (groups.length === 0) {
      setSubmitError("至少选择一个标签组或版本。");
      return;
    }
    const assignments = snapshot.assignments.filter((assignment) =>
      selectedGroups.has(assignment.group_id ?? assignment.group_key),
    );
    if (assignments.length === 0) {
      setSubmitError("所选标签组没有可分析的标签赋值。");
      return;
    }
    analysisMutation.mutate({
      ...snapshot,
      merge_strategy: mergeStrategy,
      groups,
      assignments,
      trend_granularity: trendGranularity,
    });
  };

  const persisted = persistedQuery.data;
  const totalPages = persisted
    ? Math.max(1, Math.ceil(persisted.total_receptions / persisted.page_size))
    : 1;
  const activeResult =
    activeSource === "snapshot" ? analysisMutation.data : persisted?.insights;

  const compareExactVersions = (groupIds: string[]) => {
    if (groupIds.length < 2) return;
    setDatabaseFilters((current) => ({
      ...current,
      groupKeys: "",
      groupIds: groupIds.join(", "),
    }));
    setDatabaseRequest((current) => {
      const rest = { ...current };
      delete rest.group_key;
      return {
        ...rest,
        group_id: groupIds,
        page: 1,
      };
    });
    setDatabaseRevision((value) => value + 1);
    setDatabaseFilterError(null);
    setActiveSource("database");
  };

  return (
    <div className="ag-tag-insights-page">
      <InsightContextTabs />
      <header className="ag-feature-header ag-tag-insights-header">
        <div>
          <span className="ag-eyebrow">对话标签 · 多组/多版本</span>
          <h1>目标对话标签洞察</h1>
          <p>
            对齐同一接待与时间窗，合并多组标签并分析分歧、缺失、趋势和业务差异。
          </p>
        </div>
        <div className="ag-feature-header__actions">
          <span>完整聚合 · 有界返回 · 最多 8 组 / 5,000 条赋值</span>
        </div>
      </header>

      <section
        className="ag-database-insight-panel ag-tag-filter-console"
        aria-labelledby="database-insight-title"
      >
        <div className="ag-insight-section__heading">
          <div>
            <h2 id="database-insight-title">数据库实时标签洞察</h2>
            <p>
              默认只读当前标签；填写精确 key@version 后可读取并对比历史版本。筛选、分页、矩阵和证据均由后端按租户计算。
            </p>
          </div>
          <span>主分析流程</span>
        </div>
        <form onSubmit={submitDatabaseFilters}>
          <div className="ag-database-filter-grid">
            <label>
              门店 ID
              <input
                aria-label="门店 ID"
                value={databaseFilters.storeIds}
                placeholder="S1, S2"
                onChange={(event) =>
                  setDatabaseFilters((current) => ({
                    ...current,
                    storeIds: event.target.value,
                  }))
                }
              />
            </label>
            <label>
              销售姓名
              <input
                aria-label="销售姓名"
                value={databaseFilters.agentNames}
                placeholder="小林, 小周"
                onChange={(event) =>
                  setDatabaseFilters((current) => ({
                    ...current,
                    agentNames: event.target.value,
                  }))
                }
              />
            </label>
            <label>
              业务场景
              <select
                aria-label="业务场景"
                value={databaseFilters.scenario}
                onChange={(event) =>
                  setDatabaseFilters((current) => ({
                    ...current,
                    scenario: event.target.value as "" | ReceptionScenario,
                  }))
                }
              >
                <option value="">全部场景</option>
                <option value="gold">金店销售</option>
                <option value="automotive">汽车销售</option>
                <option value="custom">自定义</option>
              </select>
            </label>
            <label>
              接待 ID
              <input
                aria-label="接待 ID"
                value={databaseFilters.receptionIds}
                inputMode="numeric"
                placeholder="9, 10"
                onChange={(event) =>
                  setDatabaseFilters((current) => ({
                    ...current,
                    receptionIds: event.target.value,
                  }))
                }
              />
            </label>
            <label>
              标签组 key
              <input
                aria-label="标签组 key"
                value={databaseFilters.groupKeys}
                placeholder="model, review"
                onChange={(event) =>
                  setDatabaseFilters((current) => ({
                    ...current,
                    groupKeys: event.target.value,
                  }))
                }
              />
            </label>
            <label>
              精确标签组版本
              <input
                aria-label="精确标签组版本"
                value={databaseFilters.groupIds}
                placeholder="review@v1, review@v2"
                onChange={(event) =>
                  setDatabaseFilters((current) => ({
                    ...current,
                    groupIds: event.target.value,
                  }))
                }
              />
            </label>
            <label>
              开始时间
              <input
                type="datetime-local"
                aria-label="开始时间"
                value={databaseFilters.startedFrom}
                onChange={(event) =>
                  setDatabaseFilters((current) => ({
                    ...current,
                    startedFrom: event.target.value,
                  }))
                }
              />
            </label>
            <label>
              结束时间
              <input
                type="datetime-local"
                aria-label="结束时间"
                value={databaseFilters.startedTo}
                onChange={(event) =>
                  setDatabaseFilters((current) => ({
                    ...current,
                    startedTo: event.target.value,
                  }))
                }
              />
            </label>
            <label>
              数据库合并策略
              <select
                aria-label="数据库合并策略"
                value={databaseFilters.mergeStrategy}
                onChange={(event) =>
                  setDatabaseFilters((current) => ({
                    ...current,
                    mergeStrategy: event.target.value as TagMergeStrategy,
                  }))
                }
              >
                {MERGE_STRATEGIES.map((strategy) => (
                  <option key={strategy.key} value={strategy.key}>
                    {strategy.label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              趋势粒度
              <select
                aria-label="趋势粒度"
                value={databaseFilters.trendGranularity}
                onChange={(event) =>
                  setDatabaseFilters((current) => ({
                    ...current,
                    trendGranularity: event.target.value as TrendGranularity,
                  }))
                }
              >
                <option value="day">按日</option>
                <option value="week">按周</option>
                <option value="month">按月</option>
              </select>
            </label>
          </div>
          <div className="ag-database-filter-actions">
            <button type="submit" disabled={persistedQuery.isFetching}>
              {persistedQuery.isFetching
                ? "正在加载数据库洞察…"
                : "加载数据库洞察"}
            </button>
            <button
              type="button"
              className="is-secondary"
              onClick={() => {
                setDatabaseFilters(DEFAULT_DATABASE_FILTERS);
                setDatabaseRequest(DEFAULT_DATABASE_REQUEST);
                setDatabaseRevision((value) => value + 1);
                setDatabaseFilterError(null);
                setActiveSource("database");
              }}
            >
              重置筛选
            </button>
            {persisted && (
              <span>
                第 {persisted.page} / {totalPages} 页
              </span>
            )}
          </div>
          {databaseFilterError && <ErrorText>{databaseFilterError}</ErrorText>}
        </form>
      </section>

      {activeSource === "database" && persistedQuery.isPending && (
        <div className="ag-feature-loading" role="status">
          正在加载持久化标签矩阵…
        </div>
      )}
      {activeSource === "database" && persistedQuery.isError && (
        <div className="ag-feature-empty" role="alert">
          <strong>数据库标签洞察加载失败</strong>
          <span>
            {persistedQuery.error instanceof Error
              ? persistedQuery.error.message
              : "接口暂不可用"}
          </span>
          <button type="button" onClick={() => persistedQuery.refetch()}>
            重新加载
          </button>
        </div>
      )}
      {activeSource === "database" &&
        persisted &&
        !persistedQuery.isPending &&
        !persisted.insights && (
          <div className="ag-feature-empty ag-database-empty" role="status">
            <strong>数据库中暂无符合条件的目标标签</strong>
            <span>
              已命中 {persisted.total_receptions} 个接待，但没有
              {persisted.selection_mode === "exact_versions"
                ? "所选历史版本"
                : "当前版本"}
              标签赋值；可先在接待工作台运行“派生目标标签”或检查版本选择。
            </span>
          </div>
        )}

      {activeSource === "database" && persisted && persisted.insights && (
        <>
          <GovernanceActions
            result={persisted.insights}
            persisted={persisted}
            request={databaseRequest}
            onCompareVersions={compareExactVersions}
          />
          <InsightResult result={persisted.insights} persisted={persisted} />
          <nav className="ag-insight-pagination" aria-label="洞察分页">
            <button
              type="button"
              disabled={persisted.page <= 1 || persistedQuery.isFetching}
              onClick={() => goToPage(persisted.page - 1)}
            >
              上一页
            </button>
            <span>
              第 {persisted.page} / {totalPages} 页 · 共{" "}
              {persisted.total_receptions} 个接待
            </span>
            <button
              type="button"
              disabled={
                persisted.page >= totalPages || persistedQuery.isFetching
              }
              onClick={() => goToPage(persisted.page + 1)}
            >
              下一页
            </button>
          </nav>
        </>
      )}

      <details className="ag-advanced-snapshot">
        <summary>高级：导入 JSON 标签快照</summary>
        <p>
          仅用于离线复盘或外部标签组对比；日常分析应使用上方数据库实时流程。
        </p>
        <section className="ag-snapshot-panel" aria-labelledby="snapshot-title">
          <div className="ag-snapshot-panel__input">
            <div className="ag-insight-section__heading">
              <div>
                <h2 id="snapshot-title">载入标签快照</h2>
                <p>
                  数据会提交到 POST /tag-insights/analyze；同一 group_key 的多个
                  version 会作为独立列对比。
                </p>
              </div>
            </div>
            <textarea
              aria-label="标签快照 JSON"
              value={rawSnapshot}
              spellCheck={false}
              placeholder='{"tenant_id":"...","groups":[...],"assignments":[...]}'
              onChange={(event) => setRawSnapshot(event.target.value)}
            />
            <div className="ag-form-actions">
              <button
                type="button"
                onClick={() => loadSnapshot(rawSnapshot)}
                disabled={!rawSnapshot.trim()}
              >
                载入标签快照
              </button>
              <button
                type="button"
                className="is-secondary"
                onClick={() => fileInputRef.current?.click()}
              >
                选择 JSON 文件
              </button>
              <input
                ref={fileInputRef}
                type="file"
                accept="application/json,.json"
                hidden
                onChange={handleFile}
              />
            </div>
            {importError && <ErrorText>{importError}</ErrorText>}
          </div>

          <div className="ag-snapshot-panel__controls">
            {!snapshot ? (
              <div className="ag-snapshot-empty" role="status">
                <strong>尚未载入真实标签快照</strong>
                <span>导入后才能选择标签组/版本，并运行合并与对比分析。</span>
              </div>
            ) : (
              <>
                <div className="ag-snapshot-summary">
                  <span>
                    租户 <strong>{snapshot.tenant_id}</strong>
                  </span>
                  <span>
                    标签组 <strong>{snapshot.groups.length}</strong>
                  </span>
                  <span>
                    标签赋值 <strong>{snapshot.assignments.length}</strong>
                  </span>
                </div>
                <fieldset className="ag-group-selector">
                  <legend>标签组 / 版本多选</legend>
                  <div>
                    {snapshot.groups.map((group) => (
                      <label key={groupIdentity(group)}>
                        <input
                          type="checkbox"
                          checked={selectedGroups.has(groupIdentity(group))}
                          aria-label={`选择标签组 ${groupIdentity(group)}`}
                          onChange={() => toggleGroup(groupIdentity(group))}
                        />
                        <span>
                          <strong>{group.group_key}</strong>
                          <small>
                            {group.version} · {group.source} · P{group.priority}
                          </small>
                        </span>
                      </label>
                    ))}
                  </div>
                  <footer>
                    <button
                      type="button"
                      onClick={() => {
                        setSelectedGroups(
                          new Set(snapshot.groups.map(groupIdentity)),
                        );
                        analysisMutation.reset();
                      }}
                    >
                      全选
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        setSelectedGroups(new Set());
                        analysisMutation.reset();
                      }}
                    >
                      清空
                    </button>
                  </footer>
                </fieldset>

                <div className="ag-analysis-options">
                  <label>
                    标签合并策略
                    <select
                      aria-label="标签合并策略"
                      value={mergeStrategy}
                      onChange={(event) => {
                        setMergeStrategy(
                          event.target.value as TagMergeStrategy,
                        );
                        analysisMutation.reset();
                      }}
                    >
                      {MERGE_STRATEGIES.map((strategy) => (
                        <option key={strategy.key} value={strategy.key}>
                          {strategy.label}
                        </option>
                      ))}
                    </select>
                    <small>{selectedStrategy.help}</small>
                  </label>
                  <label>
                    快照趋势粒度
                    <select
                      aria-label="快照趋势粒度"
                      value={trendGranularity}
                      onChange={(event) => {
                        setTrendGranularity(
                          event.target.value as TrendGranularity,
                        );
                        analysisMutation.reset();
                      }}
                    >
                      <option value="day">按日</option>
                      <option value="week">按周</option>
                      <option value="month">按月</option>
                    </select>
                  </label>
                </div>
                <button
                  type="button"
                  className="ag-analyze-button"
                  disabled={
                    selectedGroups.size === 0 || analysisMutation.isPending
                  }
                  onClick={submitAnalysis}
                >
                  {analysisMutation.isPending
                    ? "正在分析…"
                    : "运行合并与对比分析"}
                </button>
                <p className="ag-selection-summary">
                  已选 {selectedGroups.size} 组 / {selectedAssignments.length}{" "}
                  条赋值
                </p>
                {submitError && <ErrorText>{submitError}</ErrorText>}
              </>
            )}
          </div>
        </section>
      </details>

      {activeSource === "snapshot" && activeResult && (
        <>
          <div className="ag-source-notice">
            当前展示高级 JSON 快照分析结果。
            <button
              type="button"
              disabled={!persisted?.insights}
              onClick={() => setActiveSource("database")}
            >
              返回数据库结果
            </button>
          </div>
          <GovernanceActions
            result={activeResult}
            request={databaseRequest}
            onCompareVersions={compareExactVersions}
          />
          <InsightResult result={activeResult} />
        </>
      )}
    </div>
  );
}

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { PanelState } from "@/components/PanelState";
import { Metric } from "@/components/governance/Metric";
import { StatusChip } from "@/components/governance/StatusChip";
import {
  compactPercent,
  numericMetric,
  signedPercent,
} from "@/components/governance/format";
import { listTagEvaluations, promotePromptArtifact } from "@/api/services";
import type { PromptArtifactSummary, TagEvaluation } from "@/types/api";
import { getErrorMessage, getErrorStatus } from "@/utils/errors";

const TERMINAL_STATUSES = new Set(["completed", "failed"]);

interface LabelDelta {
  tagKey: string;
  baseline: number;
  candidate: number;
  delta: number;
}

function labelDeltas(evaluation: TagEvaluation): LabelDelta[] {
  const candidate = evaluation.supported_label_f1 ?? {};
  const baseline = evaluation.baseline_label_f1 ?? {};
  const tagKeys = [...new Set([...Object.keys(candidate), ...Object.keys(baseline)])];
  return tagKeys
    .map((tagKey) => {
      const before = baseline[tagKey] ?? 0;
      const after = candidate[tagKey] ?? 0;
      return { tagKey, baseline: before, candidate: after, delta: after - before };
    })
    .sort((a, b) => Math.abs(b.delta) - Math.abs(a.delta));
}

function FlipList({ deltas }: { deltas: LabelDelta[] }) {
  const regressed = deltas.filter((item) => item.delta < 0).sort((a, b) => a.delta - b.delta);
  const improved = deltas.filter((item) => item.delta > 0).sort((a, b) => b.delta - a.delta);

  const renderItem = (item: LabelDelta) => (
    <li key={item.tagKey}>
      <strong>{item.tagKey}</strong>
      <span>{signedPercent(item.delta)}</span>
      <small>
        {compactPercent(item.baseline)} → {compactPercent(item.candidate)}
      </small>
    </li>
  );

  return (
    <div className="ag-plab-flip-list">
      {/* 从对变错默认展开：这是最需要人看见的一类。 */}
      <details open className="is-regression">
        <summary>从对变错（{regressed.length}）</summary>
        {regressed.length === 0 ? (
          <p className="ag-compact-state">没有标签的 F1 下降。</p>
        ) : (
          <ul>{regressed.map(renderItem)}</ul>
        )}
      </details>
      <details className="is-improvement">
        <summary>从错变对（{improved.length}）</summary>
        {improved.length === 0 ? (
          <p className="ag-compact-state">没有标签的 F1 提升。</p>
        ) : (
          <ul>{improved.map(renderItem)}</ul>
        )}
      </details>
      <p className="ag-compact-state">
        以上按标签 F1 变化推导。逐样本的翻转清单需要评估接口返回样本级结果，当前
        接口未提供。
      </p>
    </div>
  );
}

/** 与后端 PromoteArtifactCreate 同步：^[\w.-]+$，1–32 字。 */
const VERSION_SUFFIX_PATTERN = /^[\w.-]{1,32}$/;

export function ReplayPanel({
  artifact,
  isAdmin,
  onGoToCompile,
}: {
  artifact: PromptArtifactSummary | undefined;
  isAdmin: boolean;
  onGoToCompile: () => void;
}) {
  const queryClient = useQueryClient();
  const [versionSuffix, setVersionSuffix] = useState("");
  const [changeSummary, setChangeSummary] = useState("");

  const promote = useMutation({
    mutationFn: (payload: { artifactId: number }) =>
      promotePromptArtifact(payload.artifactId, {
        version_suffix: versionSuffix.trim(),
        change_summary: changeSummary.trim(),
      }),
    onSuccess: () => {
      // 列表缓存与深链兜底详情都带着 candidate_tagger_version_id，两个都要刷。
      void queryClient.invalidateQueries({ queryKey: ["prompt-lab", "artifacts"] });
      void queryClient.invalidateQueries({ queryKey: ["prompt-lab", "artifact"] });
    },
  });

  const evaluations = useQuery({
    queryKey: ["tag-governance", "evaluations"],
    queryFn: listTagEvaluations,
    enabled: artifact?.candidate_tagger_version_id != null,
    retry: false,
    refetchInterval: (query) =>
      (query.state.data?.items ?? []).some(
        (item) => item.status && !TERMINAL_STATUSES.has(item.status),
      )
        ? 3_000
        : false,
  });

  const matched = useMemo(() => {
    const versionId = artifact?.candidate_tagger_version_id;
    if (versionId == null) return undefined;
    return (evaluations.data?.items ?? [])
      .filter((item) => item.tagger_version_id === versionId)
      .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at))[0];
  }, [artifact, evaluations.data]);

  if (!artifact) {
    return (
      <div className="ag-plab-empty">
        <p>先在「编译运行」里选择一个产物。</p>
        <button type="button" onClick={onGoToCompile}>
          前往编译运行
        </button>
      </div>
    );
  }

  if (artifact.candidate_tagger_version_id == null) {
    // 这是最常见的状态，要做得像「流程下一步」而不是「没数据」。
    // 晋级刚成功、列表缓存还没刷新时，先展示铸出的版本，别让人再点一次。
    if (promote.data) {
      return (
        <div className="ag-plab-empty">
          <p role="status">
            已晋级为候选抽取版本{" "}
            <strong>#{promote.data.candidate_tagger_version.id}</strong>（
            {promote.data.candidate_tagger_version.version}）。候选仍是 draft，
            需在标签治理中心用冻结金标集完成评估并通过门禁后才能部署。
          </p>
          <Link to="/tag-governance?tab=taggers">前往标签治理查看候选版本</Link>
        </div>
      );
    }

    if (!isAdmin) {
      // 对非管理员诚实：不是没数据，而是差一步需要管理员执行的晋级。
      return (
        <div className="ag-plab-empty">
          <p>
            该产物尚未晋级为候选抽取版本，因此还没有回放结果。晋级会铸出一个
            draft 抽取版本供评估使用，该操作需要管理员权限——请在「梯度与补丁」
            完成审阅后，联系管理员在本页发起晋级。
          </p>
        </div>
      );
    }

    const suffixValid = VERSION_SUFFIX_PATTERN.test(versionSuffix.trim());
    const summaryValid = changeSummary.trim().length >= 8;
    return (
      <div className="ag-plab-empty ag-plab-promote">
        <p>
          该产物尚未晋级为候选抽取版本，因此还没有回放结果。在「梯度与补丁」里
          确认要采纳的补丁后，在此晋级：会铸出一个 draft 候选版本，仍需通过
          评估与部署门禁，不会直通生产。
        </p>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (suffixValid && summaryValid && !promote.isPending) {
              promote.mutate({ artifactId: artifact.id });
            }
          }}
        >
          <label className="ag-plab-inline-field">
            <span>版本后缀（字母、数字、._-，≤32 字）</span>
            <input
              aria-label="版本后缀"
              value={versionSuffix}
              maxLength={32}
              placeholder={`如 r${artifact.id}`}
              onChange={(event) => setVersionSuffix(event.target.value)}
            />
          </label>
          <label className="ag-plab-inline-field">
            <span>变更说明（至少 8 字）</span>
            <textarea
              aria-label="变更说明"
              rows={2}
              maxLength={4000}
              value={changeSummary}
              onChange={(event) => setChangeSummary(event.target.value)}
            />
          </label>
          <button type="submit" disabled={!suffixValid || !summaryValid || promote.isPending}>
            {promote.isPending ? "正在晋级…" : "晋级为候选版本"}
          </button>
        </form>
        {promote.isError && (
          <p className="ag-inline-feedback is-error" role="alert">
            {getErrorStatus(promote.error) === 409
              ? "版本号或配置与已有抽取版本冲突，请换一个版本后缀后重试。"
              : getErrorMessage(promote.error, "晋级失败，请稍后重试。")}
          </p>
        )}
      </div>
    );
  }

  const deltas = matched ? labelDeltas(matched) : [];

  return (
    <div className="ag-plab-replay">
      <p className="ag-compact-state">
        已晋级为候选抽取版本 <strong>#{artifact.candidate_tagger_version_id}</strong>，
        <Link to="/tag-governance?tab=taggers">在标签治理中查看</Link>。
      </p>
      <PanelState
        pending={evaluations.isPending}
        error={evaluations.error}
        empty={matched === undefined}
        emptyTitle="候选版本还没有评估结果"
        emptyDescription="在标签治理中心用冻结金标集对候选版本发起一次评估。"
        onRetry={() => void evaluations.refetch()}
      >
        {matched && (
          <>
            <section className="ag-governance-card">
              <header>
                <div>
                  <span className="ag-card-kicker">EVALUATION</span>
                  <h2>评估 #{matched.id}</h2>
                </div>
                {matched.status && !TERMINAL_STATUSES.has(matched.status) ? (
                  <StatusChip status={matched.status} />
                ) : (
                  <span
                    className={`ag-gate-badge ${matched.passed ? "is-pass" : "is-fail"}`}
                    role="status"
                    aria-label={matched.passed ? "门禁通过" : "门禁拦截"}
                  >
                    {matched.passed ? "门禁通过" : "门禁拦截"}
                  </span>
                )}
              </header>
              <div className="ag-quality-grid">
                <Metric label="Macro F1" value={numericMetric(matched.metrics.macro_f1) ?? undefined} />
                <Metric
                  label="关键标签召回"
                  value={numericMetric(matched.metrics.critical_recall) ?? undefined}
                />
                <Metric
                  label="证据覆盖"
                  value={numericMetric(matched.metrics.evidence_coverage) ?? undefined}
                />
                <Metric
                  label="错误率"
                  value={numericMetric(matched.metrics.error_rate) ?? undefined}
                  inverse
                />
              </div>
            </section>

            <section className="ag-governance-card">
              <header>
                <div>
                  <span className="ag-card-kicker">PER-LABEL</span>
                  <h2>按标签的变化</h2>
                  {/* 接口按标签只返回 F1，不能按 tag 编造 P/R。 */}
                  <p>
                    接口按标签只返回 F1；精确率与召回率为全量聚合值：精确率{" "}
                    {compactPercent(numericMetric(matched.baseline_metrics.precision))} → 候选{" "}
                    {compactPercent(numericMetric(matched.metrics.precision))}，召回率{" "}
                    {compactPercent(numericMetric(matched.baseline_metrics.recall))} → 候选{" "}
                    {compactPercent(numericMetric(matched.metrics.recall))}。
                  </p>
                </div>
              </header>
              {deltas.length === 0 ? (
                <p className="ag-compact-state">本次评估没有返回逐标签的 F1。</p>
              ) : (
                <div className="ag-version-table-wrap">
                  <table className="ag-version-table">
                    <thead>
                      <tr>
                        <th>标签</th>
                        <th>基线 F1</th>
                        <th>候选 F1</th>
                        <th>变化</th>
                      </tr>
                    </thead>
                    <tbody>
                      {deltas.map((item) => (
                        <tr key={item.tagKey}>
                          <td>{item.tagKey}</td>
                          <td>{compactPercent(item.baseline)}</td>
                          <td>{compactPercent(item.candidate)}</td>
                          <td
                            className={
                              item.delta <= -0.01
                                ? "is-worse"
                                : item.delta >= 0.01
                                  ? "is-better"
                                  : undefined
                            }
                          >
                            {signedPercent(item.delta)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {deltas.length > 0 && <FlipList deltas={deltas} />}
            </section>

            <section className="ag-governance-card">
              <header>
                <div>
                  <span className="ag-card-kicker">GATES</span>
                  <h2>门禁</h2>
                </div>
              </header>
              <ul className="ag-gate-list">
                {matched.gates.map((gate) => (
                  <li key={gate.code}>
                    <span className={`ag-gate-badge ${gate.passed ? "is-pass" : "is-fail"}`}>
                      {gate.passed ? "通过" : "未过"}
                    </span>
                    <strong>{gate.code}</strong>
                    <span>{gate.message}</span>
                    <small>
                      {compactPercent(gate.actual)} / 阈值 {compactPercent(gate.threshold)}
                    </small>
                  </li>
                ))}
                {/* 输入预算是 prompt-lab 特有的拦截原因，评估门禁看不到它。 */}
                <li>
                  <span
                    className={`ag-gate-badge ${
                      artifact.input_budget_report.fits ? "is-pass" : "is-fail"
                    }`}
                  >
                    {artifact.input_budget_report.fits ? "通过" : "未过"}
                  </span>
                  <strong>input_budget</strong>
                  <span>候选 Prompt 与 schema 必须放得进单次调用的输入预算。</span>
                  <small>
                    {artifact.input_budget_report.fixed_tokens} /{" "}
                    {artifact.input_budget_report.usable_tokens} token
                  </small>
                </li>
              </ul>
            </section>
          </>
        )}
      </PanelState>
    </div>
  );
}

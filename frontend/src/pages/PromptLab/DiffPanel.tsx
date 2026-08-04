import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { PanelState } from "@/components/PanelState";
import { StatusChip } from "@/components/governance/StatusChip";
import {
  compactCount,
  compactPercent,
  signedCount,
} from "@/components/governance/format";
import {
  diffPromptArtifact,
  type AttributedHunk,
  type DiffLine,
} from "@/components/governance/textDiff";
import { decidePromptPatches, getPromptArtifactDiff } from "@/api/services";
import type { PromptArtifact, PromptArtifactDiff } from "@/types/api";
import { getErrorMessage, getErrorStatus } from "@/utils/errors";

import { PATCH_KIND_LABELS, PROMPT_LAB_STATUS_LABELS } from "./labels";

/**
 * 决策集合表达的是「最终采纳集」而非「本次改动」。
 *
 * 服务端的 rematerialize 会把未列出的补丁当成拒绝而移除，所以哪怕只是剔除一条
 * 示例，也必须把全部补丁按当前采纳状态原样重放。漏掉一条就会静默丢补丁——这是
 * 这个界面最容易写错的地方。
 */
function replayDecisions(
  diff: PromptArtifactDiff,
  overrides: ReadonlyMap<string, boolean> = new Map(),
): { patch_id: string; decision: "accepted" | "rejected" }[] {
  const accepted = new Set(diff.accepted_patch_ids);
  return diff.patches.map((patch) => ({
    patch_id: patch.patch_id,
    decision: (overrides.get(patch.patch_id) ?? accepted.has(patch.patch_id))
      ? ("accepted" as const)
      : ("rejected" as const),
  }));
}

function CostBar({ diff }: { diff: PromptArtifactDiff }) {
  const budget = diff.input_budget_report;
  // 与基线可比的只有「单次固定开销」：候选 Prompt 的 token 估算只是策略正文，
  // 拿它去减基线的固定开销（含 schema）会把「更贵」算成「省了」。
  const fixedDelta = diff.fixed_token_delta;
  return (
    <section className="ag-plab-cost-bar">
      <dl>
        <div>
          <dt>候选 Prompt</dt>
          <dd>{compactCount(diff.prompt_token_estimate)} token</dd>
          <small>仅策略正文</small>
        </div>
        <div>
          <dt>单次固定开销</dt>
          <dd>{compactCount(budget.fixed_tokens)} token</dd>
          <small>基线 {compactCount(budget.baseline_fixed_tokens)}</small>
        </div>
        <div>
          <dt>固定开销变化</dt>
          <dd
            className={
              fixedDelta === null || fixedDelta === 0
                ? undefined
                : fixedDelta > 0
                  ? "is-worse"
                  : "is-better"
            }
          >
            {signedCount(fixedDelta)}
          </dd>
        </div>
        <div>
          <dt>剩余余量变化</dt>
          {/* 余量减少是坏事，配色与固定开销变化相反。 */}
          <dd className={budget.headroom_delta < 0 ? "is-worse" : "is-better"}>
            {signedCount(budget.headroom_delta)}
          </dd>
        </div>
        <div>
          <dt>余量收缩</dt>
          <dd>{compactPercent(budget.headroom_shrink_ratio)}</dd>
        </div>
        <div>
          <dt>输入预算</dt>
          <dd>
            <span
              className={`ag-gate-badge ${budget.fits ? "is-pass" : "is-fail"}`}
              role="status"
              aria-label={budget.fits ? "在输入预算内" : "超出输入预算"}
            >
              {budget.fits ? "预算内" : "超出预算"}
            </span>
          </dd>
        </div>
      </dl>
      <p className="ag-compact-state">
        余量收缩会让长对话被拆成更多次调用；即使 Prompt 本身更好，也可能因此被效率
        门禁拦下。当前每次调用可用 {compactCount(budget.usable_tokens)} token，
        其中 Prompt 与 schema 已占用 {compactCount(budget.fixed_tokens)}。
      </p>
    </section>
  );
}

function DiffSide({
  lines,
  side,
}: {
  lines: DiffLine[];
  side: "baseline" | "candidate";
}) {
  const visible = side === "baseline" ? "delete" : "insert";
  return (
    <ol className="ag-plab-diff__lines">
      {lines.map((line, index) => {
        const shown = line.op === "equal" || line.op === visible;
        const lineNumber =
          side === "baseline" ? line.baselineLine : line.candidateLine;
        if (!shown) {
          // 对侧占位，保持左右行号视觉对齐。
          return (
            <li key={index} className="ag-plab-diff__line is-spacer" aria-hidden="true" />
          );
        }
        return (
          <li
            key={index}
            className={`ag-plab-diff__line is-${line.op}`}
            aria-label={`第 ${(lineNumber ?? 0) + 1} 行${
              line.op === "equal" ? "" : line.op === "insert" ? "，新增" : "，删除"
            }`}
          >
            <span className="ag-plab-diff__gutter" aria-hidden="true">
              {lineNumber === null ? "" : lineNumber + 1}
            </span>
            <span className="ag-plab-diff__text">
              {line.spans
                ? line.spans.map((span, spanIndex) => (
                    <span
                      key={spanIndex}
                      className={span.changed ? "ag-plab-diff__span is-changed" : undefined}
                    >
                      {span.text}
                    </span>
                  ))
                : line.text || " "}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

function HunkAttribution({
  hunk,
  patchKindById,
}: {
  hunk: AttributedHunk;
  patchKindById: ReadonlyMap<string, string>;
}) {
  const attribution = hunk.attribution;
  if (!attribution || attribution.kind === "header") return null;
  const label =
    attribution.kind === "patch"
      ? `补丁 ${attribution.id?.slice(0, 8)} · ${
          patchKindById.get(attribution.id ?? "") ?? "规则"
        }`
      : attribution.kind === "demo"
        ? `示例 ${attribution.id?.slice(0, 8)}`
        : "示例段";
  return (
    <span
      className={`ag-plab-hunk-chip${attribution.ambiguous ? " is-ambiguous" : ""}`}
    >
      {label}
      {attribution.ambiguous && "（位置邻近）"}
    </span>
  );
}

export function DiffPanel({
  artifactId,
  isAdmin,
  onArtifactCreated,
  onClearArtifact,
  onGoToCompile,
}: {
  artifactId: number | null;
  isAdmin: boolean;
  onArtifactCreated: (artifact: PromptArtifact) => void;
  onClearArtifact: () => void;
  onGoToCompile: () => void;
}) {
  const queryClient = useQueryClient();
  const [dropped, setDropped] = useState<Set<string>>(new Set());

  const diff = useQuery({
    queryKey: ["prompt-lab", "artifact-diff", artifactId],
    queryFn: () => getPromptArtifactDiff(artifactId as number),
    enabled: artifactId !== null,
    retry: false,
  });

  const analysis = useMemo(() => {
    if (!diff.data) return null;
    return diffPromptArtifact({
      baselinePrompt: diff.data.baseline_prompt,
      candidatePrompt: diff.data.candidate_prompt,
      patches: diff.data.patches,
      demos: diff.data.demos,
      acceptedPatchIds: diff.data.accepted_patch_ids,
    });
  }, [diff.data]);

  const patchKindById = useMemo(
    () =>
      new Map(
        (diff.data?.patches ?? []).map((patch) => [
          patch.patch_id,
          PATCH_KIND_LABELS[patch.kind] ?? patch.kind,
        ]),
      ),
    [diff.data],
  );

  const dropDemos = useMutation({
    mutationFn: (payload: { artifactId: number; body: Parameters<typeof decidePromptPatches>[1] }) =>
      decidePromptPatches(payload.artifactId, payload.body),
    onSuccess: (artifact) => {
      setDropped(new Set());
      onArtifactCreated(artifact);
      void queryClient.invalidateQueries({ queryKey: ["prompt-lab", "artifacts"] });
      void queryClient.invalidateQueries({ queryKey: ["prompt-lab", "artifact-diff"] });
    },
  });

  if (artifactId === null) {
    return (
      <div className="ag-plab-empty">
        <p>先在「编译运行」里选择一个产物，这里会展示它与基线的逐行对照。</p>
        <button type="button" onClick={onGoToCompile}>
          前往编译运行
        </button>
      </div>
    );
  }

  if (diff.isError && getErrorStatus(diff.error) === 404) {
    return (
      <div className="ag-plab-empty">
        <p>该产物不存在或已被清理。</p>
        <button type="button" onClick={onClearArtifact}>
          返回产物列表
        </button>
      </div>
    );
  }

  return (
    <PanelState
      pending={diff.isPending}
      error={diff.error}
      empty={!diff.data}
      emptyTitle="暂无差异数据"
      emptyDescription="该产物还没有可对照的 Prompt。"
      onRetry={() => void diff.refetch()}
      pendingLabel="正在计算差异…"
    >
      {diff.data && analysis && (
        <div className="ag-plab-diff-panel">
          <CostBar diff={diff.data} />

          {analysis.result.degraded && (
            <p className="ag-inline-feedback is-error" role="alert">
              差异规模超限，部分区段已按整段替换展示，行级精度有损失。
            </p>
          )}
          {!analysis.map.exact && (
            <p className="ag-compact-state">
              本次差异无法归属到具体补丁（服务端渲染规则可能已变更），仅展示逐行对照。
            </p>
          )}

          <section className="ag-plab-diff">
            <div className="ag-plab-diff__pane">
              <h3>基线 Prompt</h3>
              {analysis.hunks.map((hunk, index) => (
                <div key={index} className={`ag-plab-diff__hunk is-${hunk.op}`}>
                  <DiffSide lines={hunk.lines} side="baseline" />
                </div>
              ))}
            </div>
            <div className="ag-plab-diff__pane">
              <h3>候选 Prompt</h3>
              {analysis.hunks.map((hunk, index) => (
                <div key={index} className={`ag-plab-diff__hunk is-${hunk.op}`}>
                  <HunkAttribution hunk={hunk} patchKindById={patchKindById} />
                  <DiffSide lines={hunk.lines} side="candidate" />
                </div>
              ))}
            </div>
          </section>

          {diff.data.demos.length > 0 && (
            <section className="ag-governance-card">
              <header>
                <div>
                  <span className="ag-card-kicker">INLINED DEMOS</span>
                  <h2>内联示例</h2>
                  <p>
                    示例会随每次生产推理发给模型。剔除后会生成一个新的候选产物。
                  </p>
                </div>
              </header>
              <ul className="ag-plab-demo-list">
                {diff.data.demos.map((demo) => {
                  const isDropped = dropped.has(demo.demo_id);
                  return (
                    <li
                      key={demo.demo_id}
                      className={`ag-plab-demo-item${isDropped ? " is-dropped" : ""}`}
                    >
                      <div className="ag-plab-demo-item__head">
                        <span className="ag-card-kicker">
                          示例 {demo.demo_id.slice(0, 8)}
                        </span>
                        <StatusChip
                          status={demo.redaction_mode}
                          labels={PROMPT_LAB_STATUS_LABELS}
                        />
                        {isAdmin && (
                          <button
                            type="button"
                            className="is-secondary"
                            aria-label={`${isDropped ? "还原" : "剔除"}示例 ${demo.demo_id.slice(0, 8)}`}
                            onClick={() =>
                              setDropped((previous) => {
                                const next = new Set(previous);
                                if (next.has(demo.demo_id)) next.delete(demo.demo_id);
                                else next.add(demo.demo_id);
                                return next;
                              })
                            }
                          >
                            {isDropped ? "还原" : "剔除"}
                          </button>
                        )}
                      </div>
                      <blockquote>{demo.rendered_text}</blockquote>
                      <p className="ag-plab-demo-item__source">
                        来源 {demo.subject_type} #{demo.subject_id} ·{" "}
                        {demo.segment_ids.length} 个片段 · 指纹{" "}
                        {demo.source_checksum.slice(0, 12)}
                        {demo.reception_id !== null && (
                          <>
                            {" · "}
                            <Link to={`/receptions/${demo.reception_id}/workspace`}>
                              查看接待
                            </Link>
                          </>
                        )}
                      </p>
                    </li>
                  );
                })}
              </ul>
              {isAdmin && dropped.size > 0 && (
                <div className="ag-plab-staged-bar" role="status">
                  <span>已标记剔除 {dropped.size} 条示例</span>
                  <button type="button" className="is-secondary" onClick={() => setDropped(new Set())}>
                    全部还原
                  </button>
                  <button
                    type="button"
                    disabled={dropDemos.isPending}
                    onClick={() =>
                      dropDemos.mutate({
                        artifactId,
                        body: {
                          // 补丁按现状原样重放，只改示例。
                          decisions: replayDecisions(diff.data),
                          dropped_demo_ids: [...dropped],
                        },
                      })
                    }
                  >
                    {dropDemos.isPending ? "正在提交…" : "提交剔除"}
                  </button>
                </div>
              )}
              {dropDemos.isError && (
                <p className="ag-inline-feedback is-error" role="alert">
                  {getErrorMessage(dropDemos.error, "剔除示例失败，请稍后重试。")}
                </p>
              )}
            </section>
          )}
        </div>
      )}
    </PanelState>
  );
}

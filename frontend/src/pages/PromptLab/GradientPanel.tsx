import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { PanelState } from "@/components/PanelState";
import { GovernanceDialog } from "@/components/governance/GovernanceDialog";
import { StatusChip } from "@/components/governance/StatusChip";
import {
  compactCount,
  displayValue,
  failureStageLabel,
  signedPercent,
} from "@/components/governance/format";
import {
  decidePromptPatches,
  getPromptArtifactDiff,
  listPromptGradients,
} from "@/api/services";
import type {
  PromptArtifact,
  PromptGradient,
  PromptGradientDecision,
  PromptPatch,
} from "@/types/api";
import { getErrorMessage, getErrorStatus } from "@/utils/errors";

import { PATCH_KIND_LABELS, PROMPT_LAB_STATUS_LABELS } from "./labels";

/**
 * 已知的效果字段与其格式。
 *
 * 没有回放就没有指标。未知 key 走兜底展示，全空时明说「尚未回放」。
 * **绝不能因为字段缺失就显示 0 或编造数值**：那会让人以为补丁经过了验证。
 *
 * `replayed` 与 `low_confidence` 不在这张表里——它们是控制标志而非指标，
 * 渲染成一行「true」既没信息量，又会把真正的数字挤下去。
 */
const EVALUATION_FIELDS: Readonly<
  Record<string, { label: string; format: "count" | "signed-percent" }>
> = {
  source_badcase_count: { label: "关联错误样本", format: "count" },
  cluster_support: { label: "聚类样本量", format: "count" },
  gradient_rounds: { label: "梯度轮次", format: "count" },
  macro_f1_delta: { label: "Macro F1 变化", format: "signed-percent" },
  review_rate_delta: { label: "复核率变化", format: "signed-percent" },
  improved_count: { label: "改善样本", format: "count" },
  regressed_count: { label: "退化样本", format: "count" },
  support: { label: "样本量", format: "count" },
};

const DECISION_FILTERS: { id: PromptGradientDecision | "all"; label: string }[] = [
  { id: "all", label: "全部" },
  { id: "pending", label: "待决定" },
  { id: "accepted", label: "已接受" },
  { id: "rejected", label: "已拒绝" },
];

const CONTROL_FLAGS = new Set(["tag_key_deltas", "replayed", "low_confidence"]);

function EvaluationStage({ evaluation }: { evaluation: Record<string, unknown> }) {
  const entries = Object.entries(evaluation);
  const notReplayed = evaluation.replayed === false;
  const lowConfidence = evaluation.low_confidence === true;
  if (entries.length === 0) {
    return (
      <p className="ag-compact-state">
        尚未回放评估。接受该补丁后，可在「回放对比」查看它对整体指标的影响。
      </p>
    );
  }
  const sideEffects = evaluation.tag_key_deltas;
  return (
    <>
      {notReplayed && (
        <p className="ag-compact-state">
          尚未回放评估，下列只是编译期记录的证据量，不代表补丁的实际效果。
        </p>
      )}
      {lowConfidence && (
        <p className="ag-inline-feedback is-warning" role="note">
          该聚类样本不足，效果仅供参考。
        </p>
      )}
      <dl className="ag-plab-evaluation">
        {entries
          .filter(([key]) => !CONTROL_FLAGS.has(key))
          .map(([key, value]) => {
            const field = EVALUATION_FIELDS[key];
            const rendered =
              field && typeof value === "number"
                ? field.format === "count"
                  ? compactCount(value)
                  : signedPercent(value)
                : displayValue(value);
            return (
              <div key={key}>
                <dt>{field?.label ?? key}</dt>
                <dd>{rendered}</dd>
              </div>
            );
          })}
      </dl>
      {sideEffects !== undefined &&
        typeof sideEffects === "object" &&
        sideEffects !== null && (
          <div className="ag-plab-side-effects">
            {/* 局部修好、全局变差是提示词优化最常见的陷阱，这一段最值钱。 */}
            <span className="ag-card-kicker">对其他标签的影响</span>
            <dl>
              {Object.entries(sideEffects as Record<string, unknown>).map(
                ([tagKey, delta]) => (
                  <div key={tagKey}>
                    <dt>{tagKey}</dt>
                    <dd
                      className={
                        typeof delta === "number" && delta < 0 ? "is-worse" : "is-better"
                      }
                    >
                      {typeof delta === "number" ? signedPercent(delta) : displayValue(delta)}
                    </dd>
                  </div>
                ),
              )}
            </dl>
          </div>
        )}
    </>
  );
}

function GradientCard({
  gradient,
  patch,
  staged,
  onStage,
  onNoteChange,
  isAdmin,
}: {
  gradient: PromptGradient;
  patch: PromptPatch | undefined;
  staged: { decision: "accepted" | "rejected"; note: string } | undefined;
  onStage: (decision: "accepted" | "rejected" | null) => void;
  onNoteChange: (note: string) => void;
  isAdmin: boolean;
}) {
  const stagedClass = staged ? ` is-staged-${staged.decision}` : "";
  const support =
    typeof gradient.evaluation.support === "number"
      ? gradient.evaluation.support
      : typeof gradient.evaluation.source_badcase_count === "number"
        ? gradient.evaluation.source_badcase_count
        : null;

  return (
    <li className={`ag-plab-gradient-card${stagedClass}`}>
      <div className="ag-plab-gradient-card__head">
        <span className="ag-card-kicker">补丁 {gradient.patch_id.slice(0, 8)}</span>
        <StatusChip status={gradient.decision} labels={PROMPT_LAB_STATUS_LABELS} />
        {patch && <span className="ag-plab-kind">{PATCH_KIND_LABELS[patch.kind] ?? patch.kind}</span>}
      </div>

      <ol className="ag-plab-gradient-stages">
        <li className="ag-plab-gradient-card__stage">
          <h4>① 失败样本</h4>
          <dl>
            <div>
              <dt>标签</dt>
              <dd>{gradient.tag_key ?? "—"}</dd>
            </div>
            <div>
              <dt>失败阶段</dt>
              <dd>{failureStageLabel(gradient.failure_stage)}</dd>
            </div>
            <div>
              <dt>失败模式</dt>
              <dd>{gradient.failure_mode ?? "—"}</dd>
            </div>
          </dl>
        </li>
        <li className="ag-plab-gradient-card__stage">
          <h4>② 评价反馈</h4>
          <blockquote>{gradient.gradient_text || "—"}</blockquote>
          <small>第 {gradient.iteration} 轮</small>
        </li>
        <li className="ag-plab-gradient-card__stage">
          <h4>③ 修改建议</h4>
          <pre>{gradient.proposed_edit || "—"}</pre>
          {patch && patch.target_tag_keys.length > 0 && (
            <p className="ag-plab-target-tags">
              目标标签：{patch.target_tag_keys.join("、")}
            </p>
          )}
        </li>
        <li className="ag-plab-gradient-card__stage">
          <h4>④ 应用后效果</h4>
          {support !== null && support < 10 && (
            <p className="ag-compact-state">样本不足（{support} 例），效果仅供参考。</p>
          )}
          <EvaluationStage evaluation={gradient.evaluation} />
        </li>
      </ol>

      {isAdmin && (
        <div className="ag-plab-gradient-card__actions">
          <button
            type="button"
            aria-pressed={staged?.decision === "accepted"}
            aria-label={`接受补丁 ${gradient.patch_id.slice(0, 8)}`}
            onClick={() => onStage(staged?.decision === "accepted" ? null : "accepted")}
          >
            接受
          </button>
          <button
            type="button"
            className="is-secondary"
            aria-pressed={staged?.decision === "rejected"}
            aria-label={`拒绝补丁 ${gradient.patch_id.slice(0, 8)}`}
            onClick={() => onStage(staged?.decision === "rejected" ? null : "rejected")}
          >
            拒绝
          </button>
          {staged?.decision === "rejected" && (
            <label className="ag-plab-note">
              拒绝理由（可选，≤1000 字）
              <textarea
                rows={2}
                maxLength={1000}
                aria-label="拒绝理由"
                value={staged.note}
                onChange={(event) => onNoteChange(event.target.value)}
              />
            </label>
          )}
        </div>
      )}
    </li>
  );
}

export function GradientPanel({
  artifactId,
  isAdmin,
  onArtifactCreated,
  onGoToCompile,
}: {
  artifactId: number | null;
  isAdmin: boolean;
  onArtifactCreated: (artifact: PromptArtifact) => void;
  onGoToCompile: () => void;
}) {
  const queryClient = useQueryClient();
  const [decisionFilter, setDecisionFilter] = useState<PromptGradientDecision | "all">(
    "all",
  );
  const [staged, setStaged] = useState<
    Map<string, { decision: "accepted" | "rejected"; note: string }>
  >(new Map());
  const [confirming, setConfirming] = useState(false);

  const gradients = useQuery({
    queryKey: ["prompt-lab", "gradients", artifactId, decisionFilter],
    queryFn: () =>
      listPromptGradients(
        decisionFilter === "all"
          ? { artifact_id: artifactId as number }
          : { artifact_id: artifactId as number, decision: decisionFilter },
      ),
    enabled: artifactId !== null,
    retry: false,
  });

  // 与 DiffPanel 共用查询键：从那边切过来是零请求。补丁的 kind / target_tag_keys
  // 只在 diff 响应里，梯度记录本身没有。
  const diff = useQuery({
    queryKey: ["prompt-lab", "artifact-diff", artifactId],
    queryFn: () => getPromptArtifactDiff(artifactId as number),
    enabled: artifactId !== null,
    retry: false,
  });

  const patchById = useMemo(
    () => new Map((diff.data?.patches ?? []).map((patch) => [patch.patch_id, patch])),
    [diff.data],
  );

  const decide = useMutation({
    mutationFn: (payload: {
      artifactId: number;
      body: Parameters<typeof decidePromptPatches>[1];
    }) => decidePromptPatches(payload.artifactId, payload.body),
    onSuccess: (artifact) => {
      setStaged(new Map());
      setConfirming(false);
      onArtifactCreated(artifact);
      void queryClient.invalidateQueries({ queryKey: ["prompt-lab", "artifacts"] });
      void queryClient.invalidateQueries({ queryKey: ["prompt-lab", "artifact-diff"] });
      void queryClient.invalidateQueries({ queryKey: ["prompt-lab", "gradients"] });
    },
  });

  if (artifactId === null) {
    return (
      <div className="ag-plab-empty">
        <p>先在「编译运行」里选择一个产物，这里会展示它的每条修改建议。</p>
        <button type="button" onClick={onGoToCompile}>
          前往编译运行
        </button>
      </div>
    );
  }

  const items = gradients.data?.items ?? [];
  const acceptedCount = [...staged.values()].filter((s) => s.decision === "accepted").length;
  const rejectedCount = staged.size - acceptedCount;

  /**
   * 未暂存的补丁必须按当前采纳状态一并重放——服务端把 decisions 当作最终采纳集，
   * 漏掉的补丁会被移除。这一段与 DiffPanel 的 replayDecisions 是同一个约束。
   */
  const buildDecisions = () => {
    const currentlyAccepted = new Set(diff.data?.accepted_patch_ids ?? []);
    return (diff.data?.patches ?? []).map((patch) => {
      const stagedItem = staged.get(patch.patch_id);
      const decision =
        stagedItem?.decision ??
        (currentlyAccepted.has(patch.patch_id) ? "accepted" : "rejected");
      const note = stagedItem?.note.trim();
      return {
        patch_id: patch.patch_id,
        decision,
        ...(note ? { note } : {}),
      };
    });
  };

  const removedCount = buildDecisions().filter((d) => d.decision === "rejected").length;

  return (
    <div className="ag-plab-gradients">
      <div className="ag-panel-toolbar">
        <div>
          <h2>梯度与补丁</h2>
          <p>每条建议都记录了它从哪个失败样本来、依据什么诊断、产生了什么效果。</p>
        </div>
        <label className="ag-plab-inline-field">
          <span>决策</span>
          <select
            aria-label="按决策筛选梯度"
            value={decisionFilter}
            onChange={(event) =>
              setDecisionFilter(event.target.value as PromptGradientDecision | "all")
            }
          >
            {DECISION_FILTERS.map((option) => (
              <option key={option.id} value={option.id}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
      </div>

      {staged.size > 0 && (
        <div className="ag-plab-staged-bar" role="status" aria-live="polite">
          <span>
            已暂存 {staged.size} 项决策（接受 {acceptedCount} / 拒绝 {rejectedCount}）
          </span>
          <button type="button" className="is-secondary" onClick={() => setStaged(new Map())}>
            清空暂存
          </button>
          <button type="button" onClick={() => setConfirming(true)}>
            提交决策
          </button>
        </div>
      )}

      {decide.isError && (
        <p className="ag-inline-feedback is-error" role="alert">
          {getErrorStatus(decide.error) === 409
            ? "产物已被他人更新，请重新加载后再提交。"
            : getErrorMessage(decide.error, "提交决策失败，请稍后重试。")}
        </p>
      )}

      <PanelState
        pending={gradients.isPending}
        error={gradients.error}
        empty={items.length === 0}
        emptyTitle="该产物没有梯度记录"
        emptyDescription="内置编译器只在存在错误簇时才产出建议。"
        onRetry={() => void gradients.refetch()}
      >
        <ul className="ag-plab-gradient-list">
          {items.map((gradient) => (
            <GradientCard
              key={gradient.id}
              gradient={gradient}
              patch={patchById.get(gradient.patch_id)}
              staged={staged.get(gradient.patch_id)}
              isAdmin={isAdmin}
              onStage={(decision) =>
                setStaged((previous) => {
                  const next = new Map(previous);
                  if (decision === null) next.delete(gradient.patch_id);
                  else
                    next.set(gradient.patch_id, {
                      decision,
                      note: previous.get(gradient.patch_id)?.note ?? "",
                    });
                  return next;
                })
              }
              onNoteChange={(note) =>
                setStaged((previous) => {
                  const next = new Map(previous);
                  const current = next.get(gradient.patch_id);
                  if (current) next.set(gradient.patch_id, { ...current, note });
                  return next;
                })
              }
            />
          ))}
        </ul>
      </PanelState>

      {confirming && (
        <GovernanceDialog
          id="prompt-decision-dialog-title"
          kicker="PATCH REVIEW"
          title="提交补丁决策"
          pending={decide.isPending}
          danger
          onClose={() => setConfirming(false)}
          onSubmit={() =>
            decide.mutate({
              artifactId,
              body: { decisions: buildDecisions(), dropped_demo_ids: [] },
            })
          }
          submitLabel="确认提交"
          pendingLabel="正在提交…"
          error={
            decide.isError
              ? getErrorMessage(decide.error, "提交决策失败。")
              : null
          }
        >
          <div className="is-full ag-confirm-dialog__copy">
            <p>
              提交后会生成一个新的候选产物。其中 <strong>{removedCount}</strong> 条补丁
              将不出现在新的 Prompt 里，当前产物会被标记为已被取代。
            </p>
            <p className="ag-compact-state">
              未逐条决策的补丁会按当前采纳状态原样保留。
            </p>
          </div>
        </GovernanceDialog>
      )}
    </div>
  );
}

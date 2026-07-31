import { useMutation } from "@tanstack/react-query";
import {
  IconBulb,
  IconCheckCircle,
  IconClose,
  IconRefresh,
  IconSwap,
} from "@arco-design/web-react/icon";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  createTagJob,
  createTagReviewBatch,
} from "@/api/services";
import type {
  AnalyzeTagInsightsResponse,
  CreateTagReviewSubject,
  ReceptionTagInsightsRequest,
  ReceptionTagInsightsResponse,
  TagJobScope,
} from "@/types/api";
import "./GovernanceActions.css";

interface GovernanceActionsProps {
  result: AnalyzeTagInsightsResponse;
  persisted?: ReceptionTagInsightsResponse;
  request: ReceptionTagInsightsRequest;
  onCompareVersions: (groupIds: string[]) => void;
}

type DialogKind = "compare" | null;

function hashScope(value: string): string {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(36);
}

function parseSubject(
  targetId: string,
  tagKey: string,
): CreateTagReviewSubject | null {
  const receptionMatch = targetId.match(/reception:(\d+)/i);
  const receptionId = receptionMatch ? Number(receptionMatch[1]) : undefined;
  const unitMatch = targetId.match(/(?:unit|dialogue[_-]?unit):(\d+)/i);
  if (unitMatch) {
    return {
      subject_type: "dialogue_unit",
      subject_id: Number(unitMatch[1]),
      ...(receptionId ? { reception_id: receptionId } : {}),
      tag_key: tagKey,
    };
  }
  if (receptionMatch) {
    return {
      subject_type: "reception",
      subject_id: Number(receptionMatch[1]),
      tag_key: tagKey,
    };
  }
  return null;
}

export function GovernanceActions({
  result,
  persisted,
  request,
  onCompareVersions,
}: GovernanceActionsProps) {
  const [dialog, setDialog] = useState<DialogKind>(null);
  const [feedback, setFeedback] = useState<{
    message: string;
    link?: { to: string; label: string };
  } | null>(null);

  const reviewSubjects = useMemo(() => {
    const unique = new Map<string, CreateTagReviewSubject>();
    result.matrix
      .filter((row) => row.conflict || row.missing_group_keys.length > 0)
      .slice(0, 1_000)
      .forEach((row) => {
        const subject = parseSubject(row.target_id, row.label_key);
        if (subject) {
          const candidate = row.cells
            .flatMap((cell) => cell.assignments)
            .sort(
              (left, right) =>
                (right.confidence ?? -1) - (left.confidence ?? -1),
            )[0];
          const enrichedSubject: CreateTagReviewSubject = {
            ...subject,
            ...(candidate
              ? {
                  proposed_value: candidate.value,
                  confidence: candidate.confidence ?? undefined,
                  evidence_refs: candidate.evidence_refs.map((evidence) => ({
                    recording_id: evidence.recording_id,
                    start_sec:
                      evidence.start_ms === null ||
                      evidence.start_ms === undefined
                        ? undefined
                        : evidence.start_ms / 1_000,
                    end_sec:
                      evidence.end_ms === null ||
                      evidence.end_ms === undefined
                        ? undefined
                        : evidence.end_ms / 1_000,
                    text_excerpt: evidence.text_excerpt ?? undefined,
                    ref_id: evidence.ref_id,
                    kind: evidence.kind,
                  })),
                }
              : {}),
          };
          unique.set(
            `${subject.subject_type}:${subject.subject_id}:${subject.tag_key}`,
            enrichedSubject,
          );
        }
      });
    return [...unique.values()];
  }, [result.matrix]);

  const groupIds =
    persisted?.selected_group_ids.length
      ? persisted.selected_group_ids
      : result.groups
          .map((group) => group.group_id ?? `${group.group_key}@${group.version}`)
          .filter(Boolean);

  const rerunReceptionIds = useMemo(
    () =>
      request.reception_id?.length
        ? request.reception_id
        : persisted?.returned_reception_ids.length
          ? persisted.returned_reception_ids
          : [],
    [persisted?.returned_reception_ids, request.reception_id],
  );
  const canRerun = rerunReceptionIds.length > 0;
  const runScope = useMemo<TagJobScope>(
    () => ({
      ...(request.store_id?.length ? { store_ids: request.store_id } : {}),
      ...(rerunReceptionIds.length
        ? { reception_ids: rerunReceptionIds }
        : {}),
      ...(groupIds.length ? { group_ids: groupIds } : {}),
      ...(request.scenario?.length ? { scenarios: request.scenario } : {}),
      ...(request.started_from ? { started_from: request.started_from } : {}),
      ...(request.started_to ? { started_to: request.started_to } : {}),
    }),
    [
      groupIds,
      rerunReceptionIds,
      request.scenario,
      request.started_from,
      request.started_to,
      request.store_id,
    ],
  );
  const optimizationCohort = useMemo(
    () => ({
      source: "tag_insights" as const,
      filters: {
        ...(request.store_id?.length
          ? { store_ids: request.store_id }
          : {}),
        ...(request.agent_name?.length
          ? { agent_names: request.agent_name }
          : {}),
        ...(rerunReceptionIds.length
          ? { reception_ids: rerunReceptionIds }
          : {}),
        ...(request.scenario?.length
          ? { scenarios: request.scenario }
          : {}),
        ...(request.group_key?.length
          ? { group_keys: request.group_key }
          : {}),
        ...(request.started_from ? { started_from: request.started_from } : {}),
        ...(request.started_to ? { started_to: request.started_to } : {}),
      },
      ...(groupIds.length ? { group_ids: groupIds } : {}),
      conflict_only: result.overview.conflict_cells > 0,
    }),
    [
      groupIds,
      rerunReceptionIds,
      request.agent_name,
      request.group_key,
      request.scenario,
      request.started_from,
      request.started_to,
      request.store_id,
      result.overview.conflict_cells,
    ],
  );
  const optimizationHref = useMemo(() => {
    const params = new URLSearchParams({
      tab: "evolution",
      mode: "optimize",
      cohort: JSON.stringify(optimizationCohort),
    });
    return `/tag-governance?${params.toString()}`;
  }, [optimizationCohort]);

  const reviewMutation = useMutation({
    mutationFn: () =>
      createTagReviewBatch({
        reason: result.overview.conflict_cells > 0 ? "conflict" : "missing",
        subjects: reviewSubjects,
        review_bundle_id: `insight-${hashScope(
          JSON.stringify(optimizationCohort),
        )}`,
      }),
    onSuccess: (batch) =>
      setFeedback({
        message: `已创建 ${batch.created_count} 个复核任务`,
        link: { to: "/tag-review", label: "进入复核工作台" },
      }),
  });
  const runMutation = useMutation({
    mutationFn: () => {
      const serializedScope = JSON.stringify(runScope);
      return createTagJob(
        { job_type: "recompute", scope: runScope },
        `insight-rerun-${hashScope(serializedScope)}`,
      );
    },
    onSuccess: (job) =>
      setFeedback({
        message: `范围重跑 #${job.id} 已进入队列`,
        link: { to: `/tag-runs/${job.id}`, label: `查看运行 #${job.id}` },
      }),
  });
  const activeError = reviewMutation.error ?? runMutation.error;

  return (
    <section className="ag-insight-action-bridge" aria-labelledby="insight-action-title">
      <header>
        <div>
          <span className="ag-eyebrow">INSIGHT → ACTION</span>
          <h2 id="insight-action-title">把洞察送回标签闭环</h2>
          <p>
            当前筛选范围、冲突单元和版本选择会作为动作输入保留，避免人工二次抄录。
          </p>
        </div>
        <span>
          {reviewSubjects.length} 个可复核单元 · {groupIds.length} 个版本
        </span>
      </header>
      <div className="ag-insight-action-bridge__actions">
        <button
          type="button"
          aria-label="创建复核批次"
          disabled={reviewSubjects.length === 0 || reviewMutation.isPending}
          onClick={() => {
            setFeedback(null);
            reviewMutation.mutate();
          }}
        >
          <span className="ag-insight-action-icon" aria-hidden="true">
            <IconCheckCircle />
          </span>
          <span>
            <strong>创建复核批次</strong>
            <small>将冲突、缺失送入人工队列</small>
          </span>
        </button>
        <Link
          to={optimizationHref}
          aria-label="创建候选"
          onClick={() => {
            setFeedback(null);
          }}
        >
          <span className="ag-insight-action-icon" aria-hidden="true">
            <IconBulb />
          </span>
          <span>
            <strong>创建候选</strong>
            <small>携金标与生产版本进入自动优化</small>
          </span>
        </Link>
        <button
          type="button"
          aria-label="范围重跑"
          disabled={!canRerun || runMutation.isPending}
          onClick={() => {
            setFeedback(null);
            runMutation.mutate();
          }}
        >
          <span className="ag-insight-action-icon" aria-hidden="true">
            <IconRefresh />
          </span>
          <span>
            <strong>范围重跑</strong>
            <small>按当前筛选创建幂等任务</small>
          </span>
        </button>
        <button
          type="button"
          aria-label="版本对比"
          disabled={groupIds.length < 2}
          onClick={() => setDialog("compare")}
        >
          <span className="ag-insight-action-icon" aria-hidden="true">
            <IconSwap />
          </span>
          <span>
            <strong>版本对比</strong>
            <small>保留范围并精确选择 key@version</small>
          </span>
        </button>
      </div>
      {!canRerun && (
        <p className="ag-insight-action-note">
          范围重跑需要明确的接待 ID，请先缩小洞察筛选范围。
        </p>
      )}
      {feedback && (
        <p className="ag-insight-action-feedback is-success" role="status">
          <span>{feedback.message}</span>
          {feedback.link && (
            <Link to={feedback.link.to}>{feedback.link.label}</Link>
          )}
        </p>
      )}
      {activeError && (
        <p className="ag-insight-action-feedback is-error" role="alert">
          {activeError instanceof Error ? activeError.message : "动作执行失败"}
        </p>
      )}

      {dialog === "compare" && (
        <div className="ag-insight-dialog-backdrop">
          <section
            className="ag-insight-action-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="insight-compare-title"
            onKeyDown={(event) => {
              if (event.key === "Escape") setDialog(null);
            }}
          >
            <header>
              <div>
                <span className="ag-eyebrow">EXACT VERSION SCOPE</span>
                <h2 id="insight-compare-title">版本对比范围</h2>
              </div>
              <button
                type="button"
                aria-label="关闭"
                onClick={() => setDialog(null)}
              >
                <IconClose />
              </button>
            </header>
            <div className="ag-insight-compare-dialog">
              <p>将使用以下精确版本身份，门店、接待和时间筛选保持不变。</p>
              <ul>
                {groupIds.map((groupId) => (
                  <li key={groupId}>{groupId}</li>
                ))}
              </ul>
              <footer>
                <button
                  type="button"
                  className="is-secondary"
                  onClick={() => setDialog(null)}
                >
                  取消
                </button>
                <button
                  type="button"
                  onClick={() => {
                    onCompareVersions(groupIds);
                    setDialog(null);
                  }}
                >
                  使用此范围对比
                </button>
              </footer>
            </div>
          </section>
        </div>
      )}
    </section>
  );
}

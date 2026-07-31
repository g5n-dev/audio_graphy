import { type FormEvent, useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Button, Empty, Spin, Tag } from "@arco-design/web-react";
import { Link, useNavigate } from "react-router-dom";
import {
  acceptReceptionProposal,
  discoverReceptionProposals,
  listReceptions,
} from "@/api/services";
import type {
  ReceptionAutomaticProposal,
  ReceptionDiscoveryRequest,
  ReceptionDiscoveryResponse,
  ReceptionProposalAcceptRequest,
  ReceptionProposalAcceptResponse,
  ReceptionResponseApi,
  ReceptionScenario,
  ReceptionSplitAcceptanceResponse,
  ReceptionStatus,
} from "@/types/api";

const PAGE_SIZE = 20;
const DISCOVERY_LIMIT = 200;

const RECEPTION_STATUS_OPTIONS: Array<{
  value: "" | ReceptionStatus;
  label: string;
}> = [
  { value: "", label: "全部状态" },
  { value: "proposed", label: "待确认" },
  { value: "needs_review", label: "待复核" },
  { value: "confirmed", label: "已确认" },
  { value: "processing", label: "处理中" },
  { value: "ready", label: "可调听" },
  { value: "split", label: "已拆分" },
  { value: "archived", label: "已归档" },
];

const STATUS_LABELS: Record<ReceptionStatus, string> = {
  proposed: "待确认",
  needs_review: "待复核",
  confirmed: "已确认",
  processing: "处理中",
  ready: "可调听",
  split: "已拆分",
  archived: "已归档",
};

const SCENARIO_LABELS: Record<ReceptionScenario, string> = {
  gold: "金店销售",
  automotive: "汽车销售",
  custom: "自定义场景",
};

const DECISION_LABELS = {
  merge: "建议合并",
  reject: "不建议合并",
  needs_review: "需要复核",
} as const;

function toLocalInputValue(date: Date): string {
  const offsetDate = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return offsetDate.toISOString().slice(0, 16);
}

function toIsoDate(value: string): string | null {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}

function formatDate(value: string | null): string {
  if (!value) return "时间待补全";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function formatDuration(startedAt: string, endedAt: string): string {
  const durationSeconds = Math.max(
    0,
    Math.round((Date.parse(endedAt) - Date.parse(startedAt)) / 1_000),
  );
  const hours = Math.floor(durationSeconds / 3_600);
  const minutes = Math.floor((durationSeconds % 3_600) / 60);
  const seconds = durationSeconds % 60;
  return [
    hours > 0 ? `${hours}小时` : "",
    minutes > 0 ? `${minutes}分` : "",
    `${seconds}秒`,
  ]
    .filter(Boolean)
    .join("");
}

function errorMessage(error: unknown): string {
  if (error instanceof Error && error.message.trim()) return error.message;
  if (typeof error === "string" && error.trim()) return error;
  if (typeof error === "object" && error !== null) {
    const response = Reflect.get(error, "response");
    if (typeof response === "object" && response !== null) {
      const data = Reflect.get(response, "data");
      if (typeof data === "object" && data !== null) {
        const envelope = Reflect.get(data, "error");
        if (typeof envelope === "object" && envelope !== null) {
          const message = Reflect.get(envelope, "message");
          if (typeof message === "string" && message.trim()) return message;
        }
        const detail = Reflect.get(data, "detail");
        if (typeof detail === "string" && detail.trim()) return detail;
      }
    }
  }
  return "服务暂时不可用，请稍后重试。";
}

function proposalKey(proposal: ReceptionAutomaticProposal): string {
  return [
    proposal.candidate_type,
    proposal.recording_ids.join("-"),
    proposal.split_at_sec ?? "none",
  ].join(":");
}

function isSplitAcceptance(
  accepted: ReceptionProposalAcceptResponse,
): accepted is ReceptionSplitAcceptanceResponse {
  return (
    "candidate_type" in accepted &&
    accepted.candidate_type === "recording_split"
  );
}

function proposalTitle(proposal: ReceptionAutomaticProposal): string {
  if (proposal.candidate_type === "duration_review") return "时长待补全";
  if (proposal.candidate_type === "recording_split") return "长录音切分建议";
  return proposal.recording_ids.length === 1
    ? "单录音接待"
    : "短录音接待组合";
}

function confidenceTone(confidence: number): string {
  if (confidence >= 0.8) return "high";
  if (confidence >= 0.55) return "medium";
  return "low";
}

interface ProposalCardProps {
  proposal: ReceptionAutomaticProposal;
  isAccepting: boolean;
  acceptancePending: boolean;
  onAccept: (proposal: ReceptionAutomaticProposal) => void;
}

function ProposalCard({
  proposal,
  isAccepting,
  acceptancePending,
  onAccept,
}: ProposalCardProps) {
  const canAcceptMerge =
    proposal.candidate_type === "merge_group" &&
    proposal.decision !== "reject" &&
    proposal.duration_status === "available";
  const canAcceptSplit =
    proposal.candidate_type === "recording_split" &&
    proposal.decision === "needs_review" &&
    proposal.duration_status === "available" &&
    proposal.split_at_sec !== null &&
    proposal.at_segment_id !== null &&
    proposal.proposal_token !== null;
  const canAccept = canAcceptMerge || canAcceptSplit;
  const confidencePercent = Math.round(proposal.confidence * 100);

  return (
    <article
      className={`ag-proposal-card ag-proposal-card--${proposal.candidate_type}`}
    >
      <header className="ag-proposal-card__header">
        <div>
          <span className="ag-proposal-card__kind">
            {proposalTitle(proposal)}
          </span>
          <h3>
            录音 {proposal.recording_ids.map((id) => `#${id}`).join(" + ")}
          </h3>
        </div>
        <Tag
          color={
            proposal.decision === "merge"
              ? "green"
              : proposal.decision === "needs_review"
                ? "orange"
                : "red"
          }
        >
          {DECISION_LABELS[proposal.decision]}
        </Tag>
      </header>

      <div className="ag-proposal-card__facts">
        <span>{proposal.store_id}</span>
        <span>
          {formatDate(proposal.started_at)} — {formatDate(proposal.ended_at)}
        </span>
        {proposal.split_at_sec !== null && (
          <span>
            建议边界 {proposal.split_at_sec.toFixed(1)} 秒
            {proposal.at_segment_id
              ? ` · 分段 #${proposal.at_segment_id}`
              : ""}
          </span>
        )}
      </div>

      <div className="ag-confidence">
        <div>
          <span>置信度 {confidencePercent}%</span>
          <small>
            {proposal.duration_status === "available"
              ? "时长可用"
              : "时长不可用"}
          </small>
        </div>
        <div
          className="ag-confidence__track"
          role="progressbar"
          aria-label={`${proposalTitle(proposal)}置信度`}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={confidencePercent}
        >
          <span
            className={`ag-confidence__fill is-${confidenceTone(proposal.confidence)}`}
            style={{ width: `${confidencePercent}%` }}
          />
        </div>
      </div>

      <ul className="ag-proposal-reasons" aria-label="候选判断依据">
        {proposal.reasons.map((reason, index) => (
          <li key={`${reason.code}-${index}`}>
            <span>
              <code>{reason.code}</code>
              {reason.hard_constraint && <em>硬约束</em>}
            </span>
            <p>{reason.detail}</p>
            <small>
              贡献值 {reason.contribution > 0 ? "+" : ""}
              {reason.contribution.toFixed(2)}
            </small>
          </li>
        ))}
      </ul>

      <footer className="ag-proposal-card__footer">
        {canAccept ? (
          <>
            <p>
              {canAcceptSplit
                ? "执行时会重新校验签名快照与边界，源录音保持不变，并原子创建前后两个接待。"
                : "接受后由服务端再次校验，并创建可溯源的完整接待时间轴。"}
            </p>
            <Button
              type="primary"
              size="small"
              loading={isAccepting}
              disabled={acceptancePending}
              onClick={() => onAccept(proposal)}
            >
              {canAcceptSplit
                ? "执行切分并自动分析"
                : proposal.recording_ids.length === 1
                  ? "创建单录音接待"
                  : "接受并创建接待"}
            </Button>
          </>
        ) : proposal.candidate_type === "recording_split" ? (
          <p className="ag-review-note">
            切分快照已失效或证据不足，请重新扫描后再执行；源录音不会被改写。
          </p>
        ) : proposal.candidate_type === "duration_review" ? (
          <p className="ag-review-note">
            先完成索引时长后再处理，当前候选不可创建接待。
          </p>
        ) : (
          <p className="ag-review-note">
            服务端判定为拒绝，当前组合不可创建接待。
          </p>
        )}
      </footer>
    </article>
  );
}

export default function ReceptionEntryPage() {
  const navigate = useNavigate();
  const initialEnd = useMemo(() => new Date(), []);
  const initialStart = useMemo(
    () => new Date(initialEnd.getTime() - 24 * 60 * 60 * 1_000),
    [initialEnd],
  );

  const [receptionId, setReceptionId] = useState("");
  const [directError, setDirectError] = useState<string | null>(null);

  const [page, setPage] = useState(1);
  const [queueStoreDraft, setQueueStoreDraft] = useState("");
  const [queueStatusDraft, setQueueStatusDraft] = useState<
    "" | ReceptionStatus
  >("");
  const [queueStore, setQueueStore] = useState("");
  const [queueStatus, setQueueStatus] = useState<"" | ReceptionStatus>("");

  const [scenario, setScenario] =
    useState<ReceptionScenario>("automotive");
  const [candidateStore, setCandidateStore] = useState("");
  const [recordedFrom, setRecordedFrom] = useState(
    toLocalInputValue(initialStart),
  );
  const [recordedTo, setRecordedTo] = useState(
    toLocalInputValue(initialEnd),
  );
  const [shortRecordingMaxSec, setShortRecordingMaxSec] = useState(300);
  const [discoveryValidation, setDiscoveryValidation] = useState<string | null>(
    null,
  );
  const [discoverySnapshot, setDiscoverySnapshot] = useState<{
    scenario: ReceptionScenario;
    result: ReceptionDiscoveryResponse;
  } | null>(null);
  const [acceptingKey, setAcceptingKey] = useState<string | null>(null);

  const queueQuery = useQuery({
    queryKey: ["receptions", page, PAGE_SIZE, queueStore, queueStatus],
    queryFn: () =>
      listReceptions({
        page,
        page_size: PAGE_SIZE,
        store_id: queueStore || undefined,
        status: queueStatus || undefined,
      }),
  });

  const discoveryMutation = useMutation({
    mutationFn: (request: ReceptionDiscoveryRequest) =>
      discoverReceptionProposals(request),
    onSuccess: (result, request) => {
      setDiscoverySnapshot({ scenario: request.scenario, result });
    },
  });

  const acceptMutation = useMutation({
    mutationFn: async (request: ReceptionProposalAcceptRequest) => {
      const accepted = await acceptReceptionProposal(request);
      const splitAcceptance = isSplitAcceptance(accepted);
      const receptions: ReceptionResponseApi[] = splitAcceptance
        ? [...accepted.receptions]
        : [accepted];
      return splitAcceptance
        ? {
            kind: "split" as const,
            accepted,
            receptions,
          }
        : {
            kind: "merge" as const,
            accepted,
            receptions,
          };
    },
    onSuccess: (result) => {
      const { receptions } = result;
      const automationMessage =
        result.kind === "split"
          ? `长录音已在 ${result.accepted.split_at_sec.toFixed(1)} 秒处原子拆为接待 #${receptions[0]?.id}、#${receptions[1]?.id}；后台自动化与标签抽取任务已事务入队。`
          : "接待已创建；后台自动化与标签抽取任务已事务入队，工作台将持续刷新进度。";
      const primaryReception = receptions[0];
      if (!primaryReception) return;
      navigate(`/receptions/${primaryReception.id}/workspace`, {
        state: { automationMessage },
      });
    },
    onSettled: () => setAcceptingKey(null),
  });

  const openWorkspace = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const normalizedId = receptionId.trim();
    if (!/^[1-9]\d*$/.test(normalizedId)) {
      setDirectError("请输入有效的正整数接待 ID。");
      return;
    }
    navigate(`/receptions/${normalizedId}/workspace`);
  };

  const applyQueueFilters = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setPage(1);
    setQueueStore(queueStoreDraft.trim());
    setQueueStatus(queueStatusDraft);
  };

  const scanCandidates = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const normalizedStore = candidateStore.trim();
    const fromIso = toIsoDate(recordedFrom);
    const toIso = toIsoDate(recordedTo);
    if (!normalizedStore) {
      setDiscoveryValidation("请输入需要扫描的候选门店。");
      return;
    }
    if (!fromIso || !toIso || Date.parse(toIso) <= Date.parse(fromIso)) {
      setDiscoveryValidation("结束时间必须晚于开始时间。");
      return;
    }
    if (
      Date.parse(toIso) - Date.parse(fromIso) >
      31 * 24 * 60 * 60 * 1_000
    ) {
      setDiscoveryValidation("单次扫描时间窗不能超过 31 天。");
      return;
    }

    const request: ReceptionDiscoveryRequest = {
      scenario,
      store_id: normalizedStore,
      recorded_from: fromIso,
      recorded_to: toIso,
      short_recording_max_sec: shortRecordingMaxSec,
      limit: DISCOVERY_LIMIT,
    };
    setDiscoveryValidation(null);
    setDiscoverySnapshot(null);
    acceptMutation.reset();
    discoveryMutation.reset();
    discoveryMutation.mutate(request);
  };

  const acceptProposal = (proposal: ReceptionAutomaticProposal) => {
    if (!discoverySnapshot) return;
    const request: ReceptionProposalAcceptRequest = {
      scenario: discoverySnapshot.scenario,
      recording_ids: proposal.recording_ids,
      merge_mode: "logical",
      ...(proposal.candidate_type === "recording_split" &&
      proposal.split_at_sec !== null &&
      proposal.at_segment_id !== null &&
      proposal.proposal_token !== null
        ? {
            candidate_type: "recording_split" as const,
            split_at_sec: proposal.split_at_sec,
            at_segment_id: proposal.at_segment_id,
            proposal_token: proposal.proposal_token,
          }
        : {}),
    };
    setAcceptingKey(proposalKey(proposal));
    acceptMutation.reset();
    acceptMutation.mutate(request);
  };

  const total = queueQuery.data?.total ?? 0;
  const lastPage = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const proposalItems = discoverySnapshot?.result.items ?? [];

  return (
    <div className="ag-reception-entry-page">
      <header className="ag-feature-header ag-reception-hub__header">
        <div>
          <span className="ag-eyebrow">Reception Intelligence · 自动闭环</span>
          <h1>接待自动化工作队列</h1>
          <p>
            从真实录音扫描候选、复核合并依据，再进入对话切分与图谱调听工作台。
          </p>
        </div>
        <div className="ag-feature-header__actions">
          <Link to="/reception-flow">查看状态路径</Link>
          <Link to="/tag-insights">进入标签洞察</Link>
        </div>
      </header>

      <main className="ag-reception-hub">
        <section
          className="ag-reception-panel ag-reception-panel--queue"
          aria-labelledby="reception-queue-title"
        >
          <header className="ag-reception-panel__header">
            <div>
              <span className="ag-panel-kicker">WORK QUEUE</span>
              <h2 id="reception-queue-title">接待工作队列</h2>
              <p>按门店和处理状态查看已落库接待，支持分页回溯。</p>
            </div>
            <strong>共 {total} 个接待</strong>
          </header>

          <form className="ag-queue-filters" onSubmit={applyQueueFilters}>
            <label>
              <span>门店筛选</span>
              <input
                aria-label="门店筛选"
                value={queueStoreDraft}
                placeholder="例如 store-001"
                onChange={(event) => setQueueStoreDraft(event.target.value)}
              />
            </label>
            <label>
              <span>状态筛选</span>
              <select
                aria-label="状态筛选"
                value={queueStatusDraft}
                onChange={(event) =>
                  setQueueStatusDraft(
                    event.target.value as "" | ReceptionStatus,
                  )
                }
              >
                {RECEPTION_STATUS_OPTIONS.map((option) => (
                  <option key={option.value || "all"} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
            <Button htmlType="submit" type="primary">
              查询工作队列
            </Button>
          </form>

          <div className="ag-queue-state" aria-live="polite">
            {queueQuery.isPending ? (
              <div className="ag-inline-loading" role="status">
                <Spin />
                <span>正在加载接待队列…</span>
              </div>
            ) : queueQuery.isError ? (
              <div className="ag-inline-error" role="alert">
                <strong>工作队列加载失败</strong>
                <p>{errorMessage(queueQuery.error)}</p>
                <Button
                  size="small"
                  onClick={() => queueQuery.refetch()}
                >
                  重新加载工作队列
                </Button>
              </div>
            ) : queueQuery.data.items.length === 0 ? (
              <Empty description="当前筛选条件下暂无真实接待" />
            ) : (
              <div className="ag-reception-table-wrap">
                <table className="ag-reception-table">
                  <thead>
                    <tr>
                      <th>接待</th>
                      <th>门店 / 销售</th>
                      <th>时间 / 时长</th>
                      <th>状态</th>
                      <th>
                        <span className="ag-visually-hidden">操作</span>
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {queueQuery.data.items.map((reception) => (
                      <tr key={reception.id}>
                        <td>
                          <strong>#{reception.id}</strong>
                          <small>{SCENARIO_LABELS[reception.scenario]}</small>
                        </td>
                        <td>
                          <strong>{reception.store_id}</strong>
                          <small>{reception.agent_name ?? "未关联销售"}</small>
                        </td>
                        <td>
                          <strong>{formatDate(reception.started_at)}</strong>
                          <small>
                            {formatDuration(
                              reception.started_at,
                              reception.ended_at,
                            )}
                          </small>
                        </td>
                        <td>
                          <span
                            className={`ag-status ag-status--${reception.status}`}
                          >
                            {STATUS_LABELS[reception.status]}
                          </span>
                          <small>
                            {reception.merge_confidence === null
                              ? "置信度待补全"
                              : `合并 ${Math.round(reception.merge_confidence * 100)}%`}
                          </small>
                        </td>
                        <td>
                          <Button
                            size="small"
                            type="text"
                            onClick={() =>
                              navigate(
                                `/receptions/${reception.id}/workspace`,
                              )
                            }
                          >
                            打开工作台
                          </Button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          <nav className="ag-queue-pagination" aria-label="接待工作队列分页">
            <Button
              size="small"
              disabled={page <= 1 || queueQuery.isFetching}
              onClick={() => setPage((current) => Math.max(1, current - 1))}
            >
              上一页
            </Button>
            <span>
              第 {page} / {lastPage} 页
            </span>
            <Button
              size="small"
              disabled={page >= lastPage || queueQuery.isFetching}
              onClick={() =>
                setPage((current) => Math.min(lastPage, current + 1))
              }
            >
              下一页
            </Button>
          </nav>
        </section>

        <aside className="ag-reception-hub__aside">
          <section
            className="ag-reception-panel ag-reception-panel--direct"
            aria-labelledby="reception-entry-title"
          >
            <header className="ag-reception-panel__header">
              <div>
                <span className="ag-panel-kicker">DIRECT ACCESS</span>
                <h2 id="reception-entry-title">接待 ID 直达</h2>
              </div>
            </header>
            <form className="ag-direct-form" onSubmit={openWorkspace}>
              <label htmlFor="reception-id">接待 ID</label>
              <div>
                <input
                  id="reception-id"
                  inputMode="numeric"
                  autoComplete="off"
                  value={receptionId}
                  placeholder="例如 1024"
                  onChange={(event) => {
                    setReceptionId(event.target.value);
                    setDirectError(null);
                  }}
                />
                <Button htmlType="submit" type="primary">
                  打开调听工作台
                </Button>
              </div>
            </form>
            {directError && (
              <p className="ag-form-error" role="alert">
                {directError}
              </p>
            )}
          </section>

          <section className="ag-automation-note" aria-label="自动化边界">
            <span>自动化原则</span>
            <strong>建议可解释，写入需复核</strong>
            <p>
              候选扫描不改写数据；接受组合时服务端会重新校验占用、门店、时间与时长。
            </p>
          </section>
        </aside>

        <section
          className="ag-reception-panel ag-reception-panel--discovery"
          aria-labelledby="reception-discovery-title"
        >
          <header className="ag-reception-panel__header">
            <div>
              <span className="ag-panel-kicker">CANDIDATE DISCOVERY</span>
              <h2 id="reception-discovery-title">接待候选扫描</h2>
              <p>
                在单门店、最长 31 天的窗口内发现短录音组合和长录音边界。
              </p>
            </div>
            {discoverySnapshot && (
              <strong>
                已扫描 {discoverySnapshot.result.scanned_recordings} 段录音
              </strong>
            )}
          </header>

          <form className="ag-discovery-form" onSubmit={scanCandidates}>
            <label>
              <span>业务场景</span>
              <select
                aria-label="业务场景"
                value={scenario}
                onChange={(event) =>
                  setScenario(event.target.value as ReceptionScenario)
                }
              >
                <option value="automotive">汽车销售</option>
                <option value="gold">金店销售</option>
                <option value="custom">自定义场景</option>
              </select>
            </label>
            <label>
              <span>候选门店</span>
              <input
                aria-label="候选门店"
                value={candidateStore}
                placeholder="必填，例如 store-001"
                onChange={(event) => {
                  setCandidateStore(event.target.value);
                  setDiscoveryValidation(null);
                }}
              />
            </label>
            <label>
              <span>开始时间</span>
              <input
                aria-label="开始时间"
                type="datetime-local"
                value={recordedFrom}
                onChange={(event) => {
                  setRecordedFrom(event.target.value);
                  setDiscoveryValidation(null);
                }}
              />
            </label>
            <label>
              <span>结束时间</span>
              <input
                aria-label="结束时间"
                type="datetime-local"
                value={recordedTo}
                onChange={(event) => {
                  setRecordedTo(event.target.value);
                  setDiscoveryValidation(null);
                }}
              />
            </label>
            <label>
              <span>短录音上限（秒）</span>
              <input
                aria-label="短录音上限（秒）"
                type="number"
                min={1}
                max={14_400}
                value={shortRecordingMaxSec}
                onChange={(event) =>
                  setShortRecordingMaxSec(
                    Math.min(
                      14_400,
                      Math.max(1, Number(event.target.value) || 1),
                    ),
                  )
                }
              />
            </label>
            <Button
              htmlType="submit"
              type="primary"
              loading={discoveryMutation.isPending}
              disabled={discoveryMutation.isPending}
            >
              扫描候选
            </Button>
          </form>

          {discoveryValidation && (
            <p className="ag-inline-error" role="alert">
              {discoveryValidation}
            </p>
          )}
          {discoveryMutation.isError && (
            <div className="ag-inline-error" role="alert">
              <strong>候选扫描失败</strong>
              <p>{errorMessage(discoveryMutation.error)}</p>
              <Button size="small" onClick={() => discoveryMutation.reset()}>
                关闭错误
              </Button>
            </div>
          )}
          {acceptMutation.isError && (
            <div className="ag-inline-error" role="alert">
              <strong>候选接受失败</strong>
              <p>{errorMessage(acceptMutation.error)}</p>
              <p>候选未写入，可刷新扫描结果后重试。</p>
            </div>
          )}

          <div className="ag-discovery-results" aria-live="polite">
            {discoveryMutation.isPending ? (
              <div className="ag-inline-loading" role="status">
                <Spin />
                <span>正在扫描真实录音与分段…</span>
              </div>
            ) : discoverySnapshot === null ? (
              <Empty description="设置真实门店与时间窗后开始扫描，不展示模拟候选。" />
            ) : (
              <>
                {discoverySnapshot.result.truncated && (
                  <p className="ag-truncated-note" role="status">
                    本次扫描达到 {DISCOVERY_LIMIT} 条上限，请缩小时间窗后继续。
                  </p>
                )}
                {proposalItems.length === 0 ? (
                  <Empty description="该时间窗内没有可复核候选" />
                ) : (
                  <div className="ag-proposal-grid">
                    {proposalItems.map((proposal) => (
                      <ProposalCard
                        key={proposalKey(proposal)}
                        proposal={proposal}
                        isAccepting={
                          acceptMutation.isPending &&
                          acceptingKey === proposalKey(proposal)
                        }
                        acceptancePending={acceptMutation.isPending}
                        onAccept={acceptProposal}
                      />
                    ))}
                  </div>
                )}
              </>
            )}
          </div>
        </section>
      </main>
    </div>
  );
}

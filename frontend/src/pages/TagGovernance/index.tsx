import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  IconCheck,
  IconCheckCircleFill,
  IconClose,
  IconEye,
  IconExclamationCircleFill,
  IconHistory,
  IconPauseCircle,
  IconTags,
  IconUndo,
} from "@arco-design/web-react/icon";
import {
  type FormEvent,
  type KeyboardEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import {
  approveTagDeployment,
  createTagDeployment,
  createTagEvaluation,
  createTagGoldSet,
  createTagSchema,
  createTagSchemaVersion,
  createTaggerVersion,
  freezeTagGoldSet,
  listTagAuditEvents,
  listTagDeployments,
  listTagDeploymentObservations,
  listTagEvaluations,
  listTagGoldSets,
  listTagSchemas,
  listTaggerVersions,
  publishTagSchemaVersion,
  resumeTagDeployment,
  rollbackTagDeployment,
} from "@/api/services";
import type {
  CreateTagDeploymentRequest,
  CreateTagEvaluationRequest,
  CreateTagGoldSetRequest,
  CreateTagSchemaRequest,
  CreateTagSchemaVersionRequest,
  CreateTaggerVersionRequest,
  FreezeTagGoldSetRequest,
  TagAuditEvent,
  TagDeployment,
  TagEvaluation,
  TagGoldSet,
  TagDefinition,
  TagOptimizationSourceCohort,
  TagSchema,
  TaggerVersion,
} from "@/types/api";
import { PanelState } from "@/components/PanelState";
import {
  compactPercent,
  formatDate,
  numericMetric,
  statusLabel,
} from "@/components/governance/format";
import { Metric } from "@/components/governance/Metric";
import { StatusChip } from "@/components/governance/StatusChip";
import { useAuthStore } from "@/stores/auth";
import { getErrorMessage, getErrorStatus } from "@/utils/errors";
import { EvolutionPanel } from "./EvolutionPanel";
import "./tagGovernance.css";

const TABS = [
  {
    id: "taxonomy",
    label: "标签体系",
    description: "定义语义、适用场景、值域与证据约束",
  },
  {
    id: "taggers",
    label: "抽取版本",
    description: "管理模型、提示词、规则与阈值版本",
  },
  {
    id: "evaluations",
    label: "评估实验",
    description: "以冻结金标集验证质量门禁",
  },
  {
    id: "deployments",
    label: "发布监控",
    description: "影子、灰度、审批与回滚",
  },
  {
    id: "evolution",
    label: "自进化",
    description: "反馈、Badcase、有界搜索与候选差异",
  },
  {
    id: "audit",
    label: "审计",
    description: "追踪治理动作和责任主体",
  },
] as const;

type TabId = (typeof TABS)[number]["id"];

const GOVERNANCE_PENDING_LABEL = "正在加载治理数据…";
const TERMINAL_EVALUATION_STATUSES = new Set(["completed", "failed"]);
const TERMINAL_DEPLOYMENT_STATUSES = new Set([
  "production",
  "rolled_back",
  "retired",
]);
const DRIFT_REVIEW_PAUSE_REASON = "distribution drift requires review";
const RESUMABLE_DEPLOYMENT_STATUSES = new Set<TagDeployment["status"]>([
  "shadow",
  "canary_5",
  "canary_25",
  "awaiting_admin",
]);

function canResumeDriftDeployment(deployment: TagDeployment): boolean {
  return (
    deployment.promotion_paused &&
    deployment.pause_reason === DRIFT_REVIEW_PAUSE_REASON &&
    RESUMABLE_DEPLOYMENT_STATUSES.has(deployment.status)
  );
}

/**
 * 部署是否可引用这次评估：后端 create_deployment 要求
 * metrics.evaluation_lane === "holdout" 且 sealed_release === true。
 * 公开「运行评估」永远走 challenge 通道，产出天然不满足该条件。
 */
function isSealedHoldoutEvaluation(evaluation: TagEvaluation): boolean {
  return (
    evaluation.metrics.evaluation_lane === "holdout" &&
    evaluation.metrics.sealed_release === true
  );
}

/**
 * 把部署冲突翻译给操作员。后端 `_domain` 把所有领域冲突都映射成同一个
 * TAG_GOVERNANCE_CONFLICT / 409，所以「评估未走密封 Holdout」这类创建冲突
 * 只能按 message 识别——否则会被误标成「修订号过期」，把操作员引去刷新。
 */
function deploymentOperationErrorCopy(error: unknown): string {
  const message = getErrorMessage(error, "部署操作失败");
  if (getErrorStatus(error) === 409 && /sealed holdout/i.test(message)) {
    return (
      "该评估不是发布服务在密封 Holdout 上运行的结果，challenge 验证结果" +
      "不能用于部署。请在自进化面板产生候选，待其密封评估通过后再创建部署。"
    );
  }
  return message;
}

function parseOptimizationCohort(
  value: string | null,
): TagOptimizationSourceCohort | null {
  if (!value || value.length > 10_000) return null;
  try {
    const parsed: unknown = JSON.parse(value);
    if (
      typeof parsed !== "object" ||
      parsed === null ||
      Array.isArray(parsed) ||
      typeof (parsed as Record<string, unknown>).source !== "string"
    ) {
      return null;
    }
    return parsed as TagOptimizationSourceCohort;
  } catch {
    return null;
  }
}

function CreateSchemaDialog({
  onClose,
  onCreate,
  pending,
}: {
  onClose: () => void;
  onCreate: (body: CreateTagSchemaRequest) => void;
  pending: boolean;
}) {
  const [key, setKey] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState<string | null>(null);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!/^[\w.-]+$/.test(key.trim())) {
      setError("体系键仅支持字母、数字、下划线、点和短横线。");
      return;
    }
    if (!name.trim()) {
      setError("体系名称不能为空。");
      return;
    }
    setError(null);
    onCreate({
      key: key.trim(),
      name: name.trim(),
      description: description.trim() || undefined,
    });
  };

  return (
    <div className="ag-dialog-backdrop">
      <section
        className="ag-governance-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="schema-dialog-title"
        onKeyDown={(event) => {
          if (event.key === "Escape" && !pending) onClose();
        }}
      >
        <header>
          <div>
            <span className="ag-card-kicker">TAG TAXONOMY</span>
            <h2 id="schema-dialog-title">新建标签体系</h2>
          </div>
          <button
            type="button"
            aria-label="关闭"
            disabled={pending}
            onClick={onClose}
          >
            <IconClose />
          </button>
        </header>
        <form onSubmit={submit}>
          <label>
            体系键
            <input
              autoFocus
              aria-label="体系键"
              placeholder="sales-dialogue"
              value={key}
              onChange={(event) => setKey(event.target.value)}
            />
          </label>
          <label>
            体系名称
            <input
              aria-label="体系名称"
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          <label className="is-full">
            体系说明
            <textarea
              rows={4}
              aria-label="体系说明"
              value={description}
              onChange={(event) => setDescription(event.target.value)}
            />
          </label>
          {error && (
            <p className="ag-inline-feedback is-error" role="alert">
              {error}
            </p>
          )}
          <footer>
            <button
              type="button"
              className="is-secondary"
              disabled={pending}
              onClick={onClose}
            >
              取消
            </button>
            <button type="submit" disabled={pending}>
              {pending ? "正在保存…" : "保存标签体系"}
            </button>
          </footer>
        </form>
      </section>
    </div>
  );
}

const DEFINITION_TEMPLATE = JSON.stringify(
  [
    {
      key: "intent.purchase",
      name: "购买意向",
      category: "intent",
      value_type: "enum",
      allowed_values: ["low", "medium", "high"],
      subject_types: ["dialogue_unit"],
      scenarios: ["gold", "automotive"],
      evidence_required: true,
      critical: true,
      required: false,
      threshold: 0.75,
    },
  ],
  null,
  2,
);

function isTagDefinition(value: unknown): value is TagDefinition {
  if (typeof value !== "object" || value === null) return false;
  const item = value as Record<string, unknown>;
  const allowedTypes = new Set(["enum", "string", "number", "boolean"]);
  return (
    typeof item.key === "string" &&
    Boolean(item.key) &&
    typeof item.name === "string" &&
    Boolean(item.name) &&
    typeof item.category === "string" &&
    Boolean(item.category) &&
    typeof item.value_type === "string" &&
    allowedTypes.has(item.value_type) &&
    Array.isArray(item.allowed_values) &&
    Array.isArray(item.subject_types) &&
    item.subject_types.length > 0 &&
    item.subject_types.every(
      (subject) => subject === "dialogue_unit" || subject === "reception",
    ) &&
    Array.isArray(item.scenarios) &&
    typeof item.evidence_required === "boolean" &&
    typeof item.critical === "boolean" &&
    typeof item.threshold === "number" &&
    item.threshold >= 0 &&
    item.threshold <= 1 &&
    (item.value_type !== "enum" || item.allowed_values.length > 0)
  );
}

function SchemaVersionDialog({
  schemaName,
  onClose,
  onCreate,
  pending,
}: {
  schemaName: string;
  onClose: () => void;
  onCreate: (body: CreateTagSchemaVersionRequest) => void;
  pending: boolean;
}) {
  const [version, setVersion] = useState("");
  const [definitions, setDefinitions] = useState(DEFINITION_TEMPLATE);
  const [error, setError] = useState<string | null>(null);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!/^[\w.-]+$/.test(version.trim())) {
      setError("体系版本号仅支持字母、数字、下划线、点和短横线。");
      return;
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(definitions);
    } catch {
      setError("标签定义 JSON 无法解析。");
      return;
    }
    if (
      !Array.isArray(parsed) ||
      parsed.length === 0 ||
      parsed.length > 256 ||
      !parsed.every(isTagDefinition)
    ) {
      setError(
        "标签定义需为 1~256 项数组，并包含合法键、类型、主体、场景、证据策略和阈值。",
      );
      return;
    }
    setError(null);
    onCreate({ version: version.trim(), definitions: parsed });
  };

  return (
    <div className="ag-dialog-backdrop">
      <section
        className="ag-governance-dialog ag-schema-version-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="schema-version-dialog-title"
        onKeyDown={(event) => {
          if (event.key === "Escape" && !pending) onClose();
        }}
      >
        <header>
          <div>
            <span className="ag-card-kicker">IMMUTABLE SNAPSHOT</span>
            <h2 id="schema-version-dialog-title">创建 {schemaName} 版本</h2>
          </div>
          <button
            type="button"
            aria-label="关闭"
            disabled={pending}
            onClick={onClose}
          >
            <IconClose />
          </button>
        </header>
        <form onSubmit={submit}>
          <label className="is-full">
            体系版本号
            <input
              autoFocus
              aria-label="体系版本号"
              placeholder="1.0.0"
              value={version}
              onChange={(event) => setVersion(event.target.value)}
            />
          </label>
          <label className="is-full">
            标签定义 JSON
            <textarea
              rows={17}
              aria-label="标签定义 JSON"
              value={definitions}
              spellCheck={false}
              onChange={(event) => setDefinitions(event.target.value)}
            />
            <small>
              发布后版本不可变；业务主体只允许 dialogue_unit 或 reception。
            </small>
          </label>
          {error && (
            <p className="ag-inline-feedback is-error" role="alert">
              {error}
            </p>
          )}
          <footer>
            <button
              type="button"
              className="is-secondary"
              disabled={pending}
              onClick={onClose}
            >
              取消
            </button>
            <button type="submit" disabled={pending}>
              {pending ? "正在保存…" : "保存体系版本"}
            </button>
          </footer>
        </form>
      </section>
    </div>
  );
}

function TaxonomyPanel({
  items,
  pending,
  error,
  onRetry,
  isAdmin,
}: {
  items: TagSchema[];
  pending: boolean;
  error: unknown;
  onRetry: () => void;
  isAdmin: boolean;
}) {
  const queryClient = useQueryClient();
  const [dialog, setDialog] = useState<
    | { kind: "schema" }
    | { kind: "version"; schemaId: number; schemaName: string }
    | null
  >(null);
  const [success, setSuccess] = useState<string | null>(null);
  const schemaMutation = useMutation({
    mutationFn: (body: CreateTagSchemaRequest) => createTagSchema(body),
    onSuccess: (schema) => {
      setDialog(null);
      setSuccess(`标签体系 ${schema.name} 已创建`);
      queryClient.invalidateQueries({
        queryKey: ["tag-governance", "schemas"],
      });
    },
  });
  const schemaVersionMutation = useMutation({
    mutationFn: ({
      schemaId,
      body,
    }: {
      schemaId: number;
      body: CreateTagSchemaVersionRequest;
    }) => createTagSchemaVersion(schemaId, body),
    onSuccess: (version) => {
      setDialog(null);
      setSuccess(`体系版本 ${version.version} 已创建，校验后可发布`);
      queryClient.invalidateQueries({
        queryKey: ["tag-governance", "schemas"],
      });
    },
  });
  const publishMutation = useMutation({
    mutationFn: ({
      schemaId,
      versionId,
    }: {
      schemaId: number;
      versionId: number;
    }) => publishTagSchemaVersion(schemaId, versionId),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["tag-governance", "schemas"] }),
  });

  return (
    <>
      <div className="ag-panel-toolbar">
        <div>
          <strong>版本化标签字典</strong>
          <span>
            标签键、值域、证据要求和适用场景随版本冻结，发布后不原地修改。
          </span>
        </div>
        {isAdmin && (
          <button type="button" onClick={() => setDialog({ kind: "schema" })}>
            新建标签体系
          </button>
        )}
      </div>
      {success && (
        <p className="ag-inline-feedback is-success" role="status">
          {success}
        </p>
      )}
      <PanelState
        pending={pending}
        error={error}
        empty={items.length === 0}
        emptyTitle="尚未建立标签体系"
        emptyDescription="先定义标签键、值域、适用场景与证据要求，再创建抽取版本。"
        onRetry={onRetry}
        pendingLabel={GOVERNANCE_PENDING_LABEL}
      >
        <div className="ag-taxonomy-grid">
          {items.map((schema) => {
            const definitions =
              schema.versions?.flatMap((version) => version.definitions) ?? [];
            const latestVersion = schema.versions?.at(0);
            return (
              <article className="ag-governance-card" key={schema.id}>
                <header>
                  <div className="ag-schema-icon" aria-hidden="true">
                    <IconTags />
                  </div>
                  <div>
                    <span className="ag-card-kicker">{schema.key}</span>
                    <h2>{schema.name}</h2>
                    <p>{schema.description || "暂无体系说明"}</p>
                  </div>
                  {latestVersion && <StatusChip status={latestVersion.status} />}
                </header>
                <dl className="ag-schema-summary">
                  <div>
                    <dt>版本</dt>
                    <dd>{latestVersion?.version ?? "—"}</dd>
                  </div>
                  <div>
                    <dt>标签定义</dt>
                    <dd>{definitions.length}</dd>
                  </div>
                  <div>
                    <dt>更新时间</dt>
                    <dd>{formatDate(schema.updated_at)}</dd>
                  </div>
                </dl>
                {definitions.length > 0 && (
                  <div className="ag-definition-list">
                    {definitions.slice(0, 8).map((definition) => (
                      <div key={`${latestVersion?.id}-${definition.key}`}>
                        <span>
                          <strong>{definition.name}</strong>
                          <code>{definition.key}</code>
                        </span>
                        <span>
                          {definition.critical && <b>关键</b>}
                          {definition.evidence_required && (
                            <span className="ag-definition-evidence">
                              需证据
                            </span>
                          )}
                          <small>
                            阈值 {compactPercent(definition.threshold)}
                          </small>
                        </span>
                      </div>
                    ))}
                  </div>
                )}
                {isAdmin && (
                  <footer>
                    <button
                      type="button"
                      className="is-secondary"
                      aria-label={`为${schema.name}创建版本`}
                      onClick={() =>
                        setDialog({
                          kind: "version",
                          schemaId: schema.id,
                          schemaName: schema.name,
                        })
                      }
                    >
                      创建体系版本
                    </button>
                    {latestVersion?.status === "draft" && (
                      <button
                        type="button"
                        disabled={publishMutation.isPending}
                        onClick={() =>
                          publishMutation.mutate({
                            schemaId: schema.id,
                            versionId: latestVersion.id,
                          })
                        }
                      >
                        发布体系版本
                      </button>
                    )}
                  </footer>
                )}
              </article>
            );
          })}
        </div>
      </PanelState>
      {(schemaMutation.isError ||
        schemaVersionMutation.isError ||
        publishMutation.isError) && (
        <p className="ag-inline-feedback is-error" role="alert">
          {getErrorMessage(
            schemaMutation.error ??
              schemaVersionMutation.error ??
              publishMutation.error,
            "标签体系操作失败",
          )}
        </p>
      )}
      {isAdmin && dialog?.kind === "schema" && (
        <CreateSchemaDialog
          pending={schemaMutation.isPending}
          onClose={() => setDialog(null)}
          onCreate={(body) => schemaMutation.mutate(body)}
        />
      )}
      {isAdmin && dialog?.kind === "version" && (
        <SchemaVersionDialog
          schemaName={dialog.schemaName}
          pending={schemaVersionMutation.isPending}
          onClose={() => setDialog(null)}
          onCreate={(body) =>
            schemaVersionMutation.mutate({
              schemaId: dialog.schemaId,
              body,
            })
          }
        />
      )}
    </>
  );
}

function CandidateDialog({
  onClose,
  onCreate,
  pending,
}: {
  onClose: () => void;
  onCreate: (body: CreateTaggerVersionRequest) => void;
  pending: boolean;
}) {
  const [schemaVersionId, setSchemaVersionId] = useState("");
  const [version, setVersion] = useState("");
  const [modelVersion, setModelVersion] = useState("");
  const [engine, setEngine] =
    useState<CreateTaggerVersionRequest["engine"]>("hybrid");
  const [error, setError] = useState<string | null>(null);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const schemaId = Number(schemaVersionId);
    if (!Number.isSafeInteger(schemaId) || schemaId <= 0) {
      setError("请输入有效的标签体系版本 ID。");
      return;
    }
    if (!version.trim() || !modelVersion.trim()) {
      setError("候选版本号和模型版本不能为空。");
      return;
    }
    setError(null);
    onCreate({
      schema_version_id: schemaId,
      version: version.trim(),
      engine,
      prompt_content: "",
      rule_bundle: {},
      model_version: modelVersion.trim(),
      thresholds: {},
    });
  };

  return (
    <div className="ag-dialog-backdrop">
      <section
        className="ag-governance-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="candidate-dialog-title"
      >
        <header>
          <div>
            <span className="ag-card-kicker">VERSION CANDIDATE</span>
            <h2 id="candidate-dialog-title">创建抽取候选版本</h2>
          </div>
          <button type="button" aria-label="关闭" onClick={onClose}>
            <IconClose />
          </button>
        </header>
        <form onSubmit={submit}>
          <label>
            标签体系版本 ID
            <input
              type="number"
              min="1"
              aria-label="标签体系版本 ID"
              value={schemaVersionId}
              onChange={(event) => setSchemaVersionId(event.target.value)}
            />
          </label>
          <label>
            候选版本号
            <input
              aria-label="候选版本号"
              placeholder="tagger-2.4"
              value={version}
              onChange={(event) => setVersion(event.target.value)}
            />
          </label>
          <label>
            抽取引擎
            <select
              aria-label="抽取引擎"
              value={engine}
              onChange={(event) =>
                setEngine(
                  event.target.value as CreateTaggerVersionRequest["engine"],
                )
              }
            >
              <option value="hybrid">模型 + 规则</option>
              <option value="llm">大模型</option>
              <option value="rule">规则引擎</option>
            </select>
          </label>
          <label>
            模型版本
            <input
              aria-label="模型版本"
              placeholder="model-b"
              value={modelVersion}
              onChange={(event) => setModelVersion(event.target.value)}
            />
          </label>
          {error && (
            <p className="ag-inline-feedback is-error" role="alert">
              {error}
            </p>
          )}
          <footer>
            <button type="button" className="is-secondary" onClick={onClose}>
              取消
            </button>
            <button type="submit" disabled={pending}>
              {pending ? "正在保存…" : "保存候选"}
            </button>
          </footer>
        </form>
      </section>
    </div>
  );
}

function TaggersPanel({
  items,
  pending,
  error,
  onRetry,
  isAdmin,
}: {
  items: TaggerVersion[];
  pending: boolean;
  error: unknown;
  onRetry: () => void;
  isAdmin: boolean;
}) {
  const queryClient = useQueryClient();
  const [dialog, setDialog] = useState<"candidate" | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const mutation = useMutation({
    mutationFn: (body: CreateTaggerVersionRequest) =>
      createTaggerVersion(body),
    onSuccess: (candidate) => {
      setSuccess(`候选版本 ${candidate.version} 已创建`);
      setDialog(null);
      queryClient.invalidateQueries({
        queryKey: ["tag-governance", "taggers"],
      });
    },
  });
  return (
    <>
      <div className="ag-panel-toolbar">
        <div>
          <strong>抽取资产</strong>
          <span>版本化保存模型、提示词、规则与阈值，确保每次结果可重现。</span>
        </div>
        <div className="ag-panel-toolbar__actions">
          {isAdmin && (
            <button type="button" onClick={() => setDialog("candidate")}>
              创建候选版本
            </button>
          )}
        </div>
      </div>
      {success && (
        <p className="ag-inline-feedback is-success" role="status">
          {success}
        </p>
      )}
      {mutation.isError && (
        <p className="ag-inline-feedback is-error" role="alert">
          {getErrorMessage(mutation.error, "候选版本创建失败")}
        </p>
      )}
      <PanelState
        pending={pending}
        error={error}
        empty={items.length === 0}
        emptyTitle="暂无抽取版本"
        emptyDescription="创建第一个候选版本后，即可进入金标评估与灰度发布。"
        onRetry={onRetry}
        pendingLabel={GOVERNANCE_PENDING_LABEL}
      >
        <div className="ag-version-table-wrap">
          <table className="ag-version-table">
            <thead>
              <tr>
                <th>版本</th>
                <th>引擎 / 模型</th>
                <th>标签体系</th>
                <th>阈值</th>
                <th>状态</th>
                <th>更新时间</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.id}>
                  <td>
                    <strong>{item.version}</strong>
                    <small>#{item.id}</small>
                  </td>
                  <td>
                    {item.engine}
                    <small>{item.model_version}</small>
                  </td>
                  <td>v#{item.schema_version_id}</td>
                  <td>{Object.keys(item.thresholds).length} 项</td>
                  <td>
                    <StatusChip status={item.status} />
                  </td>
                  <td>{formatDate(item.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </PanelState>
      {isAdmin && dialog === "candidate" && (
        <CandidateDialog
          pending={mutation.isPending}
          onClose={() => setDialog(null)}
          onCreate={(body) => mutation.mutate(body)}
        />
      )}
    </>
  );
}

function GoldSetDialog({
  onClose,
  onCreate,
  pending,
}: {
  onClose: () => void;
  onCreate: (body: CreateTagGoldSetRequest) => void;
  pending: boolean;
}) {
  const [key, setKey] = useState("");
  const [name, setName] = useState("");
  const [schemaVersionId, setSchemaVersionId] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState<string | null>(null);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const schemaId = Number(schemaVersionId);
    if (!/^[\w.-]+$/.test(key.trim()) || !name.trim()) {
      setError("金标集键和名称不能为空，键仅支持字母、数字、点及横线。");
      return;
    }
    if (!Number.isSafeInteger(schemaId) || schemaId <= 0) {
      setError("标签体系版本 ID 必须是正整数。");
      return;
    }
    setError(null);
    onCreate({
      key: key.trim(),
      name: name.trim(),
      description: description.trim() || undefined,
      schema_version_id: schemaId,
    });
  };

  return (
    <div className="ag-dialog-backdrop">
      <section
        className="ag-governance-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="gold-set-dialog-title"
        onKeyDown={(event) => {
          if (event.key === "Escape" && !pending) onClose();
        }}
      >
        <header>
          <div>
            <span className="ag-card-kicker">REVIEWED GOLD SET</span>
            <h2 id="gold-set-dialog-title">新建金标集</h2>
          </div>
          <button
            type="button"
            aria-label="关闭"
            disabled={pending}
            onClick={onClose}
          >
            <IconClose />
          </button>
        </header>
        <form onSubmit={submit}>
          <label>
            金标集键
            <input
              autoFocus
              aria-label="金标集键"
              value={key}
              onChange={(event) => setKey(event.target.value)}
            />
          </label>
          <label>
            金标集名称
            <input
              aria-label="金标集名称"
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          <label>
            标签体系版本 ID
            <input
              type="number"
              min="1"
              aria-label="金标集标签体系版本 ID"
              value={schemaVersionId}
              onChange={(event) => setSchemaVersionId(event.target.value)}
            />
          </label>
          <label>
            说明
            <input
              aria-label="金标集说明"
              value={description}
              onChange={(event) => setDescription(event.target.value)}
            />
          </label>
          {error && (
            <p className="ag-inline-feedback is-error" role="alert">
              {error}
            </p>
          )}
          <footer>
            <button
              type="button"
              className="is-secondary"
              disabled={pending}
              onClick={onClose}
            >
              取消
            </button>
            <button type="submit" disabled={pending}>
              {pending ? "正在保存…" : "保存金标集"}
            </button>
          </footer>
        </form>
      </section>
    </div>
  );
}

function FreezeGoldSetDialog({
  goldSet,
  onClose,
  onFreeze,
  pending,
}: {
  goldSet: TagGoldSet;
  onClose: () => void;
  onFreeze: (body: FreezeTagGoldSetRequest) => void;
  pending: boolean;
}) {
  const [version, setVersion] = useState("");
  const [reviewBundleIds, setReviewBundleIds] = useState("");
  const [completeness, setCompleteness] = useState({
    full_applicable_matrix: false,
    frozen_input_snapshots: false,
    reception_level_isolation: false,
    t2_t3_truth_only: false,
  });
  const [error, setError] = useState<string | null>(null);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!/^[\w.-]+$/.test(version.trim())) {
      setError("金标版本号不能为空，且只能包含字母、数字、点及横线。");
      return;
    }
    const bundleIds = [
      ...new Set(
        reviewBundleIds
          .split(/[\s,，]+/)
          .filter(Boolean)
          .map((value) => value.trim()),
      ),
    ];
    if (
      bundleIds.length === 0 ||
      bundleIds.length > 1_000 ||
      bundleIds.some((id) => id.length > 255)
    ) {
      setError("请输入 1~1,000 个有效的复核批次 ID，用逗号分隔。");
      return;
    }
    if (!Object.values(completeness).every(Boolean)) {
      setError("冻结前必须逐项确认四项金标完整性约束。");
      return;
    }
    setError(null);
    onFreeze({
      version: version.trim(),
      cohort: {
        review_bundle_ids: bundleIds,
        truth_tiers: ["t2", "t3"],
        subject_types: ["dialogue_unit", "reception"],
      },
      completeness_checklist: {
        full_applicable_matrix: true,
        frozen_input_snapshots: true,
        reception_level_isolation: true,
        t2_t3_truth_only: true,
      },
    });
  };

  return (
    <div className="ag-dialog-backdrop">
      <section
        className="ag-governance-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="freeze-gold-set-dialog-title"
        onKeyDown={(event) => {
          if (event.key === "Escape" && !pending) onClose();
        }}
      >
        <header>
          <div>
            <span className="ag-card-kicker">IMMUTABLE HOLDOUT</span>
            <h2 id="freeze-gold-set-dialog-title">
              冻结 {goldSet.name} 版本
            </h2>
          </div>
          <button
            type="button"
            aria-label="关闭"
            disabled={pending}
            onClick={onClose}
          >
            <IconClose />
          </button>
        </header>
        <form onSubmit={submit}>
          <label className="is-full">
            金标版本号
            <input
              autoFocus
              aria-label="金标版本号"
              placeholder="2026.07"
              value={version}
              onChange={(event) => setVersion(event.target.value)}
            />
          </label>
          <label className="is-full">
            复核批次 ID
            <textarea
              rows={3}
              aria-label="复核批次 ID"
              placeholder="release-2026-07, audit-2026-07"
              value={reviewBundleIds}
              onChange={(event) => setReviewBundleIds(event.target.value)}
            />
            <small>
              服务端只从指定复核批次解析 T2/T3 真值，覆盖对话单元与接待级样本；
              客户端不能直接指定决策记录。
            </small>
          </label>
          <fieldset className="ag-gold-checklist">
            <legend>冻结完整性确认</legend>
            {[
              ["full_applicable_matrix", "已覆盖所有适用标签矩阵"],
              ["frozen_input_snapshots", "已冻结输入快照"],
              ["reception_level_isolation", "已按接待隔离样本"],
              ["t2_t3_truth_only", "仅包含 T2/T3 真值"],
            ].map(([key, label]) => (
              <label key={key}>
                <input
                  type="checkbox"
                  checked={
                    completeness[key as keyof typeof completeness]
                  }
                  onChange={(event) =>
                    setCompleteness((current) => ({
                      ...current,
                      [key]: event.target.checked,
                    }))
                  }
                />
                {label}
              </label>
            ))}
          </fieldset>
          {error && (
            <p className="ag-inline-feedback is-error" role="alert">
              {error}
            </p>
          )}
          <footer>
            <button
              type="button"
              className="is-secondary"
              disabled={pending}
              onClick={onClose}
            >
              取消
            </button>
            <button type="submit" disabled={pending}>
              {pending ? "正在冻结…" : "冻结金标版本"}
            </button>
          </footer>
        </form>
      </section>
    </div>
  );
}

function EvaluationDialog({
  onClose,
  onCreate,
  pending,
}: {
  onClose: () => void;
  onCreate: (body: CreateTagEvaluationRequest) => void;
  pending: boolean;
}) {
  const [taggerVersionId, setTaggerVersionId] = useState("");
  const [goldSetVersionId, setGoldSetVersionId] = useState("");
  const [baselineVersionId, setBaselineVersionId] = useState("");
  const [error, setError] = useState<string | null>(null);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const taggerId = Number(taggerVersionId);
    const goldSetId = Number(goldSetVersionId);
    const baselineId = Number(baselineVersionId);
    if (
      !Number.isSafeInteger(taggerId) ||
      taggerId <= 0 ||
      !Number.isSafeInteger(goldSetId) ||
      goldSetId <= 0 ||
      !Number.isSafeInteger(baselineId) ||
      baselineId <= 0
    ) {
      setError("候选、基线抽取版本 ID 和金标集版本 ID 必须是正整数。");
      return;
    }
    if (taggerId === baselineId) {
      setError("基线抽取版本不能与候选版本相同。");
      return;
    }
    setError(null);
    onCreate({
      tagger_version_id: taggerId,
      gold_set_version_id: goldSetId,
      baseline_tagger_version_id: baselineId,
    });
  };

  return (
    <div className="ag-dialog-backdrop">
      <section
        className="ag-governance-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="evaluation-dialog-title"
        onKeyDown={(event) => {
          if (event.key === "Escape" && !pending) onClose();
        }}
      >
        <header>
          <div>
            <span className="ag-card-kicker">HOLDOUT EVALUATION</span>
            <h2 id="evaluation-dialog-title">运行冻结金标评估</h2>
          </div>
          <button
            type="button"
            aria-label="关闭"
            disabled={pending}
            onClick={onClose}
          >
            <IconClose />
          </button>
        </header>
        <form onSubmit={submit}>
          <label>
            候选抽取版本 ID
            <input
              type="number"
              min="1"
              autoFocus
              aria-label="候选抽取版本 ID"
              value={taggerVersionId}
              onChange={(event) => setTaggerVersionId(event.target.value)}
            />
          </label>
          <label>
            金标集版本 ID
            <input
              type="number"
              min="1"
              aria-label="金标集版本 ID"
              value={goldSetVersionId}
              onChange={(event) => setGoldSetVersionId(event.target.value)}
            />
          </label>
          <label className="is-full">
            基线抽取版本 ID
            <input
              type="number"
              min="1"
              aria-label="基线抽取版本 ID"
              value={baselineVersionId}
              onChange={(event) => setBaselineVersionId(event.target.value)}
            />
            <small>
              后端会在同一冻结 holdout 上对候选与基线双跑，前端不能传入或篡改指标。
            </small>
          </label>
          {error && (
            <p className="ag-inline-feedback is-error" role="alert">
              {error}
            </p>
          )}
          <footer>
            <button
              type="button"
              className="is-secondary"
              disabled={pending}
              onClick={onClose}
            >
              取消
            </button>
            <button type="submit" disabled={pending}>
              {pending ? "正在创建…" : "启动评估任务"}
            </button>
          </footer>
        </form>
      </section>
    </div>
  );
}

function EvaluationsPanel({
  items,
  pending,
  error,
  onRetry,
}: {
  items: TagEvaluation[];
  pending: boolean;
  error: unknown;
  onRetry: () => void;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [dialog, setDialog] = useState<
    | { kind: "evaluation" }
    | { kind: "gold-set" }
    | { kind: "freeze"; goldSet: TagGoldSet }
    | null
  >(null);
  const [success, setSuccess] = useState<string | null>(null);
  const goldSetsQuery = useQuery({
    queryKey: ["tag-governance", "gold-sets"],
    queryFn: listTagGoldSets,
    retry: false,
  });
  const goldSetMutation = useMutation({
    mutationFn: (body: CreateTagGoldSetRequest) => createTagGoldSet(body),
    onSuccess: (goldSet) => {
      setDialog(null);
      setSuccess(`金标集 ${goldSet.name} 已创建`);
      queryClient.invalidateQueries({
        queryKey: ["tag-governance", "gold-sets"],
      });
    },
  });
  const freezeMutation = useMutation({
    mutationFn: ({
      goldSetId,
      body,
    }: {
      goldSetId: number;
      body: FreezeTagGoldSetRequest;
    }) => freezeTagGoldSet(goldSetId, body),
    onSuccess: (version) => {
      setDialog(null);
      setSuccess(
        `已冻结金标版本 #${version.id}，共 ${version.item_count} 项`,
      );
      queryClient.invalidateQueries({
        queryKey: ["tag-governance", "gold-sets"],
      });
    },
  });
  const evaluationMutation = useMutation({
    mutationFn: (body: CreateTagEvaluationRequest) =>
      createTagEvaluation(
        body,
        `evaluation-${body.tagger_version_id}-${body.gold_set_version_id}-${Date.now().toString(36)}`,
      ),
    onSuccess: ({ job_id: jobId }) => {
      setDialog(null);
      queryClient.invalidateQueries({
        queryKey: ["tag-governance", "evaluations"],
      });
      navigate(`/tag-runs/${jobId}`);
    },
  });

  return (
    <>
      <div className="ag-panel-toolbar">
        <div>
          <strong>冻结金标质量门禁</strong>
          <span>
            在后端隔离的 holdout 数据上计算指标，运行状态与失败原因可完整追踪。
          </span>
        </div>
        <div className="ag-panel-toolbar__actions">
          <button
            type="button"
            className="is-secondary"
            onClick={() => setDialog({ kind: "gold-set" })}
          >
            新建金标集
          </button>
          <button
            type="button"
            onClick={() => setDialog({ kind: "evaluation" })}
          >
            运行评估
          </button>
        </div>
      </div>
      {success && (
        <p className="ag-inline-feedback is-success" role="status">
          {success}
        </p>
      )}
      {(evaluationMutation.isError ||
        goldSetMutation.isError ||
        freezeMutation.isError) && (
        <p className="ag-inline-feedback is-error" role="alert">
          {getErrorMessage(
            evaluationMutation.error ??
              goldSetMutation.error ??
              freezeMutation.error,
            "评估资产操作失败",
          )}
        </p>
      )}
      <section className="ag-gold-set-section" aria-labelledby="gold-set-title">
        <header>
          <div>
            <span className="ag-card-kicker">GOLD SET REGISTRY</span>
            <h2 id="gold-set-title">金标集</h2>
          </div>
          <Link to="/tag-review">管理复核队列</Link>
        </header>
        {goldSetsQuery.isPending ? (
          <p className="ag-compact-state" role="status">
            正在加载金标集…
          </p>
        ) : goldSetsQuery.isError ? (
          <p className="ag-compact-state is-error" role="alert">
            金标集加载失败：
            {getErrorMessage(goldSetsQuery.error)}
          </p>
        ) : goldSetsQuery.data.items.length === 0 ? (
          <p className="ag-compact-state">
            尚无金标集，请先完成人工复核并创建冻结快照。
          </p>
        ) : (
          <div className="ag-gold-set-list">
            {goldSetsQuery.data.items.map((goldSet) => (
              <article key={goldSet.id}>
                <span>
                  <strong>{goldSet.name}</strong>
                  <code>{goldSet.key}</code>
                </span>
                <small>体系版本 #{goldSet.schema_version_id}</small>
                <button
                  type="button"
                  aria-label={`冻结${goldSet.name}版本`}
                  onClick={() => setDialog({ kind: "freeze", goldSet })}
                >
                  冻结版本
                </button>
              </article>
            ))}
          </div>
        )}
      </section>
      <PanelState
        pending={pending}
        error={error}
        empty={items.length === 0}
        emptyTitle="暂无评估实验"
        emptyDescription="冻结金标集后运行候选版本评估，系统会执行质量门禁。"
        onRetry={onRetry}
        pendingLabel={GOVERNANCE_PENDING_LABEL}
      >
        <div className="ag-evaluation-grid">
          {items.map((evaluation) => {
            const evaluationPending =
              evaluation.status === "queued" || evaluation.status === "running";
            return (
              <article className="ag-governance-card" key={evaluation.id}>
                <header>
                  <div>
                    <span className="ag-card-kicker">
                      EVALUATION #{evaluation.id}
                    </span>
                    <h2>候选 #{evaluation.tagger_version_id}</h2>
                    <p>
                      候选 #{evaluation.tagger_version_id} 对比基线 #
                      {evaluation.baseline_tagger_version_id} · 金标集版本 #
                      {evaluation.gold_set_version_id} ·{" "}
                      {formatDate(evaluation.created_at)}
                    </p>
                  </div>
                  {evaluationPending ? (
                    <StatusChip status={evaluation.status ?? "queued"} />
                  ) : (
                    <span
                      className={`ag-gate-badge ${evaluation.passed ? "is-pass" : "is-fail"}`}
                      role="status"
                      aria-label={
                        evaluation.passed
                          ? "质量门禁通过"
                          : "质量门禁未通过"
                      }
                    >
                      {evaluation.passed ? (
                        <>
                          <IconCheckCircleFill aria-hidden="true" /> 门禁通过
                        </>
                      ) : (
                        <>
                          <IconExclamationCircleFill aria-hidden="true" /> 门禁拦截
                        </>
                      )}
                    </span>
                  )}
                </header>
                <div className="ag-quality-grid">
                  <Metric
                    label="Macro F1"
                    value={evaluation.metrics.macro_f1}
                  />
                  <Metric
                    label="关键标签召回"
                    value={evaluation.metrics.critical_recall}
                  />
                  <Metric
                    label="证据覆盖"
                    value={evaluation.metrics.evidence_coverage}
                  />
                  <Metric
                    label="错误率"
                    value={evaluation.metrics.error_rate}
                    inverse
                  />
                </div>
                <div className="ag-gate-list">
                  {evaluation.gates.map((gate) => (
                    <div key={gate.code}>
                      <span className={gate.passed ? "is-pass" : "is-fail"}>
                        {gate.passed ? (
                          <IconCheck aria-hidden="true" />
                        ) : (
                          <IconExclamationCircleFill aria-hidden="true" />
                        )}
                      </span>
                      <span>
                        <strong>{gate.code}</strong>
                        <small>{gate.message}</small>
                      </span>
                      <b>
                        {compactPercent(gate.actual)} /{" "}
                        {compactPercent(gate.threshold)}
                      </b>
                    </div>
                  ))}
                </div>
                {/* 评估通道决定结果能不能进入发布：challenge 结果后端会
                    409 拒绝部署，所以在卡片上就说明去向，别等到部署报错。 */}
                {!evaluationPending &&
                  (isSealedHoldoutEvaluation(evaluation) ? (
                    evaluation.passed ? (
                      <footer className="ag-evaluation-lane is-holdout">
                        <span>
                          Sealed Holdout 评估已通过，可进入受控发布。
                        </span>
                        <Link
                          to={`/tag-governance?tab=deployments&deploy_evaluation_id=${evaluation.id}`}
                        >
                          创建影子部署
                        </Link>
                      </footer>
                    ) : (
                      <footer className="ag-evaluation-lane is-holdout">
                        <span>
                          Sealed Holdout 评估未通过，该候选不能部署。
                        </span>
                      </footer>
                    )
                  ) : (
                    <footer className="ag-evaluation-lane is-challenge">
                      <span>
                        Challenge 验证通道：仅验证结果，不能直接用于部署，
                        请走自进化产生候选。
                      </span>
                      <Link to="/tag-governance?tab=evolution">
                        前往自进化
                      </Link>
                    </footer>
                  ))}
              </article>
            );
          })}
        </div>
      </PanelState>
      {dialog?.kind === "evaluation" && (
        <EvaluationDialog
          pending={evaluationMutation.isPending}
          onClose={() => setDialog(null)}
          onCreate={(body) => evaluationMutation.mutate(body)}
        />
      )}
      {dialog?.kind === "gold-set" && (
        <GoldSetDialog
          pending={goldSetMutation.isPending}
          onClose={() => setDialog(null)}
          onCreate={(body) => goldSetMutation.mutate(body)}
        />
      )}
      {dialog?.kind === "freeze" && (
        <FreezeGoldSetDialog
          goldSet={dialog.goldSet}
          pending={freezeMutation.isPending}
          onClose={() => setDialog(null)}
          onFreeze={(body) =>
            freezeMutation.mutate({
              goldSetId: dialog.goldSet.id,
              body,
            })
          }
        />
      )}
    </>
  );
}

function deploymentStep(status: TagDeployment["status"]): number {
  return (
    {
      shadow: 0,
      canary_5: 1,
      canary_25: 2,
      awaiting_admin: 3,
      production: 4,
      rolled_back: 4,
      retired: 4,
    } satisfies Record<TagDeployment["status"], number>
  )[status];
}

function DeploymentDialog({
  taggerVersions,
  initialEvaluationId = null,
  serverError = null,
  onClose,
  onCreate,
  pending,
}: {
  taggerVersions: TaggerVersion[];
  /** 从评估卡片 / 自进化 CTA 带过来的密封评估 ID，避免人工二次抄录。 */
  initialEvaluationId?: string | null;
  serverError?: unknown;
  onClose: () => void;
  onCreate: (body: CreateTagDeploymentRequest) => void;
  pending: boolean;
}) {
  const [taggerVersionId, setTaggerVersionId] = useState("");
  const [evaluationId, setEvaluationId] = useState(initialEvaluationId ?? "");
  const [baselineVersionId, setBaselineVersionId] = useState("");
  const [error, setError] = useState<string | null>(null);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const taggerId = Number(taggerVersionId);
    const evaluationRunId = Number(evaluationId);
    const baselineId = Number(baselineVersionId);
    if (
      !Number.isSafeInteger(taggerId) ||
      taggerId <= 0 ||
      !Number.isSafeInteger(evaluationRunId) ||
      evaluationRunId <= 0 ||
      !Number.isSafeInteger(baselineId) ||
      baselineId <= 0
    ) {
      setError("候选、评估与回滚基线 ID 均必须填写正整数。");
      return;
    }
    if (taggerId === baselineId) {
      setError("回滚基线不能与候选抽取版本相同。");
      return;
    }
    const candidate = taggerVersions.find((item) => item.id === taggerId);
    const baseline = taggerVersions.find((item) => item.id === baselineId);
    if (!candidate || !baseline) {
      setError("候选和回滚基线必须来自当前租户已加载的抽取版本。");
      return;
    }
    if (candidate.schema_version_id !== baseline.schema_version_id) {
      setError("回滚基线必须与候选抽取版本使用同一标签体系版本。");
      return;
    }
    setError(null);
    onCreate({
      tagger_version_id: taggerId,
      evaluation_run_id: evaluationRunId,
      baseline_tagger_version_id: baselineId,
    });
  };

  return (
    <div className="ag-dialog-backdrop">
      <section
        className="ag-governance-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="deployment-dialog-title"
        onKeyDown={(event) => {
          if (event.key === "Escape" && !pending) onClose();
        }}
      >
        <header>
          <div>
            <span className="ag-card-kicker">SAFE RELEASE</span>
            <h2 id="deployment-dialog-title">创建影子部署</h2>
          </div>
          <button
            type="button"
            aria-label="关闭"
            disabled={pending}
            onClick={onClose}
          >
            <IconClose />
          </button>
        </header>
        <form onSubmit={submit}>
          <label>
            抽取版本 ID
            <input
              autoFocus
              type="number"
              min="1"
              list="deployment-candidate-versions"
              aria-label="部署抽取版本 ID"
              value={taggerVersionId}
              onChange={(event) => setTaggerVersionId(event.target.value)}
            />
          </label>
          <label>
            通过门禁的评估 ID
            <input
              type="number"
              min="1"
              aria-label="部署评估 ID"
              value={evaluationId}
              onChange={(event) => setEvaluationId(event.target.value)}
            />
          </label>
          <label className="is-full">
            回滚基线抽取版本 ID
            <input
              type="number"
              min="1"
              list="deployment-baseline-versions"
              aria-label="部署基线抽取版本 ID"
              value={baselineVersionId}
              onChange={(event) => setBaselineVersionId(event.target.value)}
            />
            <small>
              必填，且必须与候选版本使用同一标签体系、版本 ID 不同。灰度门禁触发时，系统按该版本恢复可见事实。
            </small>
          </label>
          <datalist id="deployment-candidate-versions">
            {taggerVersions.map((item) => (
              <option key={item.id} value={item.id}>
                {item.version} · 体系 #{item.schema_version_id}
              </option>
            ))}
          </datalist>
          <datalist id="deployment-baseline-versions">
            {taggerVersions
              .filter((item) => item.status === "qualified")
              .map((item) => (
                <option key={item.id} value={item.id}>
                  {item.version} · 体系 #{item.schema_version_id}
                </option>
              ))}
          </datalist>
          <p className="ag-deployment-hard-gate">
            所有质量与样本量门禁均为硬门禁，不支持人工覆盖。评估未通过时，
            服务端会拒绝创建部署。
          </p>
          {error && (
            <p className="ag-inline-feedback is-error" role="alert">
              {error}
            </p>
          )}
          {/* 服务端拒绝要在对话框里就地解释：创建失败时对话框不会关闭，
              渲染在其背后的面板错误条会被遮住。 */}
          {!error && serverError !== null && serverError !== undefined && (
            <p className="ag-inline-feedback is-error" role="alert">
              {deploymentOperationErrorCopy(serverError)}
            </p>
          )}
          <footer>
            <button
              type="button"
              className="is-secondary"
              disabled={pending}
              onClick={onClose}
            >
              取消
            </button>
            <button type="submit" disabled={pending}>
              {pending ? "正在创建…" : "创建影子部署"}
            </button>
          </footer>
        </form>
      </section>
    </div>
  );
}

function RollbackDialog({
  deploymentId,
  onClose,
  onRollback,
  pending,
}: {
  deploymentId: number;
  onClose: () => void;
  onRollback: (reason: string) => void;
  pending: boolean;
}) {
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!reason.trim()) {
      setError("回滚原因不能为空，审计记录需要保留决策依据。");
      return;
    }
    setError(null);
    onRollback(reason.trim());
  };
  return (
    <div className="ag-dialog-backdrop">
      <section
        className="ag-governance-dialog ag-danger-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="rollback-dialog-title"
        onKeyDown={(event) => {
          if (event.key === "Escape" && !pending) onClose();
        }}
      >
        <header>
          <div>
            <span className="ag-card-kicker">AUDITED ROLLBACK</span>
            <h2 id="rollback-dialog-title">回滚部署 #{deploymentId}</h2>
          </div>
          <button
            type="button"
            aria-label="关闭"
            disabled={pending}
            onClick={onClose}
          >
            <IconClose />
          </button>
        </header>
        <form onSubmit={submit}>
          <label className="is-full">
            回滚原因
            <textarea
              autoFocus
              rows={5}
              aria-label="回滚原因"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
            <small>
              回滚会恢复基线可见事实；无基线的主体将自动进入修复队列。
            </small>
          </label>
          {error && (
            <p className="ag-inline-feedback is-error" role="alert">
              {error}
            </p>
          )}
          <footer>
            <button
              type="button"
              className="is-secondary"
              disabled={pending}
              onClick={onClose}
            >
              取消
            </button>
            <button type="submit" className="is-danger" disabled={pending}>
              {pending ? "正在回滚…" : "确认回滚"}
            </button>
          </footer>
        </form>
      </section>
    </div>
  );
}

function ResumeDeploymentDialog({
  deploymentId,
  error,
  onClose,
  onRefresh,
  onResume,
  pending,
}: {
  deploymentId: number;
  error: unknown;
  onClose: () => void;
  onRefresh: () => void;
  onResume: (reason: string) => void;
  pending: boolean;
}) {
  const [reason, setReason] = useState("");
  const [validationError, setValidationError] = useState<string | null>(null);
  const staleRevision = getErrorStatus(error) === 409;
  const normalizedReason = reason.trim();
  const reasonLength = Array.from(normalizedReason).length;
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!normalizedReason) {
      setValidationError("管理员复核结论不能为空。");
      return;
    }
    if (reasonLength < 8) {
      setValidationError("管理员复核结论至少需要 8 个字符。");
      return;
    }
    setValidationError(null);
    onResume(normalizedReason);
  };
  return (
    <div className="ag-dialog-backdrop">
      <section
        className="ag-governance-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="resume-deployment-dialog-title"
        onKeyDown={(event) => {
          if (event.key === "Escape" && !pending) onClose();
        }}
      >
        <header>
          <div>
            <span className="ag-card-kicker">DRIFT REVIEW</span>
            <h2 id="resume-deployment-dialog-title">
              完成复核并恢复部署 #{deploymentId}
            </h2>
          </div>
          <button
            type="button"
            aria-label="关闭"
            disabled={pending}
            onClick={onClose}
          >
            <IconClose />
          </button>
        </header>
        <form onSubmit={submit}>
          <label className="is-full">
            管理员复核结论 / 恢复理由
            <textarea
              autoFocus
              rows={5}
              aria-label="管理员复核结论 / 恢复理由"
              value={reason}
              onChange={(event) => {
                const nextReason = event.target.value;
                const nextLength = Array.from(nextReason.trim()).length;
                setReason(nextReason);
                setValidationError(
                  nextLength > 0 && nextLength < 8
                    ? "管理员复核结论至少需要 8 个字符。"
                    : null,
                );
              }}
            />
            <small>
              此操作仅解除漂移暂停，发布阶段和流量不会改变；后续仍由可信
              Monitor 根据锁定门禁自动推进。
            </small>
            <small>管理员复核结论至少需要 8 个字符。</small>
          </label>
          {(validationError !== null ||
            (error !== null && error !== undefined)) && (
            <p className="ag-inline-feedback is-error" role="alert">
              {validationError ??
                (staleRevision
                  ? "部署已被其他操作更新，当前修订号已过期。刷新后再执行，避免覆盖并发变更。"
                  : getErrorMessage(error, "恢复自动推进失败"))}
            </p>
          )}
          <footer>
            <button
              type="button"
              className="is-secondary"
              disabled={pending}
              onClick={onClose}
            >
              取消
            </button>
            {staleRevision && (
              <button type="button" disabled={pending} onClick={onRefresh}>
                刷新部署状态
              </button>
            )}
            <button
              type="submit"
              disabled={pending || staleRevision || reasonLength < 8}
            >
              {pending ? "正在恢复…" : "确认恢复推进"}
            </button>
          </footer>
        </form>
      </section>
    </div>
  );
}

function automaticPromotionWaiting(
  status: TagDeployment["status"],
): string | null {
  return (
    {
      shadow:
        "等待 Monitor 完成 Shadow 阶段的锁定时长、配对样本、随机审计与硬门禁；通过后自动进入 5% 灰度。",
      canary_5:
        "等待 Monitor 完成 5% 灰度的锁定时长、服务样本、随机审计与硬门禁；通过后自动进入 25% 灰度。",
      canary_25:
        "等待 Monitor 完成 25% 灰度的锁定时长、服务样本、随机审计与硬门禁；通过后自动进入管理员审批。",
      awaiting_admin: "自动灰度门禁已全部通过，等待管理员明确批准进入生产。",
      production: null,
      rolled_back: null,
      retired: null,
    } satisfies Record<TagDeployment["status"], string | null>
  )[status];
}

function observationActionLabel(
  action: "observe" | "pause" | "rollback",
): string {
  return {
    observe: "持续观测",
    pause: "暂停推进",
    rollback: "自动回滚",
  }[action];
}

function ObservationActionIcon({
  action,
}: {
  action: "observe" | "pause" | "rollback";
}) {
  const Icon =
    action === "rollback"
      ? IconUndo
      : action === "pause"
        ? IconPauseCircle
        : IconEye;
  return (
    <span className={`ag-observation-action-icon is-${action}`} aria-hidden="true">
      <Icon />
    </span>
  );
}

function DeploymentObservations({
  deployment,
}: {
  deployment: TagDeployment;
}) {
  const monitoringActive =
    deployment.status !== "rolled_back" && deployment.status !== "retired";
  const [expanded, setExpanded] = useState(monitoringActive);
  const query = useQuery({
    queryKey: [
      "tag-governance",
      "deployment-observations",
      deployment.id,
    ],
    queryFn: () => listTagDeploymentObservations(deployment.id),
    enabled: monitoringActive || expanded,
    retry: false,
    refetchInterval: monitoringActive ? 5_000 : false,
  });
  const observations = useMemo(
    () =>
      [...(query.data?.items ?? [])].sort(
        (left, right) =>
          Date.parse(left.window_end) - Date.parse(right.window_end),
      ),
    [query.data?.items],
  );
  const isDemoObservation = observations.some(
    (item) => item.is_demo === true || item.data_source === "demo",
  );
  const stageSamples = useMemo(() => {
    const totals = new Map<string, number>();
    observations.forEach((item) => {
      totals.set(item.stage, (totals.get(item.stage) ?? 0) + item.sample_count);
    });
    return [...totals.entries()];
  }, [observations]);
  const errorRateSeries = observations
    .map((item) => ({
      id: item.id,
      value: numericMetric(item.metrics.error_rate),
    }))
    .filter(
      (item): item is { id: number; value: number } =>
        item.value !== null,
    );
  const driftSeries = observations
    .map((item) => ({
      id: item.id,
      value: numericMetric(item.metrics.drift_max_jsd),
    }))
    .filter(
      (item): item is { id: number; value: number } =>
        item.value !== null,
    );
  const maxErrorRate = Math.max(
    0.01,
    ...errorRateSeries.map((item) => item.value),
  );
  const maxDrift = Math.max(0.1, ...driftSeries.map((item) => item.value));
  const trendPoints = (series: Array<{ id: number; value: number }>, max: number) =>
    series
      .map((item, index) => {
        const x =
          series.length <= 1 ? 0 : (index / (series.length - 1)) * 260;
        const y = 58 - (item.value / max) * 52;
        return `${x},${y}`;
      })
      .join(" ");

  if (!expanded) {
    return (
      <button
        type="button"
        className="ag-observation-history-toggle"
        aria-expanded="false"
        onClick={() => setExpanded(true)}
      >
        查看历史发布观测
      </button>
    );
  }

  return (
    <section
      className="ag-deployment-observations"
      aria-labelledby={`deployment-observations-${deployment.id}`}
    >
      <header>
        <strong id={`deployment-observations-${deployment.id}`}>
          发布健康观测
        </strong>
        <div>
          <span>
            {query.isFetching
              ? "同步中…"
              : isDemoObservation
                ? "演示数据 · 5 分钟窗口"
                : "生产观测 · 5 分钟窗口"}
          </span>
          {!monitoringActive && (
            <button
              type="button"
              aria-expanded="true"
              onClick={() => setExpanded(false)}
            >
              收起
            </button>
          )}
        </div>
      </header>
      {query.isPending && <p role="status">正在加载灰度观测…</p>}
      {query.isError && (
        <div className="ag-observation-error" role="alert">
          <span>
            {getErrorMessage(query.error, "发布观测加载失败")}
          </span>
          <button type="button" onClick={() => void query.refetch()}>
            重新加载观测
          </button>
        </div>
      )}
      {!query.isPending && !query.isError && observations.length === 0 && (
        <p className="ag-observation-empty">
          尚无观测数据；影子或灰度流量产生样本后会显示趋势与门禁动作。
        </p>
      )}
      {observations.length > 0 && (
        <>
          <dl className="ag-observation-samples">
            {stageSamples.map(([stage, sampleCount]) => (
              <div key={stage}>
                <dt>{statusLabel(stage)}</dt>
                <dd>{sampleCount.toLocaleString("zh-CN")} 样本</dd>
              </div>
            ))}
          </dl>
          <div className="ag-observation-trends">
            <div className="ag-observation-trend">
              <div>
                <strong>错误率趋势</strong>
                <span>
                  最新 {compactPercent(errorRateSeries.at(-1)?.value)}
                </span>
              </div>
              {errorRateSeries.length > 0 ? (
                <svg
                  viewBox="0 0 260 64"
                  role="img"
                  aria-label={`部署 ${deployment.id} error_rate 5分钟趋势`}
                  preserveAspectRatio="none"
                >
                  <polyline points={trendPoints(errorRateSeries, maxErrorRate)} />
                </svg>
              ) : (
                <p>观测尚未返回 error_rate。</p>
              )}
            </div>
            <div className="ag-observation-trend is-drift">
              <div>
                <strong>候选 / 基线分布漂移</strong>
                <span>
                  最新 JSD{" "}
                  {driftSeries.at(-1)?.value.toFixed(3) ?? "—"}
                </span>
              </div>
              {driftSeries.length > 0 ? (
                <svg
                  viewBox="0 0 260 64"
                  role="img"
                  aria-label={`部署 ${deployment.id} Jensen-Shannon 漂移趋势`}
                  preserveAspectRatio="none"
                >
                  <polyline points={trendPoints(driftSeries, maxDrift)} />
                </svg>
              ) : (
                <p>同输入配对样本达到门槛后显示真实漂移。</p>
              )}
            </div>
          </div>
          <ol className="ag-observation-timeline">
            {[...observations].reverse().map((item) => (
              <li key={item.id}>
                <ObservationActionIcon action={item.action} />
                <div>
                  <strong>
                    {statusLabel(item.stage)} ·{" "}
                    {observationActionLabel(item.action)}
                  </strong>
                  <time dateTime={item.window_end}>
                    {formatDate(item.window_end)}
                  </time>
                  <p>
                    {item.sample_count} 样本 · error_rate{" "}
                    {compactPercent(numericMetric(item.metrics.error_rate))}
                    {numericMetric(item.metrics.drift_max_jsd) !== null
                      ? ` · JSD ${numericMetric(item.metrics.drift_max_jsd)?.toFixed(3)}`
                      : ""}
                  </p>
                  {item.breach_codes.length > 0 && (
                    <small>
                      门禁：{item.breach_codes.join("、")}
                    </small>
                  )}
                </div>
              </li>
            ))}
          </ol>
        </>
      )}
    </section>
  );
}

function DeploymentsPanel({
  items,
  taggerVersions,
  pending,
  error,
  onRetry,
  isAdmin,
  initialEvaluationId = null,
}: {
  items: TagDeployment[];
  taggerVersions: TaggerVersion[];
  pending: boolean;
  error: unknown;
  onRetry: () => void;
  isAdmin: boolean;
  /** 评估卡片 / 自进化 CTA 通过 deploy_evaluation_id 深链预填的评估 ID。 */
  initialEvaluationId?: string | null;
}) {
  const queryClient = useQueryClient();
  const [dialog, setDialog] = useState<
    | { kind: "create" }
    | { kind: "rollback"; deploymentId: number; revision: number }
    | { kind: "resume"; deploymentId: number; revision: number }
    | null
  >(isAdmin && initialEvaluationId ? { kind: "create" } : null);
  useEffect(() => {
    if (isAdmin && initialEvaluationId) setDialog({ kind: "create" });
  }, [initialEvaluationId, isAdmin]);
  const [success, setSuccess] = useState<string | null>(null);
  const refresh = () =>
    Promise.all([
      queryClient.invalidateQueries({
        queryKey: ["tag-governance", "deployments"],
      }),
      queryClient.invalidateQueries({
        queryKey: ["tag-governance", "deployment-observations"],
      }),
    ]);
  const createMutation = useMutation({
    mutationFn: (body: CreateTagDeploymentRequest) =>
      createTagDeployment(body),
    onSuccess: (deployment) => {
      setDialog(null);
      setSuccess(`影子部署 #${deployment.id} 已创建`);
      void refresh();
    },
  });
  const approveMutation = useMutation({
    mutationFn: ({ id, revision }: { id: number; revision: number }) =>
      approveTagDeployment(id, revision),
    onSuccess: (deployment) => {
      setSuccess(`部署 #${deployment.id} 已批准进入生产`);
      void refresh();
    },
  });
  const rollbackMutation = useMutation({
    mutationFn: ({
      id,
      reason,
      revision,
    }: {
      id: number;
      reason: string;
      revision: number;
    }) => rollbackTagDeployment(id, reason, revision),
    onSuccess: (deployment) => {
      setDialog(null);
      setSuccess(`部署 #${deployment.id} 已回滚并写入审计`);
      void refresh();
    },
  });
  const resumeMutation = useMutation({
    mutationFn: ({
      id,
      reason,
      revision,
    }: {
      id: number;
      reason: string;
      revision: number;
    }) => resumeTagDeployment(id, reason, revision),
    onSuccess: (deployment) => {
      setDialog(null);
      setSuccess(`部署 #${deployment.id} 已完成漂移复核并恢复自动推进`);
      void refresh();
    },
  });
  // 修订号 CAS 只存在于 approve / rollback；创建部署的 409 是领域冲突
  // （最常见：评估不是密封 Holdout），绝不能套用「修订号过期」的解释。
  const revisionActionError = approveMutation.error ?? rollbackMutation.error;
  const operationError =
    revisionActionError ??
    (dialog?.kind !== "create" ? createMutation.error : null);
  const staleRevision = getErrorStatus(revisionActionError) === 409;
  const refreshStaleDeployment = () => {
    approveMutation.reset();
    rollbackMutation.reset();
    void refresh();
  };
  const refreshStaleResume = () => {
    resumeMutation.reset();
    setDialog(null);
    void refresh();
  };
  const steps = ["影子验证", "5% 灰度", "25% 灰度", "管理员审批", "生产"];
  return (
    <>
      <div className="ag-panel-toolbar">
        <div>
          <strong>受控发布流水线</strong>
          <span>
            影子观察、分阶段灰度、管理员批准与有审计原因的基线回滚。
          </span>
        </div>
        {isAdmin && (
          <button
            type="button"
            onClick={() => {
              createMutation.reset();
              setDialog({ kind: "create" });
            }}
          >
            创建影子部署
          </button>
        )}
      </div>
      {success && (
        <p className="ag-inline-feedback is-success" role="status">
          {success}
        </p>
      )}
      {operationError && (
        <p className="ag-inline-feedback is-error" role="alert">
          {staleRevision
            ? "部署已被其他操作更新，当前修订号已过期。刷新后再执行，避免覆盖并发变更。"
            : deploymentOperationErrorCopy(operationError)}
          {staleRevision && (
            <button type="button" onClick={refreshStaleDeployment}>
              刷新部署状态
            </button>
          )}
        </p>
      )}
      <PanelState
        pending={pending}
        error={error}
        empty={items.length === 0}
        emptyTitle="暂无发布记录"
        emptyDescription="只有通过质量门禁的候选版本才可以创建部署。"
        onRetry={onRetry}
        pendingLabel={GOVERNANCE_PENDING_LABEL}
      >
        <div className="ag-deployment-list">
          {items.map((deployment) => {
            const currentStep = deploymentStep(deployment.status);
            const promotionWaiting = automaticPromotionWaiting(
              deployment.status,
            );
            const terminal =
              deployment.status === "rolled_back" ||
              deployment.status === "retired";
            return (
              <article className="ag-governance-card" key={deployment.id}>
                <header>
                  <div>
                    <span className="ag-card-kicker">
                      DEPLOYMENT #{deployment.id}
                    </span>
                    <h2>抽取版本 #{deployment.tagger_version_id}</h2>
                    <p>
                      评估 #{deployment.evaluation_run_id} · 基线 #
                      {deployment.baseline_tagger_version_id ?? "—"} · 修订 #
                      {deployment.revision}
                    </p>
                  </div>
                  <StatusChip status={deployment.status} />
                </header>
                <div className="ag-release-progress">
                  <div>
                    <span>灰度流量 {deployment.traffic_percent}%</span>
                    <strong>{formatDate(deployment.updated_at)}</strong>
                  </div>
                  <div
                    className="ag-release-progress__bar"
                    role="progressbar"
                    aria-label={`部署 ${deployment.id} 流量`}
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={deployment.traffic_percent}
                  >
                    <span
                      className="ag-release-progress__value"
                      style={{ width: `${deployment.traffic_percent}%` }}
                    />
                  </div>
                </div>
                <ol className="ag-release-steps">
                  {steps.map((step, index) => (
                    <li
                      key={step}
                      className={
                        deployment.status === "rolled_back"
                          ? "is-rollback"
                          : index < currentStep
                            ? "is-done"
                            : index === currentStep
                              ? "is-current"
                              : undefined
                      }
                    >
                      <span className="ag-release-step-icon">
                        {index < currentStep ? (
                          <IconCheck aria-hidden="true" />
                        ) : (
                          index + 1
                        )}
                      </span>
                      <span>{step}</span>
                    </li>
                  ))}
                </ol>
                {promotionWaiting && (
                  <section
                    className={`ag-release-auto-promotion${
                      deployment.promotion_paused ? " is-paused" : ""
                    }`}
                    role={deployment.promotion_paused ? "alert" : "status"}
                    aria-label={`部署 ${deployment.id} 自动晋级状态`}
                  >
                    <strong>可信 Monitor 自动晋级</strong>
                    <span>
                      {deployment.promotion_paused
                        ? `当前晋级已暂停：${
                            deployment.pause_reason ?? "监控门禁触发"
                          }`
                        : promotionWaiting}
                    </span>
                  </section>
                )}
                {deployment.rollback_reason && (
                  <p className="ag-release-rollback-reason">
                    回滚原因：{deployment.rollback_reason}
                  </p>
                )}
                <DeploymentObservations deployment={deployment} />
                {isAdmin && (
                  <footer className="ag-deployment-actions">
                    {canResumeDriftDeployment(deployment) && (
                      <button
                        type="button"
                        className="is-secondary"
                        disabled={resumeMutation.isPending}
                        aria-label={`完成复核并恢复部署 ${deployment.id}`}
                        onClick={() => {
                          resumeMutation.reset();
                          setSuccess(null);
                          setDialog({
                            kind: "resume",
                            deploymentId: deployment.id,
                            revision: deployment.revision,
                          });
                        }}
                      >
                        完成复核并恢复
                      </button>
                    )}
                    {deployment.status === "awaiting_admin" && (
                      <button
                        type="button"
                        disabled={
                          approveMutation.isPending ||
                          deployment.promotion_paused
                        }
                        aria-label={`批准部署 ${deployment.id} 上线`}
                        onClick={() =>
                          approveMutation.mutate({
                            id: deployment.id,
                            revision: deployment.revision,
                          })
                        }
                      >
                        管理员批准上线
                      </button>
                    )}
                    {!terminal && (
                      <button
                        type="button"
                        className="is-danger-ghost"
                        aria-label={`回滚部署 ${deployment.id}`}
                        onClick={() =>
                          setDialog({
                            kind: "rollback",
                            deploymentId: deployment.id,
                            revision: deployment.revision,
                          })
                        }
                      >
                        回滚
                      </button>
                    )}
                  </footer>
                )}
              </article>
            );
          })}
        </div>
      </PanelState>
      {isAdmin && dialog?.kind === "create" && (
        <DeploymentDialog
          taggerVersions={taggerVersions}
          initialEvaluationId={initialEvaluationId}
          serverError={createMutation.error}
          pending={createMutation.isPending}
          onClose={() => {
            createMutation.reset();
            setDialog(null);
          }}
          onCreate={(body) => createMutation.mutate(body)}
        />
      )}
      {isAdmin && dialog?.kind === "rollback" && (
        <RollbackDialog
          deploymentId={dialog.deploymentId}
          pending={rollbackMutation.isPending}
          onClose={() => setDialog(null)}
          onRollback={(reason) =>
            rollbackMutation.mutate({
              id: dialog.deploymentId,
              reason,
              revision: dialog.revision,
            })
          }
        />
      )}
      {isAdmin && dialog?.kind === "resume" && (
        <ResumeDeploymentDialog
          deploymentId={dialog.deploymentId}
          pending={resumeMutation.isPending}
          error={resumeMutation.error}
          onClose={() => {
            resumeMutation.reset();
            setDialog(null);
          }}
          onRefresh={refreshStaleResume}
          onResume={(reason) =>
            resumeMutation.mutate({
              id: dialog.deploymentId,
              reason,
              revision: dialog.revision,
            })
          }
        />
      )}
    </>
  );
}

function AuditsPanel({
  items,
  pending,
  error,
  onRetry,
}: {
  items: TagAuditEvent[];
  pending: boolean;
  error: unknown;
  onRetry: () => void;
}) {
  return (
    <PanelState
      pending={pending}
      error={error}
      empty={items.length === 0}
      emptyTitle="暂无审计事件"
      emptyDescription="体系、版本、评估、部署和人工复核动作会记录在这里。"
      onRetry={onRetry}
      pendingLabel={GOVERNANCE_PENDING_LABEL}
    >
      <ol className="ag-audit-timeline">
        {items.map((event) => (
          <li key={event.id}>
            <span className="ag-audit-event-icon" aria-hidden="true">
              <IconHistory />
            </span>
            <article>
              <header>
                <strong>{event.action}</strong>
                <time dateTime={event.occurred_at ?? event.created_at}>
                  {formatDate(event.occurred_at ?? event.created_at)}
                </time>
              </header>
              <p>
                {event.resource_type} / {event.resource_id}
              </p>
              <footer>
                <span>操作人 #{event.actor_user_id ?? "系统"}</span>
                <code>{JSON.stringify(event.payload)}</code>
              </footer>
            </article>
          </li>
        ))}
      </ol>
    </PanelState>
  );
}

export default function TagGovernancePage() {
  const isAdmin = useAuthStore((state) => state.user?.role === "admin");
  const [searchParams] = useSearchParams();
  const [activeTab, setActiveTab] = useState<TabId>(() => {
    const requestedTab = searchParams.get("tab");
    if (
      requestedTab === "taggers" &&
      searchParams.get("mode") === "optimize"
    ) {
      return "evolution";
    }
    return TABS.some((tab) => tab.id === requestedTab)
      ? (requestedTab as TabId)
      : "taxonomy";
  });
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);
  useEffect(() => {
    const requestedTab = searchParams.get("tab");
    if (
      requestedTab === "taggers" &&
      searchParams.get("mode") === "optimize"
    ) {
      setActiveTab("evolution");
      return;
    }
    if (TABS.some((tab) => tab.id === requestedTab)) {
      setActiveTab(requestedTab as TabId);
    }
  }, [searchParams]);
  const optimizationCohort = useMemo(
    () => parseOptimizationCohort(searchParams.get("cohort")),
    [searchParams],
  );
  // 评估卡片与自进化 CTA 用 deploy_evaluation_id 深链预填部署对话框；
  // 只接受正整数，深链被篡改时静默回落到空表单。
  const deployEvaluationId = useMemo(() => {
    const raw = searchParams.get("deploy_evaluation_id");
    return raw && /^[1-9]\d{0,15}$/.test(raw) ? raw : null;
  }, [searchParams]);

  const schemasQuery = useQuery({
    queryKey: ["tag-governance", "schemas"],
    queryFn: listTagSchemas,
    enabled: activeTab === "taxonomy",
    retry: false,
  });
  const taggersQuery = useQuery({
    queryKey: ["tag-governance", "taggers"],
    queryFn: listTaggerVersions,
    enabled:
      activeTab === "taggers" || (activeTab === "deployments" && isAdmin),
    retry: false,
  });
  const evaluationsQuery = useQuery({
    queryKey: ["tag-governance", "evaluations"],
    queryFn: listTagEvaluations,
    enabled: activeTab === "evaluations",
    retry: false,
    refetchInterval: (query) =>
      query.state.data?.items.some(
        (item) =>
          item.status && !TERMINAL_EVALUATION_STATUSES.has(item.status),
      )
        ? 3_000
        : false,
  });
  const deploymentsQuery = useQuery({
    queryKey: ["tag-governance", "deployments"],
    queryFn: listTagDeployments,
    enabled: activeTab === "deployments",
    retry: false,
    refetchInterval: (query) =>
      query.state.data?.items.some(
        (item) => !TERMINAL_DEPLOYMENT_STATUSES.has(item.status),
      )
        ? 3_000
        : false,
  });
  const auditQuery = useQuery({
    queryKey: ["tag-governance", "audit"],
    queryFn: listTagAuditEvents,
    enabled: activeTab === "audit",
    retry: false,
  });

  const selectTab = (index: number) => {
    const normalizedIndex = (index + TABS.length) % TABS.length;
    setActiveTab(TABS[normalizedIndex].id);
    tabRefs.current[normalizedIndex]?.focus();
  };
  const onTabKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
    index: number,
  ) => {
    const nextIndex =
      event.key === "ArrowRight"
        ? index + 1
        : event.key === "ArrowLeft"
          ? index - 1
          : event.key === "Home"
            ? 0
            : event.key === "End"
              ? TABS.length - 1
              : null;
    if (nextIndex === null) return;
    event.preventDefault();
    selectTab(nextIndex);
  };

  return (
    <main className="ag-governance-page">
      <header className="ag-governance-hero">
        <div>
          <span className="ag-eyebrow">LABEL GOVERNANCE · CLOSED LOOP</span>
          <h1>标签治理中心</h1>
          <p>
            从标签定义、抽取版本、金标评估到灰度发布和审计，把洞察变成可验证、可追溯的持续优化闭环。
          </p>
        </div>
        <div className="ag-governance-hero__actions">
          <Link to="/tag-review">进入人工复核</Link>
          <Link to="/tag-insights" className="is-secondary">
            查看标签洞察
          </Link>
        </div>
      </header>

      <nav
        className="ag-governance-tabs"
        role="tablist"
        aria-label="标签治理视图"
      >
        {TABS.map((tab, index) => (
          <button
            type="button"
            role="tab"
            key={tab.id}
            id={`tag-governance-tab-${tab.id}`}
            ref={(element) => {
              tabRefs.current[index] = element;
            }}
            aria-selected={activeTab === tab.id}
            aria-label={tab.label}
            aria-controls={`tag-governance-panel-${tab.id}`}
            tabIndex={activeTab === tab.id ? 0 : -1}
            className={activeTab === tab.id ? "is-active" : undefined}
            onClick={() => setActiveTab(tab.id)}
            onKeyDown={(event) => onTabKeyDown(event, index)}
          >
            <strong>{tab.label}</strong>
            <span>{tab.description}</span>
          </button>
        ))}
      </nav>

      <section
        className="ag-governance-panel"
        role="tabpanel"
        id={`tag-governance-panel-${activeTab}`}
        aria-labelledby={`tag-governance-tab-${activeTab}`}
        tabIndex={0}
      >
        {activeTab === "taxonomy" && (
          <TaxonomyPanel
            items={schemasQuery.data?.items ?? []}
            pending={schemasQuery.isPending}
            error={schemasQuery.error}
            onRetry={() => void schemasQuery.refetch()}
            isAdmin={isAdmin}
          />
        )}
        {activeTab === "taggers" && (
          <TaggersPanel
            items={taggersQuery.data?.items ?? []}
            pending={taggersQuery.isPending}
            error={taggersQuery.error}
            onRetry={() => void taggersQuery.refetch()}
            isAdmin={isAdmin}
          />
        )}
        {activeTab === "evaluations" && (
          <EvaluationsPanel
            items={evaluationsQuery.data?.items ?? []}
            pending={evaluationsQuery.isPending}
            error={evaluationsQuery.error}
            onRetry={() => void evaluationsQuery.refetch()}
          />
        )}
        {activeTab === "deployments" && (
          <DeploymentsPanel
            items={deploymentsQuery.data?.items ?? []}
            taggerVersions={taggersQuery.data?.items ?? []}
            pending={deploymentsQuery.isPending}
            error={deploymentsQuery.error}
            onRetry={() => void deploymentsQuery.refetch()}
            isAdmin={isAdmin}
            initialEvaluationId={deployEvaluationId}
          />
        )}
        {activeTab === "evolution" && (
          <EvolutionPanel
            isAdmin={isAdmin}
            initialDialog={
              searchParams.get("mode") === "optimize" ? "optimize" : null
            }
            initialCohort={optimizationCohort}
          />
        )}
        {activeTab === "audit" && (
          <AuditsPanel
            items={auditQuery.data?.items ?? []}
            pending={auditQuery.isPending}
            error={auditQuery.error}
            onRetry={() => void auditQuery.refetch()}
          />
        )}
      </section>
    </main>
  );
}

import {
  IconBranch,
  IconClose,
  IconRefresh,
} from "@arco-design/web-react/icon";
import type { TagFactLineageResponse } from "@/types/api";

interface TagFactLineageDrawerProps {
  factId: number;
  data?: TagFactLineageResponse;
  pending: boolean;
  error: unknown;
  onRetry: () => void;
  onClose: () => void;
}

function valueOrRedacted(value: unknown): string {
  if (typeof value === "string" && value) return value;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return "—（未提供或已按角色脱敏）";
}

export function TagFactLineageDrawer({
  factId,
  data,
  pending,
  error,
  onRetry,
  onClose,
}: TagFactLineageDrawerProps) {
  const evidence = data?.fact.evidence_refs ?? [];
  return (
    <aside
      className="ag-tag-lineage-drawer"
      role="dialog"
      aria-modal="false"
      aria-labelledby="tag-lineage-title"
    >
      <header>
        <div>
          <span className="ag-eyebrow">FACT LINEAGE</span>
          <h2 id="tag-lineage-title">标签事实 #{factId} 溯源</h2>
        </div>
        <button type="button" aria-label="关闭标签溯源" onClick={onClose}>
          <IconClose />
        </button>
      </header>

      {pending && (
        <div className="ag-tag-lineage-state" role="status">
          正在加载事实链路…
        </div>
      )}
      {Boolean(error) && (
        <div className="ag-tag-lineage-state is-error" role="alert">
          <strong>标签溯源加载失败</strong>
          <span>{error instanceof Error ? error.message : "接口暂不可用"}</span>
          <button type="button" onClick={onRetry}>
            <IconRefresh aria-hidden="true" />
            重新加载
          </button>
        </div>
      )}
      {!pending && !error && data && (
        <div className="ag-tag-lineage-content">
          <div className="ag-tag-lineage-summary">
            <IconBranch aria-hidden="true" />
            <span>
              <strong>{data.fact.tag_key}</strong>
              <small>
                {data.fact.source} · {data.is_current ? "当前事实" : "历史事实"}
              </small>
            </span>
          </div>
          <dl>
            <div>
              <dt>Input Hash</dt>
              <dd>{valueOrRedacted(data.fact.input_hash)}</dd>
            </div>
            <div>
              <dt>Schema</dt>
              <dd>
                {data.schema_version
                  ? `${data.schema_version.version} (#${data.schema_version.id})`
                  : valueOrRedacted(null)}
              </dd>
            </div>
            <div>
              <dt>Tagger</dt>
              <dd>
                {data.tagger_version
                  ? `${data.tagger_version.version} / ${data.tagger_version.engine}`
                  : valueOrRedacted(null)}
              </dd>
            </div>
            <div>
              <dt>模型</dt>
              <dd>{valueOrRedacted(data.model_version)}</dd>
            </div>
            <div>
              <dt>Job / Run</dt>
              <dd>
                Job {valueOrRedacted(data.job?.id)} / Run{" "}
                {valueOrRedacted(data.extraction_run?.id)}
              </dd>
            </div>
            <div>
              <dt>Deployment</dt>
              <dd>
                {data.deployment
                  ? `#${data.deployment.id} · ${data.deployment.status}`
                  : valueOrRedacted(null)}
              </dd>
            </div>
          </dl>
          <section aria-labelledby="tag-lineage-evidence-title">
            <h3 id="tag-lineage-evidence-title">证据链 · {evidence.length}</h3>
            {evidence.length === 0 ? (
              <p>该事实未返回可见证据。</p>
            ) : (
              <ul>
                {evidence.map((item, index) => (
                  <li key={`${String(item.ref_id ?? "evidence")}-${index}`}>
                    <strong>{String(item.ref_id ?? `证据 ${index + 1}`)}</strong>
                    <span>
                      录音 {String(item.recording_id ?? "—")} ·{" "}
                      {String(item.start_sec ?? "—")}s
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>
      )}
    </aside>
  );
}

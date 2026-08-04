import { Link } from "react-router-dom";

import { PanelState } from "@/components/PanelState";
import { compactCount } from "@/components/governance/format";
import type { PromptLabDomainCoverage, PromptLabReadiness } from "@/types/api";

/**
 * 阻塞码 → 中文文案与行动入口。
 *
 * 后端返回的是机器码，直接展示等于把内部实现丢给用户。未知码仍要原样显示——
 * 运维需要看见后端新增了什么阻塞原因，而不是被一句「未知问题」挡住。
 */
const BLOCKER_COPY: Readonly<
  Record<string, { text: string; to: string; action: string }>
> = {
  reviewed_feedback_below_200: {
    text: "已复核反馈不足 200 条，先在人工复核工作台清理队列。",
    to: "/tag-review",
    action: "进入人工复核",
  },
  no_reviewed_domains: {
    text: "还没有任何已复核的标签组合，编译器无从归纳规则。",
    to: "/tag-review",
    action: "进入人工复核",
  },
  no_frozen_gold_set: {
    text: "没有冻结的金标集版本，编译结果将无法被回放验证。",
    to: "/tag-governance?tab=evaluations",
    action: "前往冻结金标集",
  },
};

const DOMAIN_PREFIX = "domain_support_below_30:";

function blockerEntry(code: string): { text: string; to: string; action: string } {
  const known = BLOCKER_COPY[code];
  if (known) return known;
  if (code.startsWith(DOMAIN_PREFIX)) {
    const domain = code.slice(DOMAIN_PREFIX.length);
    return {
      text: `组合 ${domain} 的已复核样本不足，该标签的判定规则缺乏依据。`,
      to: "/tag-insights",
      action: "查看标签洞察",
    };
  }
  return { text: code, to: "/tag-review", action: "进入人工复核" };
}

function splitDomain(domain: string): { subjectType: string; tagKey: string } {
  const at = domain.indexOf(":");
  // tag_key 可能含点号但不含冒号，所以只切第一个冒号。
  return at === -1
    ? { subjectType: domain, tagKey: "—" }
    : { subjectType: domain.slice(0, at), tagKey: domain.slice(at + 1) };
}

type CoverageTone = "cold" | "warm" | "ready" | "empty";

function coverageTone(
  coverage: PromptLabDomainCoverage | undefined,
  threshold: number,
): CoverageTone {
  if (!coverage) return "empty";
  if (coverage.feedback_count < threshold) return "cold";
  // 刚过线的组合还很脆弱，用中间色提示它离稳还差一截。
  if (coverage.feedback_count < threshold * 2) return "warm";
  return "ready";
}

function CoverageMatrix({ data }: { data: PromptLabReadiness }) {
  const byDomain = new Map(data.domains.map((item) => [item.domain, item]));
  const parsed = data.domains.map((item) => ({
    ...splitDomain(item.domain),
    coverage: item,
  }));
  const subjectTypes = [...new Set(parsed.map((item) => item.subjectType))].sort();
  const tagKeys = [...new Set(parsed.map((item) => item.tagKey))].sort();

  if (subjectTypes.length === 0) {
    return (
      <p className="ag-compact-state">
        尚无任何已复核组合。先在人工复核工作台处理若干任务，这里会显示每个
        标签在各主体类型上的样本量。
      </p>
    );
  }

  return (
    <div className="ag-plab-matrix-wrap">
      <div
        className="ag-plab-matrix"
        role="table"
        aria-label="已复核样本覆盖矩阵"
        style={{ ["--ag-plab-cols" as string]: String(tagKeys.length) }}
      >
        <div role="row" className="ag-plab-matrix__row">
          <span role="columnheader" className="ag-plab-matrix__corner">
            主体 \ 标签
          </span>
          {tagKeys.map((tagKey) => (
            <span role="columnheader" key={tagKey} className="ag-plab-matrix__head">
              {tagKey}
            </span>
          ))}
        </div>
        {subjectTypes.map((subjectType) => (
          <div role="row" className="ag-plab-matrix__row" key={subjectType}>
            <span role="rowheader" className="ag-plab-matrix__head">
              {subjectType}
            </span>
            {tagKeys.map((tagKey) => {
              const coverage = byDomain.get(`${subjectType}:${tagKey}`);
              const tone = coverageTone(coverage, data.domain_threshold);
              const gap = coverage
                ? Math.max(0, data.domain_threshold - coverage.feedback_count)
                : data.domain_threshold;
              const label = coverage
                ? `${subjectType} / ${tagKey}：金标 ${coverage.feedback_count}，银标 ${coverage.silver_count}，${
                    gap > 0 ? `距门槛还差 ${gap} 条` : "已达门槛"
                  }`
                : `${subjectType} / ${tagKey}：暂无已复核样本，距门槛还差 ${gap} 条`;
              return (
                <span
                  role="cell"
                  key={tagKey}
                  className={`ag-plab-matrix__cell is-${tone}`}
                  aria-label={label}
                >
                  {coverage ? compactCount(coverage.feedback_count) : "—"}
                </span>
              );
            })}
          </div>
        ))}
      </div>
      <ul className="ag-plab-matrix__legend">
        <li>
          <span className="ag-plab-matrix__swatch is-cold" aria-hidden="true" />
          低于门槛（&lt; {data.domain_threshold}）
        </li>
        <li>
          <span className="ag-plab-matrix__swatch is-warm" aria-hidden="true" />
          刚过门槛
        </li>
        <li>
          <span className="ag-plab-matrix__swatch is-ready" aria-hidden="true" />
          样本充足（≥ {data.domain_threshold * 2}）
        </li>
      </ul>
    </div>
  );
}

function Threshold({
  label,
  current,
  target,
  unit,
}: {
  label: string;
  current: number;
  target: number;
  unit: string;
}) {
  const met = current >= target;
  return (
    <div className="ag-plab-threshold">
      <span className="ag-plab-threshold__head">
        <strong>{label}</strong>
        <span className={`ag-gate-badge ${met ? "is-pass" : "is-fail"}`} role="status">
          {met ? "已达标" : "未达标"}
        </span>
      </span>
      <progress
        max={target}
        value={Math.min(current, target)}
        aria-label={`${label}：${current} / ${target} ${unit}`}
      />
      <span className="ag-plab-threshold__value">
        {compactCount(current)} / {compactCount(target)} {unit}
      </span>
    </div>
  );
}

export function ReadinessPanel({
  data,
  pending,
  error,
  onRetry,
}: {
  data: PromptLabReadiness | undefined;
  pending: boolean;
  error: unknown;
  onRetry: () => void;
}) {
  return (
    <PanelState
      pending={pending}
      error={error}
      empty={!data}
      emptyTitle="暂无就绪度数据"
      emptyDescription="完成一次人工复核后，这里会显示编译前置条件。"
      onRetry={onRetry}
      pendingLabel="正在检查编译前置条件…"
    >
      {data && (
        <div className="ag-plab-readiness">
          <section className="ag-governance-card">
            <header>
              <div>
                <span className="ag-card-kicker">COMPILE PRECONDITIONS</span>
                <h2>编译前置条件</h2>
              </div>
              <span
                className={`ag-gate-badge ${data.ready ? "is-pass" : "is-fail"}`}
                role="status"
                aria-label={data.ready ? "前置条件已满足" : "前置条件未满足"}
              >
                {data.ready ? "可以编译" : "尚不可编译"}
              </span>
            </header>
            <div className="ag-plab-threshold-grid">
              <Threshold
                label="已复核反馈"
                current={data.feedback_total}
                target={data.feedback_threshold}
                unit="条"
              />
              <Threshold
                label="冻结金标集版本"
                current={data.frozen_gold_set_versions}
                target={1}
                unit="版"
              />
            </div>
          </section>

          <section className="ag-governance-card">
            <header>
              <div>
                <span className="ag-card-kicker">COVERAGE</span>
                <h2>样本覆盖</h2>
                <p>
                  只有人工金标计入门槛。银标（机器伪标）在这里可见，是为了让缺口
                  清晰——它们供聚类与不确定度排序使用，不能替代人工判断。
                </p>
              </div>
            </header>
            <CoverageMatrix data={data} />
          </section>

          {data.blockers.length > 0 && (
            <section className="ag-governance-card ag-plab-coldstart">
              <header>
                <div>
                  <span className="ag-card-kicker">COLD START</span>
                  <h2>还差什么</h2>
                </div>
              </header>
              <ol className="ag-plab-blocker-list">
                {data.blockers.map((code) => {
                  const entry = blockerEntry(code);
                  return (
                    <li key={code}>
                      <span>{entry.text}</span>
                      <Link to={entry.to}>{entry.action}</Link>
                    </li>
                  );
                })}
              </ol>
              {data.annotation_hours_remaining > 0 ? (
                <p
                  className="ag-plab-coldstart__cost"
                  title="按各组合距门槛的缺口 × 每条 5 分钟估算，仅供排期参考。"
                >
                  补齐所有未达标组合还需约{" "}
                  <strong>{data.annotation_hours_remaining} 小时</strong>{" "}
                  人工标注（按每条 5 分钟估算）。
                </p>
              ) : (
                <p className="ag-plab-coldstart__cost">所有组合的样本量均已达标。</p>
              )}
            </section>
          )}
        </div>
      )}
    </PanelState>
  );
}

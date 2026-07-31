import { useMemo } from "react";
import { Link } from "react-router-dom";
import { formatPercent } from "@/components/dialogue/format";
import type { AnalyzeTagInsightsResponse } from "@/types/api";

interface InsightVisualsProps {
  result: AnalyzeTagInsightsResponse;
}

const SERIES_COLORS = [
  "#165dff",
  "#00b42a",
  "#ff7d00",
  "#722ed1",
  "#f53f3f",
  "#14c9c9",
  "#eb0aa4",
  "#86909c",
];

function EmptyChart({ children }: { children: string }) {
  return <p className="ag-chart-empty">{children}</p>;
}

function DistributionChart({ result }: InsightVisualsProps) {
  const values = result.distributions.slice(0, 12);
  const max = Math.max(...values.map((item) => item.count), 1);
  if (values.length === 0) {
    return <EmptyChart>暂无标签分布数据。</EmptyChart>;
  }
  return (
    <div className="ag-bar-chart">
      {values.map((item, index) => (
        <div
          key={`${item.group_key}-${item.label_key}-${item.value}`}
          className="ag-bar-chart__row"
        >
          <span title={`${item.group_key} / ${item.label_key}`}>
            {item.label_key}
            <small>{item.group_key}</small>
          </span>
          <i>
            <b
              style={{
                width: `${(item.count / max) * 100}%`,
                background: SERIES_COLORS[index % SERIES_COLORS.length],
              }}
            />
          </i>
          <strong>
            {item.value} · {item.count}
          </strong>
        </div>
      ))}
    </div>
  );
}

function TrendChart({ result }: InsightVisualsProps) {
  const buckets = [
    ...new Set(result.trends.map((item) => item.bucket_key)),
  ].sort();
  const seriesByKey = new Map<
    string,
    {
      key: string;
      label: string;
    }
  >();
  const countBySeriesAndBucket = new Map<string, number>();
  result.trends.forEach((item) => {
    const key = JSON.stringify([
      item.group_key,
      item.label_key,
      item.value,
    ]);
    if (!seriesByKey.has(key)) {
      seriesByKey.set(key, {
        key,
        label: `${item.group_key} · ${item.label_key}=${item.value}`,
      });
    }
    const bucketKey = `${key}\u0000${item.bucket_key}`;
    countBySeriesAndBucket.set(
      bucketKey,
      (countBySeriesAndBucket.get(bucketKey) ?? 0) + item.count,
    );
  });
  const series = [...seriesByKey.values()].slice(0, 6);
  const max = Math.max(...countBySeriesAndBucket.values(), 1);
  if (buckets.length === 0) return <EmptyChart>暂无趋势时间桶。</EmptyChart>;

  const xStart = 48;
  const xEnd = 480;
  const yTop = 22;
  const yBottom = 148;
  const tickStep = max <= 3 ? 1 : Math.ceil(max / 3);
  const ticks =
    max <= 3
      ? Array.from({ length: max + 1 }, (_, index) => index)
      : [0, tickStep, tickStep * 2, tickStep * 3];
  const chartMax = ticks.at(-1) ?? max;
  const points = series.map((seriesItem) => {
    return buckets.map((bucket, index) => {
      const count =
        countBySeriesAndBucket.get(`${seriesItem.key}\u0000${bucket}`) ?? 0;
      const x =
        buckets.length === 1
          ? (xStart + xEnd) / 2
          : xStart + (index * (xEnd - xStart)) / (buckets.length - 1);
      const y = yBottom - (count / chartMax) * (yBottom - yTop);
      return { bucket, count, x, y };
    });
  });
  const bucketTickIndexes = new Set<number>();
  if (buckets.length <= 6) {
    buckets.forEach((_, index) => bucketTickIndexes.add(index));
  } else {
    const step = Math.ceil((buckets.length - 1) / 5);
    for (let index = 0; index < buckets.length; index += step) {
      bucketTickIndexes.add(index);
    }
    bucketTickIndexes.add(buckets.length - 1);
  }

  return (
    <div className="ag-line-chart">
      <svg viewBox="0 0 500 192" role="group" aria-label="标签趋势折线图">
        <text className="ag-line-chart__axis-title" x={xStart} y="12">
          事件数
        </text>
        {ticks.map((tick) => {
          const y = yBottom - (tick / chartMax) * (yBottom - yTop);
          return (
            <g key={tick} className="ag-line-chart__tick">
              <line x1={xStart} x2={xEnd} y1={y} y2={y} />
              <text x={xStart - 8} y={y + 3} textAnchor="end">
                {tick}
              </text>
            </g>
          );
        })}
        {points.map((linePoints, index) => {
          const seriesItem = series[index];
          const color = SERIES_COLORS[index % SERIES_COLORS.length];
          return (
            <g key={seriesItem.key} className="ag-line-chart__series">
              <polyline
                points={linePoints
                  .map((point) => `${point.x},${point.y}`)
                  .join(" ")}
                style={{ stroke: color }}
              />
              {linePoints.map((point) => {
                const accessibleLabel = `${seriesItem.label}，${point.bucket}：${point.count}`;
                return (
                  <circle
                    key={point.bucket}
                    cx={point.x}
                    cy={point.y}
                    r="3.5"
                    fill={color}
                    role="img"
                    tabIndex={0}
                    aria-label={accessibleLabel}
                  >
                    <title>
                      {seriesItem.label}｜{point.bucket}｜{point.count} 次
                    </title>
                  </circle>
                );
              })}
            </g>
          );
        })}
        {buckets.map((bucket, index) => {
          if (!bucketTickIndexes.has(index)) return null;
          const x =
            buckets.length === 1
              ? (xStart + xEnd) / 2
              : xStart + (index * (xEnd - xStart)) / (buckets.length - 1);
          return (
            <text key={bucket} x={x} y="174" textAnchor="middle">
              {bucket.slice(-10)}
            </text>
          );
        })}
      </svg>
      <div className="ag-chart-legend">
        {series.map((seriesItem, index) => (
          <span key={seriesItem.key} title={seriesItem.label}>
            <i
              style={{
                background: SERIES_COLORS[index % SERIES_COLORS.length],
              }}
            />
            {seriesItem.label}
          </span>
        ))}
      </div>
    </div>
  );
}

function StageEventVolume({ result }: InsightVisualsProps) {
  const stages = useMemo(() => {
    const counts = new Map<string, number>();
    result.distributions
      .filter(
        (item) =>
          item.label_key.toLowerCase().includes("stage") ||
          item.label_key.includes("阶段"),
      )
      .forEach((item) => {
        const label = item.label_key.startsWith("stage.")
          ? item.label_key.slice(6)
          : item.value;
        counts.set(label, (counts.get(label) ?? 0) + item.count);
      });
    return [...counts.entries()].sort((left, right) => right[1] - left[1]);
  }, [result.distributions]);
  const max = Math.max(...stages.map(([, count]) => count), 1);
  if (stages.length === 0) {
    return <EmptyChart>标签键中没有可识别的业务阶段数据。</EmptyChart>;
  }
  return (
    <ol className="ag-stage-volume" aria-label="阶段标签事件量排名">
      {stages.slice(0, 10).map(([stage, count], index) => (
        <li key={stage}>
          <span>{index + 1}</span>
          <div>
            <header>
              <strong>{stage}</strong>
              <b>{count} 次</b>
            </header>
            <i aria-hidden="true">
              <b style={{ width: `${(count / max) * 100}%` }} />
            </i>
          </div>
        </li>
      ))}
    </ol>
  );
}

function ConfidenceChart({ result }: InsightVisualsProps) {
  if (result.confidence.length === 0) {
    return <EmptyChart>暂无置信度桶数据。</EmptyChart>;
  }
  const max = Math.max(...result.confidence.map((item) => item.count), 1);
  return (
    <div className="ag-confidence-chart">
      {result.confidence.map((item, index) => (
        <div key={`${item.group_key}-${item.bucket}`}>
          <span>{item.bucket}</span>
          <i>
            <b
              style={{
                height: `${Math.max(4, (item.count / max) * 100)}%`,
                background: SERIES_COLORS[index % SERIES_COLORS.length],
              }}
            />
          </i>
          <strong>{item.count}</strong>
          <small>
            {item.group_key} · 均值 {formatPercent(item.average_confidence)}
          </small>
        </div>
      ))}
    </div>
  );
}

function CoOccurrenceList({ result }: InsightVisualsProps) {
  if (result.co_occurrences.length === 0) {
    return <EmptyChart>当前选择没有标签共现关系。</EmptyChart>;
  }
  const max = Math.max(...result.co_occurrences.map((item) => item.count), 1);
  return (
    <ol className="ag-cooccurrence-list">
      {result.co_occurrences.slice(0, 16).map((item) => (
        <li
          key={`${item.group_key}-${item.left_label}-${item.right_label}`}
        >
          <span>{item.left_label}</span>
          <b>↔</b>
          <span>{item.right_label}</span>
          <i>
            <b style={{ width: `${(item.count / max) * 100}%` }} />
          </i>
          <strong>{item.count}</strong>
          <small>{item.group_key}</small>
        </li>
      ))}
    </ol>
  );
}

function PairwiseCards({ result }: InsightVisualsProps) {
  if (result.pairwise.length === 0) {
    return <EmptyChart>至少选择两个标签组才能生成两两对比。</EmptyChart>;
  }
  return (
    <div className="ag-pairwise-grid">
      {result.pairwise.map((item) => (
        <article key={`${item.left_group_key}-${item.right_group_key}`}>
          <header>
            <strong>{item.left_group_key}</strong>
            <span>vs</span>
            <strong>{item.right_group_key}</strong>
          </header>
          <div>
            <span>
              一致率 <b>{formatPercent(item.agreement_rate)}</b>
            </span>
            <span>
              重叠率 <b>{formatPercent(item.overlap_rate)}</b>
            </span>
            <span>
              分歧 <b>{item.differences}</b>
            </span>
          </div>
          <footer>
            仅左 {item.left_only_cells} · 仅右 {item.right_only_cells}
          </footer>
        </article>
      ))}
    </div>
  );
}

function compactPercent(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "—";
  return `${new Intl.NumberFormat("zh-CN", {
    maximumFractionDigits: 1,
  }).format(value * 100)}%`;
}

function average(values: Array<number | null>): number | null {
  const finiteValues = values.filter(
    (value): value is number => value !== null && Number.isFinite(value),
  );
  if (finiteValues.length === 0) return null;
  return (
    finiteValues.reduce((total, value) => total + value, 0) /
    finiteValues.length
  );
}

function QualityLoopCard({ result }: InsightVisualsProps) {
  const coverage =
    average(result.coverage.map((item) => item.coverage_rate)) ??
    (result.overview.total_cells > 0
      ? result.overview.complete_cells / result.overview.total_cells
      : null);
  const agreement = average(
    result.pairwise.map((item) => item.agreement_rate),
  );
  const conflict = result.overview.conflict_rate;
  const evidenceDensity =
    result.overview.assignment_count > 0
      ? Math.min(
          1,
          result.output_budget.evidence_ref_count /
            result.overview.assignment_count,
        )
      : null;
  const signals = [
    { label: "平均覆盖率", value: coverage, tone: "blue" },
    { label: "平均一致率", value: agreement, tone: "green" },
    { label: "冲突率", value: conflict, tone: "orange" },
    { label: "证据密度", value: evidenceDensity, tone: "purple" },
  ] as const;

  return (
    <div className="ag-quality-loop">
      <div className="ag-quality-loop__metrics">
        {signals.map((signal) => (
          <div key={signal.label}>
            <span>
              <strong>{signal.label}</strong>
              <b>{compactPercent(signal.value)}</b>
            </span>
            <i aria-hidden="true">
              <b
                className={`is-${signal.tone}`}
                style={{ width: `${Math.max(0, signal.value ?? 0) * 100}%` }}
              />
            </i>
          </div>
        ))}
      </div>
      <dl className="ag-quality-loop__issues">
        <div>
          <dt>待复核冲突</dt>
          <dd>{result.overview.conflict_cells}</dd>
        </div>
        <div>
          <dt>缺失单元</dt>
          <dd>{result.overview.incomplete_cells}</dd>
        </div>
        <div>
          <dt>版本差异</dt>
          <dd>
            {result.pairwise.reduce(
              (total, item) => total + item.differences,
              0,
            )}
          </dd>
        </div>
      </dl>
      <footer>
        <span>将异常送入人工复核、评估和灰度发布链路。</span>
        <Link to="/tag-governance">进入标签治理中心</Link>
      </footer>
    </div>
  );
}

function DimensionTable({ result }: InsightVisualsProps) {
  if (result.dimension_comparisons.length === 0) {
    return <EmptyChart>标签快照未提供门店或人员维度。</EmptyChart>;
  }
  return (
    <div className="ag-dimension-table-wrap">
      <table className="ag-dimension-table">
        <thead>
          <tr>
            <th>维度</th>
            <th>对象</th>
            <th>标签组</th>
            <th>覆盖率</th>
            <th>平均置信度</th>
            <th>冲突率</th>
            <th>接待数</th>
          </tr>
        </thead>
        <tbody>
          {result.dimension_comparisons.map((item) => (
            <tr
              key={`${item.dimension}-${item.dimension_value}-${item.group_key}`}
            >
              <td>{item.dimension === "store" ? "门店" : "人员"}</td>
              <td>{item.dimension_value}</td>
              <td>{item.group_key}</td>
              <td>{formatPercent(item.coverage_rate)}</td>
              <td>{formatPercent(item.average_confidence)}</td>
              <td>{formatPercent(item.conflict_rate)}</td>
              <td>{item.unique_targets}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function InsightVisuals({ result }: InsightVisualsProps) {
  return (
    <>
      <section className="ag-insight-grid" aria-label="标签洞察图表">
        <article className="ag-insight-card ag-insight-card--wide">
          <header>
            <div>
              <h2>分布</h2>
              <p>各标签组的标签值与占比</p>
            </div>
          </header>
          <DistributionChart result={result} />
        </article>
        <article className="ag-insight-card ag-insight-card--wide">
          <header>
            <div>
              <h2>趋势</h2>
              <p>按选择的时间粒度观察标签变化</p>
            </div>
          </header>
          <TrendChart result={result} />
        </article>
        <article className="ag-insight-card">
          <header>
            <div>
              <h2>阶段事件量</h2>
              <p>按已发生标签事件量降序；展示频次，不代表阶段到达率或转化率</p>
            </div>
          </header>
          <StageEventVolume result={result} />
        </article>
        <article className="ag-insight-card">
          <header>
            <div>
              <h2>置信度校准</h2>
              <p>分桶数量及实际平均置信度</p>
            </div>
          </header>
          <ConfidenceChart result={result} />
        </article>
        <article className="ag-insight-card">
          <header>
            <div>
              <h2>标签共现</h2>
              <p>同一对话时间窗中的标签组合</p>
            </div>
          </header>
          <CoOccurrenceList result={result} />
        </article>
        <article className="ag-insight-card">
          <header>
            <div>
              <h2>标签组对比</h2>
              <p>一致、重叠与非对称覆盖</p>
            </div>
          </header>
          <PairwiseCards result={result} />
        </article>
        <article className="ag-insight-card ag-insight-card--wide">
          <header>
            <div>
              <h2>质量闭环</h2>
              <p>把覆盖、一致、冲突与证据完整度转成治理行动</p>
            </div>
          </header>
          <QualityLoopCard result={result} />
        </article>
      </section>

      <section className="ag-insight-section" aria-labelledby="dimension-title">
        <div className="ag-insight-section__heading">
          <div>
            <h2 id="dimension-title">门店 / 人员对比</h2>
            <p>比较标签覆盖、置信度、冲突与独立接待数。</p>
          </div>
        </div>
        <DimensionTable result={result} />
      </section>
    </>
  );
}

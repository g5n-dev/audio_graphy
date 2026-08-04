import { compactPercent } from "./format";
import "./metric.css";

/**
 * 带进度条的比率指标。
 *
 * `inverse` 用于「越低越好」的指标（错误率、复核率）：条长仍按原值，但配色按
 * `1 - value` 判定，所以低错误率显示为健康色。
 *
 * 缺失值的条长为零、数值显示为破折号——两者刻意不同，读者能区分「测到了 0」
 * 和「没测到」。
 */
export function Metric({
  label,
  value,
  inverse = false,
}: {
  label: string;
  value: number | undefined;
  inverse?: boolean;
}) {
  const safeValue = Math.min(Math.max(value ?? 0, 0), 1);
  const tone = inverse ? 1 - safeValue : safeValue;
  return (
    <div className="ag-quality-metric">
      <span>
        <strong>{label}</strong>
        <b>{compactPercent(value)}</b>
      </span>
      <span className="ag-quality-metric__track" aria-hidden="true">
        <span
          style={{
            width: `${safeValue * 100}%`,
            background:
              tone >= 0.9 ? "#00a870" : tone >= 0.75 ? "#2f6bff" : "#e5a100",
          }}
        />
      </span>
    </div>
  );
}

export function formatSeconds(value: number): string {
  if (!Number.isFinite(value) || value < 0) return "0.0";
  return value.toFixed(value >= 60 ? 0 : 1);
}

export function formatClock(value: number): string {
  const safeValue = Number.isFinite(value) && value > 0 ? value : 0;
  const minutes = Math.floor(safeValue / 60);
  const seconds = Math.floor(safeValue % 60);
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

export function formatPercent(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(1)}%`;
}

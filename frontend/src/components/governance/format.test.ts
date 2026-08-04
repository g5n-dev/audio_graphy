import { describe, expect, it } from "vitest";

import {
  compactCount,
  compactPercent,
  displayValue,
  failureStageLabel,
  formatDate,
  numericMetric,
  signedCount,
  signedPercent,
} from "./format";

describe("compactPercent", () => {
  it("renders a ratio as a percentage with one decimal", () => {
    expect(compactPercent(0.8123)).toBe("81.2%");
    expect(compactPercent(1)).toBe("100%");
  });

  it("shows a dash for missing values so they cannot be read as zero", () => {
    expect(compactPercent(null)).toBe("—");
    expect(compactPercent(undefined)).toBe("—");
    expect(compactPercent(Number.NaN)).toBe("—");
    expect(compactPercent(Number.POSITIVE_INFINITY)).toBe("—");
  });
});

describe("compactCount", () => {
  it("groups thousands", () => {
    expect(compactCount(1_234_567)).toBe("1,234,567");
    expect(compactCount(0)).toBe("0");
  });

  it("shows a dash for missing values", () => {
    expect(compactCount(null)).toBe("—");
    expect(compactCount(Number.NaN)).toBe("—");
  });
});

describe("signedPercent", () => {
  it("marks improvements with a leading plus", () => {
    expect(signedPercent(0.021)).toBe("+2.1%");
  });

  it("keeps the minus sign for regressions", () => {
    expect(signedPercent(-0.008)).toBe("-0.8%");
  });

  it("treats a measured zero as a signed value, not as missing", () => {
    expect(signedPercent(0)).toBe("+0%");
    expect(signedPercent(null)).toBe("—");
  });
});

describe("signedCount", () => {
  it("marks growth with a leading plus and groups thousands", () => {
    expect(signedCount(1_996)).toBe("+1,996");
  });

  it("keeps the minus sign for reductions", () => {
    expect(signedCount(-42)).toBe("-42");
  });

  it("shows a dash for missing values", () => {
    expect(signedCount(undefined)).toBe("—");
    expect(signedCount(Number.NaN)).toBe("—");
  });
});

describe("numericMetric", () => {
  it("passes finite numbers through", () => {
    expect(numericMetric(0.5)).toBe(0.5);
    expect(numericMetric(0)).toBe(0);
  });

  it("rejects anything that is not a finite number", () => {
    expect(numericMetric("0.5")).toBeNull();
    expect(numericMetric(Number.NaN)).toBeNull();
    expect(numericMetric(null)).toBeNull();
    expect(numericMetric({})).toBeNull();
  });
});

describe("formatDate", () => {
  it("formats an ISO timestamp in the local locale", () => {
    expect(formatDate("2026-08-03T10:30:00Z")).toContain("2026");
  });

  it("returns an unparseable value verbatim rather than showing Invalid Date", () => {
    expect(formatDate("not-a-date")).toBe("not-a-date");
  });

  it("shows a dash for an absent value", () => {
    expect(formatDate(null)).toBe("—");
    expect(formatDate("")).toBe("—");
  });
});

describe("displayValue", () => {
  it("passes strings through untouched", () => {
    expect(displayValue("已完成")).toBe("已完成");
  });

  it("serializes structured values so unknown shapes stay legible", () => {
    expect(displayValue({ support: 6 })).toBe('{"support":6}');
    expect(displayValue([1, 2])).toBe("[1,2]");
    expect(displayValue(42)).toBe("42");
  });

  it("falls back to String() when a value cannot be serialized", () => {
    const cyclic: Record<string, unknown> = {};
    cyclic.self = cyclic;
    expect(displayValue(cyclic)).toBe("[object Object]");
  });

  it("shows a dash for absent values", () => {
    expect(displayValue(null)).toBe("—");
    expect(displayValue(undefined)).toBe("—");
  });
});

describe("failureStageLabel", () => {
  it("translates known pipeline stages", () => {
    expect(failureStageLabel("tag_reasoning")).toBe("标签推理");
    expect(failureStageLabel("asr")).toBe("ASR");
  });

  it("returns an unknown stage verbatim so a new backend stage stays visible", () => {
    expect(failureStageLabel("brand_new_stage")).toBe("brand_new_stage");
  });

  it("shows a dash when the stage is absent", () => {
    expect(failureStageLabel(null)).toBe("—");
    expect(failureStageLabel(undefined)).toBe("—");
  });
});

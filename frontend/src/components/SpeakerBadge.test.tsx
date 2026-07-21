/**
 * SpeakerBadge component tests — M7 WS-3 T12.
 *
 * Covers:
 *   - Role-only rendering (agent/customer/unknown)
 *   - Ambiguity tag rendering (AMBIGUOUS/PENDING_REVIEW/null)
 *   - Tooltip presence for ambiguity
 *   - size="small" propagation
 *   - displayName prefix
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SpeakerBadge } from "./SpeakerBadge";

describe("SpeakerBadge", () => {
  it("renders agent role with 坐席 label", () => {
    render(<SpeakerBadge role="agent" />);
    expect(screen.getByText(/坐席/)).toBeInTheDocument();
  });

  it("renders customer role with 客户 label", () => {
    render(<SpeakerBadge role="customer" />);
    expect(screen.getByText(/客户/)).toBeInTheDocument();
  });

  it("renders unknown role with 未知 label", () => {
    render(<SpeakerBadge role="unknown" />);
    expect(screen.getByText(/未知/)).toBeInTheDocument();
  });

  it("does not render tooltip wrapper when ambiguity is null", () => {
    const { container } = render(<SpeakerBadge role="agent" ambiguity={null} />);
    // No .arco-tooltip trigger should be present
    const tooltipTrigger = container.querySelector(".arco-tooltip");
    expect(tooltipTrigger).toBeNull();
  });

  it("renders AMBIGUOUS prefix and tooltip", () => {
    render(<SpeakerBadge role="agent" ambiguity="AMBIGUOUS" />);
    expect(screen.getByText(/坐席/)).toBeInTheDocument();
    // The ⚠ prefix is included in the rendered label
    const tag = screen.getByText(/坐席/);
    expect(tag.textContent).toContain("⚠");
  });

  it("renders PENDING_REVIEW prefix", () => {
    render(<SpeakerBadge role="customer" ambiguity="PENDING_REVIEW" />);
    const tag = screen.getByText(/客户/);
    expect(tag.textContent).toContain("⚠");
  });

  it("includes displayName prefix when provided", () => {
    render(<SpeakerBadge role="agent" displayName="张三" />);
    expect(screen.getByText(/张三.*坐席/)).toBeInTheDocument();
  });

  it("applies small size when size='small'", () => {
    const { container } = render(
      <SpeakerBadge role="agent" size="small" />,
    );
    const tag = container.querySelector(".arco-tag");
    expect(tag?.className).toContain("small");
  });
});

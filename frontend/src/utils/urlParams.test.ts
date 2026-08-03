import { describe, expect, it } from "vitest";
import {
  buildFocusParam,
  parseAtParam,
  parseFocusParam,
  parseTimeRangeParams,
} from "./urlParams";

describe("parseFocusParam", () => {
  it("parses a well-formed type:id pair and decodes the id", () => {
    expect(parseFocusParam("录音:rec%20001")).toEqual({
      type: "录音",
      id: "rec 001",
    });
  });

  it("keeps colons inside the id", () => {
    expect(parseFocusParam("产品:产品:milestone")).toEqual({
      type: "产品",
      id: "产品:milestone",
    });
  });

  it("returns null for missing, empty or separator-less values", () => {
    expect(parseFocusParam(null)).toBeNull();
    expect(parseFocusParam("")).toBeNull();
    expect(parseFocusParam("门店")).toBeNull();
    expect(parseFocusParam(":store-1")).toBeNull();
    expect(parseFocusParam("门店:")).toBeNull();
  });

  it("returns null instead of throwing on malformed percent-encoding", () => {
    // A stray "%" used to escape as a URIError out of decodeURIComponent and
    // blank the consuming page, because parsing happens inside a render effect.
    expect(() => parseFocusParam("门店:100%")).not.toThrow();
    expect(parseFocusParam("门店:100%")).toBeNull();
    expect(parseFocusParam("录音:%E4%B8")).toBeNull();
    expect(parseFocusParam("录音:%zz")).toBeNull();
  });

  it("round-trips values built by buildFocusParam", () => {
    expect(parseFocusParam(buildFocusParam("门店", "store 001"))).toEqual({
      type: "门店",
      id: "store 001",
    });
    expect(parseFocusParam(buildFocusParam("录音", 42))).toEqual({
      type: "录音",
      id: "42",
    });
  });
});

describe("parseAtParam", () => {
  it("parses numeric values and rejects the rest", () => {
    expect(parseAtParam("1200")).toBe(1200);
    expect(parseAtParam(null)).toBeNull();
    expect(parseAtParam("")).toBeNull();
    expect(parseAtParam("abc")).toBeNull();
  });
});

describe("parseTimeRangeParams", () => {
  it("parses each bound independently", () => {
    expect(parseTimeRangeParams("100", "abc")).toEqual({
      from: 100,
      to: null,
    });
  });
});

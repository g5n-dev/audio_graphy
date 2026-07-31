import { describe, expect, it } from "vitest";
import { packPcmFrame, resampleTo16k } from "./liveCapture";

describe("live capture PCM transport", () => {
  it("prefixes mono int16 PCM with a four-byte big-endian sequence", () => {
    const pcm = new Int16Array([1, -2, 32_767]);
    const frame = packPcmFrame(0x01020304, pcm);
    const view = new DataView(frame);

    expect(view.getUint32(0, false)).toBe(0x01020304);
    expect(view.getInt16(4, true)).toBe(1);
    expect(view.getInt16(6, true)).toBe(-2);
    expect(view.getInt16(8, true)).toBe(32_767);
    expect(new Int16Array(frame.slice(4))).toEqual(pcm);
  });

  it("resamples a higher-rate mono block to 16 kHz without non-finite samples", () => {
    const input = Float32Array.from(
      { length: 480 },
      (_, index) => Math.sin((index / 480) * Math.PI * 2),
    );
    const output = resampleTo16k(input, 48_000);

    expect(output.length).toBe(160);
    expect([...output].every(Number.isFinite)).toBe(true);
    expect(Math.max(...output)).toBeLessThanOrEqual(32_767);
    expect(Math.min(...output)).toBeGreaterThanOrEqual(-32_768);
  });
});

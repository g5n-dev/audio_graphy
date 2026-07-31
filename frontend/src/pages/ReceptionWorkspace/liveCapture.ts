const TARGET_SAMPLE_RATE = 16_000;
const PCM_CHUNK_SAMPLES = 512;

export interface PcmCaptureHandle {
  stop: () => Promise<void>;
}

interface AudioContextWindow extends Window {
  webkitAudioContext?: typeof AudioContext;
}

/**
 * Wire contract: four-byte unsigned sequence (network byte order), followed
 * by little-endian signed 16-bit mono PCM.
 */
export function packPcmFrame(
  sequence: number,
  pcm: Int16Array,
): ArrayBuffer {
  const frame = new ArrayBuffer(4 + pcm.byteLength);
  const view = new DataView(frame);
  view.setUint32(0, sequence >>> 0, false);
  for (let index = 0; index < pcm.length; index += 1) {
    view.setInt16(4 + index * 2, pcm[index], true);
  }
  return frame;
}

/** Deterministic linear interpolation fallback for browsers without AudioWorklet. */
export function resampleTo16k(
  input: Float32Array,
  inputSampleRate: number,
): Int16Array {
  if (input.length === 0 || inputSampleRate <= 0) return new Int16Array();
  const outputLength = Math.max(
    1,
    Math.round((input.length * TARGET_SAMPLE_RATE) / inputSampleRate),
  );
  const output = new Int16Array(outputLength);
  const ratio = inputSampleRate / TARGET_SAMPLE_RATE;
  for (let index = 0; index < outputLength; index += 1) {
    const sourcePosition = index * ratio;
    const leftIndex = Math.min(Math.floor(sourcePosition), input.length - 1);
    const rightIndex = Math.min(leftIndex + 1, input.length - 1);
    const fraction = sourcePosition - leftIndex;
    const value =
      input[leftIndex] +
      (input[rightIndex] - input[leftIndex]) * fraction;
    const clamped = Math.min(Math.max(value, -1), 1);
    output[index] =
      clamped < 0
        ? Math.round(clamped * 32_768)
        : Math.round(clamped * 32_767);
  }
  return output;
}

const WORKLET_SOURCE = `
class AudioGraphyPcm16kProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.samples = [];
    this.position = 0;
    this.ratio = sampleRate / ${TARGET_SAMPLE_RATE};
    this.output = new Int16Array(${PCM_CHUNK_SAMPLES});
    this.outputIndex = 0;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel || channel.length === 0) return true;
    for (let index = 0; index < channel.length; index += 1) {
      this.samples.push(channel[index]);
    }
    while (this.position + 1 < this.samples.length) {
      const left = Math.floor(this.position);
      const fraction = this.position - left;
      const value =
        this.samples[left] +
        (this.samples[left + 1] - this.samples[left]) * fraction;
      const clamped = Math.min(Math.max(value, -1), 1);
      this.output[this.outputIndex] =
        clamped < 0
          ? Math.round(clamped * 32768)
          : Math.round(clamped * 32767);
      this.outputIndex += 1;
      if (this.outputIndex === this.output.length) {
        const completed = this.output;
        this.port.postMessage(completed, [completed.buffer]);
        this.output = new Int16Array(${PCM_CHUNK_SAMPLES});
        this.outputIndex = 0;
      }
      this.position += this.ratio;
    }
    const consumed = Math.floor(this.position);
    if (consumed > 0) {
      this.samples = this.samples.slice(consumed);
      this.position -= consumed;
    }
    return true;
  }
}
registerProcessor("audiography-pcm-16k", AudioGraphyPcm16kProcessor);
`;

/**
 * Capture mono microphone input and emit 16 kHz int16 chunks. AudioWorklet is
 * preferred; ScriptProcessor is an explicit compatibility fallback.
 */
export async function startPcmCapture(
  onPcm: (pcm: Int16Array) => void,
): Promise<PcmCaptureHandle> {
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new Error("当前浏览器不支持麦克风采集");
  }
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      sampleRate: TARGET_SAMPLE_RATE,
      echoCancellation: true,
      noiseSuppression: true,
    },
  });
  const AudioContextConstructor =
    window.AudioContext ??
    (window as AudioContextWindow).webkitAudioContext;
  if (!AudioContextConstructor) {
    stream.getTracks().forEach((track) => track.stop());
    throw new Error("当前浏览器不支持 Web Audio");
  }

  const context = new AudioContextConstructor({
    sampleRate: TARGET_SAMPLE_RATE,
    latencyHint: "interactive",
  });
  const source = context.createMediaStreamSource(stream);
  let captureNode: AudioNode | null = null;
  let silentGain: GainNode | null = null;
  let workletUrl: string | null = null;

  try {
    if (
      context.audioWorklet &&
      typeof AudioWorkletNode !== "undefined"
    ) {
      workletUrl = URL.createObjectURL(
        new Blob([WORKLET_SOURCE], { type: "text/javascript" }),
      );
      await context.audioWorklet.addModule(workletUrl);
      const worklet = new AudioWorkletNode(
        context,
        "audiography-pcm-16k",
        {
          numberOfInputs: 1,
          numberOfOutputs: 0,
          channelCount: 1,
        },
      );
      worklet.port.onmessage = (event: MessageEvent<Int16Array>) => {
        if (event.data instanceof Int16Array) onPcm(event.data);
      };
      source.connect(worklet);
      captureNode = worklet;
    } else {
      const processor = context.createScriptProcessor(4_096, 1, 1);
      processor.onaudioprocess = (event) => {
        const channel = event.inputBuffer.getChannelData(0);
        const pcm = resampleTo16k(channel, context.sampleRate);
        for (
          let offset = 0;
          offset < pcm.length;
          offset += PCM_CHUNK_SAMPLES
        ) {
          onPcm(pcm.slice(offset, offset + PCM_CHUNK_SAMPLES));
        }
      };
      silentGain = context.createGain();
      silentGain.gain.value = 0;
      source.connect(processor);
      processor.connect(silentGain);
      silentGain.connect(context.destination);
      captureNode = processor;
    }
  } catch (error) {
    source.disconnect();
    stream.getTracks().forEach((track) => track.stop());
    await context.close();
    throw error;
  } finally {
    if (workletUrl) URL.revokeObjectURL(workletUrl);
  }

  return {
    stop: async () => {
      source.disconnect();
      captureNode?.disconnect();
      silentGain?.disconnect();
      stream.getTracks().forEach((track) => track.stop());
      if (context.state !== "closed") await context.close();
    },
  };
}

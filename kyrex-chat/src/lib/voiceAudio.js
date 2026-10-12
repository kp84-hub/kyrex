export class Resampler {
  constructor(rate) { this.step = rate / 16000; this.pending = new Float32Array(); this.offset = 0; }
  push(input) {
    const samples = new Float32Array(this.pending.length + input.length);
    samples.set(this.pending); samples.set(input, this.pending.length);
    const output = [];
    while (this.offset + 1 < samples.length) {
      const i = Math.floor(this.offset), fraction = this.offset - i;
      output.push(samples[i] * (1 - fraction) + samples[i + 1] * fraction);
      this.offset += this.step;
    }
    const consumed = Math.min(Math.floor(this.offset), samples.length);
    this.pending = samples.slice(consumed); this.offset -= consumed;
    return Float32Array.from(output);
  }
}

export function wavBlob(chunks) {
  const count = chunks.reduce((n, chunk) => n + chunk.length, 0);
  const buffer = new ArrayBuffer(44 + count * 2), view = new DataView(buffer);
  const ascii = (offset, value) => [...value].forEach((c, i) => view.setUint8(offset + i, c.charCodeAt(0)));
  ascii(0, 'RIFF'); view.setUint32(4, 36 + count * 2, true); ascii(8, 'WAVE'); ascii(12, 'fmt ');
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, 16000, true); view.setUint32(28, 32000, true); view.setUint16(32, 2, true);
  view.setUint16(34, 16, true); ascii(36, 'data'); view.setUint32(40, count * 2, true);
  let offset = 44;
  for (const chunk of chunks) for (const sample of chunk) {
    view.setInt16(offset, Math.round(Math.max(-1, Math.min(1, sample)) * 32767), true); offset += 2;
  }
  return new Blob([buffer], { type: 'audio/wav' });
}

export class CommandCapture {
  constructor(waitSeconds = 8) { this.waitSamples = waitSeconds * 16000; this.chunks = []; this.samples = 0; this.voiced = 0; this.silence = 0; }
  push(samples) {
    this.chunks.push(samples.slice()); this.samples += samples.length;
    const rms = Math.sqrt(samples.reduce((n, x) => n + x * x, 0) / Math.max(samples.length, 1));
    if (rms >= 0.008) { this.voiced += samples.length; this.silence = 0; }
    else this.silence += samples.length;
    if (this.samples >= 480000) return 'done';
    if (this.voiced >= 2400 && this.silence >= 19200) return 'done';
    if (this.voiced < 2400 && this.samples >= this.waitSamples) return 'empty';
    return null;
  }
}

export async function openMicrophone(onSamples) {
  if (!navigator.mediaDevices?.getUserMedia) throw new Error('Open Kyrex Chat using HTTPS to use the microphone.');
  const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true }, video: false });
  let context, source, node, gain;
  try {
    context = new (window.AudioContext || window.webkitAudioContext)();
    await context.audioWorklet.addModule('/voice-pcm-worklet.js');
    await context.resume();
    const resampler = new Resampler(context.sampleRate);
    node = new AudioWorkletNode(context, 'kyrex-voice-pcm');
    node.port.onmessage = event => onSamples(resampler.push(event.data));
    source = context.createMediaStreamSource(stream); gain = context.createGain(); gain.gain.value = 0;
    source.connect(node); node.connect(gain); gain.connect(context.destination);
    const stop = () => { node.port.onmessage = null; source.disconnect(); node.disconnect(); gain.disconnect(); stream.getTracks().forEach(track => track.stop()); context.close().catch(() => {}); };
    const tone = () => {
      const oscillator = context.createOscillator(), volume = context.createGain();
      oscillator.frequency.value = 880; volume.gain.value = 0.12;
      oscillator.connect(volume); volume.connect(context.destination);
      oscillator.start(); oscillator.stop(context.currentTime + 0.12);
      oscillator.onended = () => { oscillator.disconnect(); volume.disconnect(); };
    };
    return { stop, tone, context, stream };
  } catch (error) {
    stream.getTracks().forEach(track => track.stop()); context?.close().catch(() => {}); throw error;
  }
}

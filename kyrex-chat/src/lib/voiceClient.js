import { CommandCapture, openMicrophone, wavBlob } from './voiceAudio.js';

export function spokenText(raw = '') {
  const value = String(raw).replace(/```[\s\S]*?```/g, ' Details are in your voice chat. ')
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1').replace(/[*#`|]/g, '').replace(/\s+/g, ' ').trim();
  return value.length > 1600 ? value.slice(0, 1600) + '. The full response is in your voice chat.' : value || 'Open your voice chat to review the result.';
}

export async function voiceReply(response) {
  const reader = response.body.getReader(), decoder = new TextDecoder();
  let buffer = '', reply = null;
  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      let end;
      while ((end = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, end).replace(/\r$/, ''); buffer = buffer.slice(end + 1);
        if (!line.startsWith('data: ')) continue;
        const event = JSON.parse(line.slice(6));
        if (event.type === 'done') reply = spokenText(event.content);
        if (event.type === 'error') throw new Error('The request may be saved in voice chat. Open it before repeating the command.');
        if (event.type === 'approval_request') reply = 'This needs your approval. Open your voice chat in Kyrex to review it.';
      }
      if (buffer.length > 65536) throw new Error('Voice received an unreadable response. Check Chat.');
      if (done) break;
    }
    if (!reply) throw new Error('The connection ended. Check your voice chat before repeating the command.');
    return reply;
  } finally { reader.releaseLock(); }
}

export class VoiceClient {
  constructor({ onState = () => {}, onConversation = () => {}, onResult = () => {}, fetcher = (...args) => globalThis.fetch(...args),
    microphone = openMicrophone, workerFactory = () => new Worker('/voice-wake-worker.js'), clientId } = {}) {
    this.onState = onState; this.onConversation = onConversation; this.onResult = onResult;
    this.fetcher = fetcher; this.microphone = microphone; this.workerFactory = workerFactory;
    this.clientId = clientId || crypto.randomUUID().replaceAll('-', '');
    this.generation = 0; this.phase = 'stopped'; this.lease = ''; this.wake = false;
  }
  state(phase, extra = {}) { this.phase = phase; this.onState({ phase, ...extra }); }
  headers(extra = {}) { return { 'X-Voice-Client': this.clientId, ...(this.lease ? { 'X-Voice-Lease': this.lease } : {}), ...extra }; }
  async request(path, { method = 'POST', body, headers = {}, signal = this.abort?.signal } = {}) {
    const response = await this.fetcher('/api/voice/' + path, { method, body, signal: AbortSignal.any([...(signal ? [signal] : []), AbortSignal.timeout(path === 'chat' ? 330000 : path === 'speech' ? 90000 : path === 'transcribe' ? 60000 : 15000)]), headers: this.headers(headers), cache: 'no-store' });
    if (!response.ok) {
      let message = 'Voice is unavailable. Check your voice chat before repeating a command.';
      try { message = (await response.json()).detail || message; } catch {}
      throw new Error(message);
    }
    return response;
  }
  async start(wake = false) {
    await this.stop();
    const token = ++this.generation; this.abort = new AbortController(); this.wake = wake; this.state('starting');
    try {
      const session = await (await this.request('session', { method: 'GET' })).json();
      if (token !== this.generation) return;
      if (!session.settings.configured) throw new Error('Set up Voice in Connections once to use it on your PC and phone.');
      this.settings = session.settings; this.cid = session.conversation_id;
      const lease = (await (await this.request('listen', { body: JSON.stringify({ name: /Android|iPhone|iPad/.test(navigator.userAgent) ? 'Phone browser' : 'PC browser', takeover: true }), headers: { 'Content-Type': 'application/json' } })).json()).lease;
      if (token !== this.generation) { try { await this.fetcher('/api/voice/stop', { method: 'POST', headers: { 'X-Voice-Client': this.clientId, 'X-Voice-Lease': lease } }); } catch {} return; }
      this.lease = lease;
      this.heartbeat = setInterval(async () => {
        if (this.heartbeatBusy) return;
        this.heartbeatBusy = true;
        try { await this.request('heartbeat'); }
        catch (error) { if (token === this.generation) await this.fail(error); }
        finally { this.heartbeatBusy = false; }
      }, 15000);
      const mic = await this.microphone(samples => { if (token === this.generation) this.samples(samples); });
      if (token !== this.generation) { mic.stop(); return; }
      this.mic = mic;
      this.mic.stream?.getAudioTracks().forEach(track => track.addEventListener('ended', () => {
        if (token === this.generation) this.fail(new Error('Microphone access ended. Start listening again.'));
      }));
      this.onConversation(this.cid);
      if (wake) await this.loadWake(token); else this.capture();
    } catch (error) { if (token === this.generation) await this.fail(error); }
  }
  async loadWake(token) {
    this.state('loading');
    this.worker = this.workerFactory(); this.pending = new Float32Array(); this.workerBusy = false;
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Wake detection took too long to load. Try Talk now.')), 60000);
      this.wakeReject = reject;
      this.worker.onerror = () => { clearTimeout(timer); const error = new Error('Wake detection stopped. Try Talk now.'); if (this.phase === 'loading') reject(error); else this.fail(error); };
      this.worker.onmessage = event => {
        if (token !== this.generation) return;
        if (event.data.type === 'ready') { clearTimeout(timer); this.wakeReject = null; this.state('listening'); resolve(); }
        else if (event.data.type === 'error') { clearTimeout(timer); if (this.phase === 'loading') reject(new Error(event.data.error)); else this.fail(new Error(event.data.error)); }
        else if (event.data.type === 'wake' && this.phase === 'listening') this.capture();
        else if (event.data.type === 'accepted') { this.workerBusy = false; this.flushWake(); }
      };
    });
  }
  samples(samples) {
    if (!samples.length) return;
    this.onState({ phase: this.phase, level: Math.round(Math.min(1, Math.max(...samples.map(Math.abs))) * 100) });
    if (this.phase === 'recording') {
      const result = this.recording.push(samples);
      if (result === 'done') this.finishCapture();
      else if (result === 'empty') this.fail(new Error('No speech was heard. Wait for the tone and try again.'));
    } else if (this.phase === 'listening') {
      if (this.pending.length + samples.length > 48000) { this.fail(new Error('Wake detection could not keep up. Try Talk now.')); return; }
      const pending = new Float32Array(this.pending.length + samples.length);
      pending.set(this.pending); pending.set(samples, this.pending.length); this.pending = pending; this.flushWake();
    }
  }
  flushWake() {
    if (this.phase !== 'listening' || this.workerBusy || !this.pending.length) return;
    const samples = this.pending; this.pending = new Float32Array(); this.workerBusy = true;
    this.worker.postMessage({ type: 'samples', samples }, [samples.buffer]);
  }
  capture() {
    if (!['starting', 'listening'].includes(this.phase)) return;
    this.worker?.postMessage({ type: 'pause' }); this.pending = new Float32Array(); this.recording = new CommandCapture();
    this.state('recording'); this.mic.tone();
  }
  async finishCapture() {
    if (this.phase !== 'recording') return;
    const token = this.generation, audio = wavBlob(this.recording.chunks); this.recording = null; this.state('transcribing');
    try {
      const { text } = await (await this.request('transcribe', { body: audio, headers: { 'Content-Type': 'audio/wav' } })).json();
      if (token !== this.generation) return;
      this.state('working', { transcript: text });
      const response = await this.request('chat', { body: JSON.stringify({ message: text, request_id: crypto.randomUUID().replaceAll('-', '') }), headers: { 'Content-Type': 'application/json' } });
      const reply = await voiceReply(response);
      this.onResult(this.cid);
      if (token !== this.generation) return;
      if (this.settings.engine === 'openai') {
        this.state('synthesizing');
        const speech = await this.request('speech', { body: JSON.stringify({ text: reply }), headers: { 'Content-Type': 'application/json' } });
        const data = await speech.arrayBuffer();
        if (data.byteLength > 4 * 1024 * 1024) throw new Error('Reply audio was too large. Read it in voice chat.');
        if (token !== this.generation) return;
        const audioBuffer = await this.mic.context.decodeAudioData(data);
        if (token !== this.generation) return;
        await new Promise(resolve => {
          this.player = this.mic.context.createBufferSource(); this.player.buffer = audioBuffer;
          this.player.connect(this.mic.context.destination); this.player.onended = resolve;
          this.state('speaking'); this.player.start();
        });
      } else {
        if (!window.speechSynthesis) throw new Error('Device speech is unavailable. Choose an AI voice in Connections.');
        await new Promise((resolve, reject) => {
          const utterance = new SpeechSynthesisUtterance(reply); utterance.lang = 'en-US';
          utterance.onend = resolve; utterance.onerror = () => reject(new Error('Reply playback failed. Read it in voice chat.'));
          this.state('speaking'); window.speechSynthesis.speak(utterance);
        });
      }
      if (token !== this.generation) return;
      this.player = null;
      if (this.wake) { this.state('listening'); this.worker.postMessage({ type: 'resume' }); }
      else await this.stop();
    } catch (error) { if (token === this.generation) await this.fail(error); }
  }
  async fail(error) { const message = error?.name === 'NotAllowedError' ? 'Allow microphone access in your browser, then try again.' : error.message; await this.stop(); this.state('stopped', { error: message }); }
  async stop() {
    ++this.generation; this.abort?.abort(); clearInterval(this.heartbeat); this.heartbeat = null;
    this.wakeReject?.(new Error('Listening stopped.')); this.wakeReject = null;
    this.worker?.terminate(); this.worker = null; this.pending = new Float32Array();
    try { this.player?.stop(); } catch {} this.player = null; window.speechSynthesis?.cancel();
    this.mic?.stop(); this.mic = null;
    const lease = this.lease; this.lease = ''; this.state('stopped', { level: 0 });
    if (lease) {
      try { await this.fetcher('/api/voice/stop', { method: 'POST', headers: { 'X-Voice-Client': this.clientId, 'X-Voice-Lease': lease }, keepalive: true, signal: AbortSignal.timeout(5000) }); } catch {}
    }
  }
}

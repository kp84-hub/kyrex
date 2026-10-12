"""Exercise the shipped WASM engine and visible voice controls in Chromium."""
import functools
import http.server
import json
from pathlib import Path
import runpy
import sys
import threading
from playwright.sync_api import sync_playwright
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'kyrex-voice-android'))


def main():
    cases = runpy.run_path(str(ROOT / 'kyrex-voice-android/validate_wake.py'))['fixtures']
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ROOT / 'kyrex-chat/dist'))
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=['--no-sandbox', '--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'])
        context = browser.new_context(permissions=['microphone'], service_workers='block')
        page = context.new_page()
        browser_errors = []
        page.on('pageerror', lambda error: browser_errors.append(str(error)))
        page.goto(f'http://127.0.0.1:{server.server_port}')
        page.evaluate('''async () => {
          window.wakeEvents = [];
          window.kwsWorker = new Worker('/voice-wake-worker.js');
          await new Promise((resolve, reject) => {
            const timer = setTimeout(() => reject(new Error('WASM did not initialize')), 60000);
            kwsWorker.onmessage = event => { wakeEvents.push(event.data); if (event.data.type === 'ready') { clearTimeout(timer); resolve(); } else if (event.data.type === 'error') reject(new Error(event.data.error)); };
          });
        }''')
        for index, (samples, expected) in enumerate(cases):
            actual = page.evaluate('''async samples => {
              kwsWorker.postMessage({ type: 'resume' }); let matched = false;
              for (let offset = 0; offset < samples.length; offset += 1600) {
                const pcm = Float32Array.from(samples.slice(offset, offset + 1600));
                await new Promise((resolve, reject) => {
                  const timer = setTimeout(() => reject(new Error('WASM decode did not return')), 10000);
                  kwsWorker.onmessage = event => {
                    if (event.data.type === 'wake') matched = true;
                    if (event.data.type === 'accepted') { clearTimeout(timer); resolve(); }
                    if (event.data.type === 'error') reject(new Error(event.data.error));
                  };
                  kwsWorker.postMessage({ type: 'samples', samples: pcm }, [pcm.buffer]);
                });
                if (matched) break;
              }
              return matched;
            }''', samples.tolist())
            assert actual is expected, (index + 1, actual, expected)
            print(f'WASM synthetic case {index+1}: wake={actual}, expected={expected}', flush=True)
        page.evaluate('kwsWorker.terminate()')
        # UI boot does not request microphone access or start a voice session.
        requested = []
        def mock(route):
            requested.append(route.request.url)
            url = route.request.url
            if url.endswith('/api/chat/onboarding'):
                data = {'required': False, 'step': 'ready'}
            elif url.endswith('/api/voice/session'):
                data = {'conversation_id': 'voice-browser-test', 'settings': {'configured': True, 'engine': 'device', 'voice': 'cedar'}}
            elif url.endswith('/api/voice/listen'):
                data = {'lease': 'test-only-browser-lease'}
            else:
                data = {'conversations': [], 'bots': [], 'providers': [], 'workspaces': [], 'messages': []}
            route.fulfill(status=200, content_type='application/json', body=json.dumps(data))
        page.route('**/api/**', mock)
        # Isolate browser media hardware for this transport/UI regression.
        # Native Window.fetch and the shipped controls stay unmodified.
        page.add_init_script('''
          window.voiceTestMedia = { opened: 0, stopped: 0, worklets: [] };
          const node = () => ({ connect() {}, disconnect() {} });
          Object.defineProperty(navigator.mediaDevices, 'getUserMedia', { value: async () => {
            voiceTestMedia.opened++;
            const track = { addEventListener() {}, stop() { voiceTestMedia.stopped++; } };
            return { getAudioTracks: () => [track], getTracks: () => [track] };
          }});
          window.AudioContext = class {
            constructor() {
              this.sampleRate = 48000; this.destination = {};
              this.audioWorklet = { addModule: async url => { voiceTestMedia.worklets.push(url); } };
            }
            async resume() {}
            async close() {}
            createMediaStreamSource() { return node(); }
            createGain() { return { ...node(), gain: { value: 0 } }; }
            createOscillator() {
              return { ...node(), frequency: { value: 0 }, start() {},
                stop() { queueMicrotask(() => this.onended?.()); } };
            }
          };
          window.AudioWorkletNode = class {
            constructor() { this.port = { onmessage: null }; }
            connect() {}
            disconnect() {}
          };
        ''')
        page.reload()
        assert page.get_by_role('button', name='Talk to Kyrex', exact=True).is_visible()
        assert page.get_by_role('button', name='Hey Kyrex', exact=True).is_visible()
        assert not any('/api/voice/listen' in url or '/api/voice/chat' in url for url in requested)
        # Real Window.fetch, mocked media APIs, no provider calls: actually
        # start/stop the shipped controls, rather than checking visibility alone.
        page.get_by_role('button', name='Talk to Kyrex', exact=True).click()
        try:
            page.wait_for_function("document.querySelector('.voice-controls [role=status]')?.textContent === 'Speak after the tone'", timeout=15000)
        except Exception:
            print({'voice_controls': page.locator('.voice-controls').inner_text(),
                   'requests': requested, 'browser_errors': browser_errors}, flush=True)
            raise
        assert any('/api/voice/session' in url for url in requested)
        assert any('/api/voice/listen' in url for url in requested)
        assert not page.locator('.voice-controls [role=alert]').count()
        with page.expect_response(lambda response: response.url.endswith('/api/voice/stop')):
            page.get_by_role('button', name='Stop voice', exact=True).click()
        page.wait_for_function("document.querySelector('.voice-controls [role=status]')?.textContent === 'Voice off'", timeout=5000)
        assert any('/api/voice/stop' in url for url in requested)
        assert not any('/api/voice/chat' in url for url in requested)
        assert page.evaluate('voiceTestMedia.opened') == 1
        assert page.evaluate('voiceTestMedia.stopped') == 1
        assert page.evaluate('voiceTestMedia.worklets') == ['/voice-pcm-worklet.js']
        browser.close()
    server.shutdown()
    print('Shipped browser WASM and voice controls passed; real microphone/device tests remain required.')


if __name__ == '__main__': main()

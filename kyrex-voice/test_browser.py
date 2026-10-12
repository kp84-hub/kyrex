"""Exercise the shipped WASM engine and visible voice controls in Chromium."""
import functools
import http.server
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
        browser = playwright.chromium.launch(args=['--no-sandbox'])
        page = browser.new_page(); page.goto(f'http://127.0.0.1:{server.server_port}')
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
            route.fulfill(status=200, content_type='application/json', body='{"conversations":[],"bots":[],"providers":[],"workspaces":[]}')
        page.route('**/api/**', mock)
        page.reload()
        assert page.get_by_role('button', name='Talk to Kyrex', exact=True).is_visible()
        assert page.get_by_role('button', name='Hey Kyrex', exact=True).is_visible()
        assert not any('/api/voice/listen' in url or '/api/voice/chat' in url for url in requested)
        browser.close()
    server.shutdown()
    print('Shipped browser WASM and voice controls passed; real microphone/device tests remain required.')


if __name__ == '__main__': main()

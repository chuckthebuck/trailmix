#!/usr/bin/env python3
"""Run checks against temporary data; no production database is used."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parent


def run(*args, **kwargs):
    subprocess.run(args, cwd=ROOT, check=True, **kwargs)


def main():
    node = shutil.which('node')
    if not node:
        raise SystemExit('Development checks require Node.js 18+; production does not.')
    run(sys.executable, '-m', 'unittest', 'discover', '-p', 'test_*.py', '-v')
    run(node, 'test_keyboard.cjs')
    with tempfile.TemporaryDirectory(prefix='trailmix-checks-') as temp:
        # Reserve an OS-assigned port by starting the actual HTTP server on port 0.
        # Read its bound port from a small launcher instead of choosing a fixed port.
        launcher = """import server
original_server = server.ThreadingHTTPServer
def create(*args, **kwargs):
    instance = original_server(*args, **kwargs)
    print('TEST_PORT=' + str(instance.server_port), flush=True)
    return instance
server.ThreadingHTTPServer = create
server.main()
"""
        env = dict(os.environ, TRAILMIX_ACCESS_CODE='test-access',
                   TRAILMIX_SAFETY_PIN='123456', TRAILMIX_HTTPS='0')
        proc = subprocess.Popen(
            [sys.executable, '-u', '-c', launcher, '--host', '127.0.0.1',
             '--port', '0', '--db', str(Path(temp)/'checks.sqlite3')],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True)
        try:
            line = proc.stdout.readline().strip()
            if not line.startswith('TEST_PORT='):
                raise RuntimeError('Test server could not start: '+line)
            url = 'http://127.0.0.1:'+line.split('=',1)[1]
            for attempt in range(100):
                try:
                    with urllib.request.urlopen(url, timeout=1) as response:
                        if response.status == 200:
                            break
                except OSError:
                    if proc.poll() is not None:
                        raise RuntimeError('Test server exited before becoming ready')
                    time.sleep(0.05)
            else:
                raise RuntimeError('Test server did not become ready')
            # Drain server logs to a file so a full stdout pipe cannot block writes.
            import threading
            with open(Path(temp)/'server.log','w') as log:
                def drain():
                    for output in proc.stdout:
                        log.write(output)
                reader = threading.Thread(target=drain, daemon=True)
                reader.start()
                try:
                    run(node, 'test_workflow.cjs', env=dict(env, TRAILMIX_TEST_URL=url), timeout=60)
                except subprocess.CalledProcessError:
                    log.flush()
                    print((Path(temp)/'server.log').read_text(),file=sys.stderr)
                    raise
                finally:
                    proc.terminate()
                    proc.wait(timeout=10)
                    reader.join(timeout=2)
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)
            proc.stdout.close()
    print('All checks passed. Temporary production-flow records removed.')


if __name__ == '__main__':
    main()

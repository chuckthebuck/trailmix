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


def legacy_workflow():
    node = shutil.which('node')
    if not node:
        raise SystemExit('Development checks require Node.js 18+; production does not.')
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
        env = {k:v for k,v in os.environ.items() if k not in ('RENDER','TURSO_DATABASE_URL','TURSO_AUTH_TOKEN')}
        env = dict(env, TRAILMIX_UPGRADE_WORKFLOW='0', TRAILMIX_SEED_ROSTER='0', TRAILMIX_REQUIRE_ACCESS_CODE='1', TRAILMIX_ACCESS_CODE='test-access',
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


def main():
    go, node = shutil.which('go'), shutil.which('node')
    if not go or not node:
        raise SystemExit('Development checks require Go 1.23+ and Node.js 18+. Production runs only the compiled binary.')
    run(go,'test','-count=1','./...')
    run(go,'vet','./...')
    with tempfile.TemporaryDirectory(prefix='trailmix-go-checks-') as temp:
        binary=str(Path(temp)/'trailmix')
        run(go,'build','-buildvcs=false','-trimpath','-o',binary,'.')
        env=dict(os.environ,TRAILMIX_GO_BINARY=binary)
        run(sys.executable,'-m','unittest','discover','-p','test_*.py','-v',env=env)
        run(node,'test_keyboard.cjs')
        run(node,'test_sync.cjs')
        from go_gateway import start_gateway
        import threading
        gateway=start_gateway(Path(temp)/'go-workflow.sqlite3')
        env=dict(env,TURSO_DATABASE_URL='http://127.0.0.1:'+str(gateway.server_port),
                 TURSO_AUTH_TOKEN='test-db-token',TRAILMIX_TEST_DB='1',TRAILMIX_SEED_ROSTER='0',
                 TRAILMIX_UPGRADE_WORKFLOW='0',TRAILMIX_REQUIRE_ACCESS_CODE='1',
                 TRAILMIX_ACCESS_CODE='test-access',TRAILMIX_SAFETY_PIN='123456',TRAILMIX_HTTPS='0')
        proc=subprocess.Popen([binary,'--host','127.0.0.1','--port','0'],cwd=ROOT,env=env,
                              stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
        try:
            line=proc.stdout.readline().strip()
            if not line.startswith('TrailMix Go is running at http://'):
                raise RuntimeError('Go startup failed: '+line)
            url=line.split(' at ',1)[1]
            with open(Path(temp)/'go-server.log','w') as log:
                def drain():
                    for line in proc.stdout:log.write(line)
                reader=threading.Thread(target=drain,daemon=True);reader.start()
                try:
                    run(node,'test_workflow.cjs',env=dict(env,TRAILMIX_TEST_URL=url),timeout=120)
                except Exception:
                    log.flush();print((Path(temp)/'go-server.log').read_text(),file=sys.stderr);raise
                finally:
                    proc.terminate();proc.wait(timeout=20);reader.join(timeout=2)
        finally:
            if proc.poll() is None:proc.terminate();proc.wait(timeout=20)
            proc.stdout.close();gateway.shutdown();gateway.server_close()
    # The frontend must also handle a Python server during a rolling upgrade.
    legacy_workflow()
    print('Go, migration, browser synchronization and legacy compatibility checks passed.')


if __name__ == '__main__':
    main()

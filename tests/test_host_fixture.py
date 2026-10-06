"""An actual owned HTTP process; validates readiness, pages and conditional GET."""
import json
import queue
import subprocess
import sys
import threading

import httpx

def test_actual_http_fixture_pages_and_etag(tmp_path):
    (tmp_path/'.opennexus-test').write_text('fixture',encoding='ascii')
    child = subprocess.Popen([sys.executable,'-m','tests.host_fixture',str(tmp_path)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    line = queue.Queue()
    reader = threading.Thread(target=lambda:line.put(child.stdout.readline()),daemon=True)
    reader.start()
    try:
        ready = json.loads(line.get(timeout=15))
        endpoint = f"http://127.0.0.1:{ready['port']}"
        with httpx.Client(base_url=endpoint,timeout=10) as client:
            assert client.get('/health').json() == {'status':'ok'}
            first = client.get('/catalog/v1/packages',params={'limit':30})
            assert first.json()['total'] == 135
            assert len(first.json()['items']) == 30
            assert client.get('/catalog/v1/packages',params={'limit':30},headers={'If-None-Match':first.headers['etag']}).status_code == 304
            last = client.get('/catalog/v1/packages',params={'offset':120,'limit':30})
            assert len(last.json()['items']) == 15
            assert client.get('/catalog/v1/releases/fixture-0129').json()['package_id'] == 'item-0129'
    finally:
        child.stdin.close()
        try: child.wait(timeout=10)
        except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=5)
        child.stdout.close(); child.stderr.close(); reader.join(timeout=1)
    assert child.returncode == 0

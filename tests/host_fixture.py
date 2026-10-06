"""Fresh marker-owned public HTTP fixture. No production database or tokens."""
import asyncio
import base64
import hashlib
import io
import json
from pathlib import Path
import socket
import sys
import threading
from urllib.parse import urlsplit
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import uvicorn
from community.app import Registry, create_app
from community.package import Release, signed_payload

def main():
    root = Path(sys.argv[1]).resolve(strict=True)
    if not root.is_dir() or not (root/'.opennexus-test').is_file() or (root/'catalog.sqlite3').exists():
        raise SystemExit('FRESH_ISOLATED_ROOT_REQUIRED')
    family = len(sys.argv) > 2 and sys.argv[2] == '--family'
    origins = tuple(sys.argv[3:] if family else sys.argv[2:])
    if any(urlsplit(origin).scheme != 'http' or urlsplit(origin).hostname not in {'127.0.0.1','localhost'} for origin in origins):
        raise SystemExit('LOCAL_TEST_ORIGIN_REQUIRED')
    registry = Registry(root/'catalog.sqlite3')
    signer = Ed25519PrivateKey.generate()
    public = signer.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)
    registry.add_key('fixture-key','examples',public)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive,'w') as output:
        output.writestr('persona.json',json.dumps({'system_prompt':'Controlled public fixture'}))
    blob = archive.getvalue()
    with registry.connect() as conn:
        for index in range(135):
            version = ('1.0.0+build.1' if index == 0 else f'2.0.{index}') if family else '1.0.0'
            release = Release(namespace='examples',package_id='version-family' if family else f'item-{index:04}',type='persona',version=version,name=f'课程示例 {index:03}',author_id='fixture-author',license='MIT',description='Public HTTP pagination fixture · Straße #%',sha256=hashlib.sha256(blob).hexdigest(),size=len(blob),platforms=['windows'],architectures=['x86_64'],min_app_version='0.2.0',changelog='Generated fixture',published_at='2026-10-06T00:00:00Z',key_id='fixture-key',signature='')
            release.signature = base64.b64encode(signer.sign(signed_payload(release))).decode()
            conn.execute('INSERT INTO submissions VALUES (?,?,?,?,?,?,?,?)',(f'fixture-{index:04}',release.namespace,release.package_id,release.version,release.model_dump_json(),blob,release.author_id,'published'))
    app = create_app(registry,'catalog-host-fixture',origins)
    sock = socket.socket()
    sock.bind(('127.0.0.1',0)); sock.listen(128)
    server = uvicorn.Server(uvicorn.Config(app,log_config=None,access_log=False,timeout_graceful_shutdown=1))
    def parent():
        sys.stdin.buffer.read(); server.should_exit = True
    threading.Thread(target=parent,daemon=True).start()
    async def run():
        task = asyncio.create_task(server.serve(sockets=[sock]))
        while not server.started:
            if task.done(): await task; raise RuntimeError('FIXTURE_START_FAILED')
            await asyncio.sleep(.01)
        print(json.dumps({'port':sock.getsockname()[1],'source_id':'catalog-host-fixture','key':{'key_id':'fixture-key','namespace':'examples','public_key':base64.b64encode(public).decode(),'revoked':False}}),flush=True)
        await task
    try: asyncio.run(run())
    finally: sock.close()

if __name__ == '__main__': main()

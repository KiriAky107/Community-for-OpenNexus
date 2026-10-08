import re
from fastapi.testclient import TestClient
from community.app import Registry, create_app


def test_public_shell_shared_links_static_assets_and_security_headers(tmp_path):
    with TestClient(create_app(Registry(tmp_path / 'catalog.db'))) as client:
        home = client.get('/')
        assert home.status_code == 200
        assert 'text/html' in home.headers['content-type']
        assert '<title>OpenNexus Community</title>' in home.text
        assert client.get('/packages/examples/note-reviewer?version=1.0.0').content == home.content
        assert client.get('/workbench').content == home.content
        paths = re.findall(r'(?:src|href)="(/assets/[^"]+)"', home.text)
        assert len(paths) >= 2
        for path in ['/', '/workbench', '/packages/examples/note-reviewer', *paths]:
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers['cache-control'] == 'no-store'
            assert response.headers['x-content-type-options'] == 'nosniff'
            assert response.headers['referrer-policy'] == 'no-referrer'
            csp = response.headers['content-security-policy']
            assert "frame-ancestors 'none'" in csp
            assert 'unsafe-inline' not in csp and 'unsafe-eval' not in csp
        assert client.get('/catalog/v1/sources').headers['cache-control'] == 'no-store'
        assert client.get('/catalog/v1/packages').json()['items'] == []
        assert client.get('/catalog/v1/unknown').status_code == 404
        assert client.get('/not-a-web-route').status_code == 404
        assert client.get('/packages/INVALID/note-reviewer').status_code == 404
        assert client.get('/assets/missing.js').status_code == 404


def test_missing_web_build_is_explicit_and_api_remains_available(tmp_path, monkeypatch):
    import community.web
    monkeypatch.setattr(community.web, 'STATIC', tmp_path)
    (tmp_path / 'assets').mkdir()
    with TestClient(create_app(Registry(tmp_path / 'catalog.db'))) as client:
        assert client.get('/').status_code == 503
        assert client.get('/workbench').status_code == 503
        assert client.get('/').json()['error']['code'] == 'CONSOLE_UNAVAILABLE'
        assert client.get('/catalog/v1/packages').status_code == 200

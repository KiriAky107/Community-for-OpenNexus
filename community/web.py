"""Serve the compiled public catalog without swallowing API or unknown paths."""

import re
from pathlib import Path

from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

STATIC = Path(__file__).parent / 'static'
HEADERS = {
    'Cache-Control': 'no-store',
    'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
    'Referrer-Policy': 'no-referrer',
}


def mount_web(app):
    @app.middleware('http')
    async def web_headers(request, call_next):
        response = await call_next(request)
        if request.url.path in ('/', '/workbench') or request.url.path.startswith(('/packages/', '/assets/')):
            response.headers.update(HEADERS)
        return response

    def shell():
        if not (STATIC / 'index.html').is_file():
            return JSONResponse({'error': {'code': 'CONSOLE_UNAVAILABLE'}}, status_code=503, headers=HEADERS)
        return FileResponse(STATIC / 'index.html', headers=HEADERS)

    app.add_api_route('/', shell, methods=['GET'], include_in_schema=False)
    app.add_api_route('/workbench', shell, methods=['GET'], include_in_schema=False)

    @app.get('/packages/{namespace}/{package_id}', include_in_schema=False)
    def package_page(namespace: str, package_id: str):
        if not all(re.fullmatch(r'[a-z0-9][a-z0-9-]{1,63}', value) for value in (namespace, package_id)):
            return JSONResponse({'error': {'code': 'PACKAGE_NOT_FOUND'}}, status_code=404)
        return shell()

    app.mount('/assets', StaticFiles(directory=STATIC / 'assets', check_dir=False), name='web-assets')

"""Original, read-only local MCP example. Start it separately from OpenNexus."""
import argparse
import json
import math
import statistics

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26')
TOOL = {
    'name': 'summarize_numbers',
    'description': 'Return count, total, mean, median, minimum and maximum for a small numeric dataset. No files or network access.',
    'inputSchema': {'type': 'object', 'properties': {'numbers': {'type': 'array', 'minItems': 1, 'maxItems': 1000,
                    'items': {'type': 'number', 'minimum': -1e100, 'maximum': 1e100}}}, 'required': ['numbers'], 'additionalProperties': False},
    'annotations': {'readOnlyHint': True, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': False},
}


def error(identifier, code, message):
    return JSONResponse({'jsonrpc': '2.0', 'id': identifier, 'error': {'code': code, 'message': message}})


def finite(_value):
    raise ValueError('Non-finite JSON')


@app.post('/mcp')
async def rpc(request: Request):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 32768:
            return Response(status_code=413)
    try:
        data = json.loads(body, parse_constant=finite)
    except (ValueError, UnicodeError, RecursionError):
        return error(None, -32700, 'Invalid JSON')
    if (not isinstance(data, dict) or data.get('jsonrpc') != '2.0'
            or not isinstance(data.get('method'), str)):
        return error(None, -32600, 'Invalid request')
    identifier, method, params = data.get('id'), data['method'], data.get('params', {})
    if identifier is None:
        return Response(status_code=202)
    if type(identifier) not in (int, str) or not isinstance(params, dict):
        return error(None, -32600, 'Invalid request')
    if method == 'initialize':
        requested = params.get('protocolVersion')
        result = {'protocolVersion': requested if requested in VERSIONS else VERSIONS[0], 'capabilities': {'tools': {}},
                  'serverInfo': {'name': 'opennexus-summary-example', 'version': '1.0.0'}}
    elif method == 'ping':
        result = {}
    elif method == 'tools/list':
        result = {'tools': [TOOL]}
    elif method == 'tools/call':
        args = params.get('arguments')
        if params.get('name') != TOOL['name'] or not isinstance(args, dict) or set(args) != {'numbers'}:
            return error(identifier, -32602, 'Expected summarize_numbers with numbers')
        numbers = args['numbers']
        if (not isinstance(numbers, list) or not 1 <= len(numbers) <= 1000
                or any(type(n) not in (int, float) or abs(n) > 1e100 or not math.isfinite(n) for n in numbers)):
            return error(identifier, -32602, 'Expected 1 to 1000 finite numbers within +/-1e100')
        summary = {'count': len(numbers), 'total': math.fsum(numbers), 'mean': statistics.fmean(numbers),
                   'median': statistics.median(numbers), 'minimum': min(numbers), 'maximum': max(numbers)}
        result = {'content': [{'type': 'text', 'text': json.dumps(summary, ensure_ascii=False)}], 'isError': False}
    else:
        return error(identifier, -32601, 'Unknown method')
    return JSONResponse({'jsonrpc': '2.0', 'id': identifier, 'result': result})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Read-only numeric MCP example, listening only on 127.0.0.1.')
    parser.add_argument('--port', type=int, default=18970)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('port must be 1 to 65535')
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=args.port, access_log=False, proxy_headers=False)

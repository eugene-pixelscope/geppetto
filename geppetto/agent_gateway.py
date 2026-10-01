"""TLS ingress for outbound Agents; only machine protocol routes reach the local catalog."""
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
ALLOWED = {'enroll', 'snapshot', 'jobs/poll', 'jobs/observe'}


@app.get('/health')
def health():
    return {'ok': True}


@app.post('/api/agent/{path:path}')
async def relay(path: str, request: Request):
    if path not in ALLOWED:
        return Response(status_code=404)
    if int(request.headers.get('content-length', '0')) > 16*1024*1024:
        return Response(status_code=413)
    body = await request.body()
    if len(body) > 16*1024*1024:
        return Response(status_code=413)
    headers = {k: request.headers[k] for k in ('authorization', 'content-type', 'x-geppetto-request') if k in request.headers}
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post('http://127.0.0.1:8091/api/agent/'+path, content=body, headers=headers)
    return Response(content=response.content, status_code=response.status_code, media_type='application/json')

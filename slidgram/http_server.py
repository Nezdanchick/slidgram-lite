import logging
import base64
import zlib
from aiohttp import web

log = logging.getLogger(__name__)

_gateway = None

def decode_payload(b64_payload: str):
    try:
        data = zlib.decompress(base64.urlsafe_b64decode(b64_payload)).decode('utf-8')
        parts = data.split('|', 2)
        if len(parts) == 3:
            return parts[0], parts[1], parts[2]
    except Exception as e:
        log.warning("Payload decode failed: %s", e)
    return None, None, None

async def handle_b64_media(request):
    b64_payload = request.match_info.get('payload')
    file_id, token, filename = decode_payload(b64_payload)
    if not file_id:
        return web.Response(status=404, text="Invalid link payload")

    from . import config
    if config.MEDIA_TOKEN and token != config.MEDIA_TOKEN:
        return web.Response(status=403, text="Forbidden: Invalid or expired token")

    from .session import Session
    if not Session.active_sessions:
        return web.Response(status=404, text="No active telegram sessions")

    session = list(Session.active_sessions)[0]
    tg_client = session.tg

    try:
        downloader = tg_client.get_downloader(file_id)
        if not downloader:
            return web.Response(status=404, text="File not found or expired")

        response = web.StreamResponse(
            status=200,
            reason='OK',
            headers={'Content-Type': 'application/octet-stream'}
        )
        await response.prepare(request)

        async for chunk in downloader:
            await response.write(chunk)

        await response.write_eof()
        return response

    except Exception as e:
        log.exception("Error streaming file: %s", file_id)
        return web.Response(status=500, text=str(e))

async def handle_b64_viewer(request):
    b64_payload = request.match_info.get('payload')
    file_id, token, filename = decode_payload(b64_payload)
    if not file_id:
        return web.Response(status=404, text="Invalid link payload")

    from . import config
    if config.MEDIA_TOKEN and token != config.MEDIA_TOKEN:
        return web.Response(status=403, text="Forbidden: Invalid or expired token")

    try:
        import os
        template_path = os.path.join(os.path.dirname(__file__), 'template.html')
        with open(template_path, 'r', encoding='utf-8') as f:
            html_template = f.read()
    except Exception:
        html_template = "<html><body><a href='{file_url}'>Download {filename}</a></body></html>"
        
    html = html_template.format(
        filename=filename,
        file_url=f"/b64file/{b64_payload}"
    )
    return web.Response(text=html, content_type='text/html')

async def start_server(gateway_instance, port=5050):
    global _gateway
    _gateway = gateway_instance
    app = web.Application()
    app.router.add_get('/b64/{payload}', handle_b64_viewer)
    app.router.add_get('/b64file/{payload}', handle_b64_media)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    log.info(f"Slidgram local media server started on port {port}")

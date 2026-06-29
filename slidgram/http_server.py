import logging
import base64
import zlib
from aiohttp import web

log = logging.getLogger(__name__)

_gateway = None

def decode_payload(b64_payload: str):
    try:
        b64_payload += '=' * (-len(b64_payload) % 4)
        data = zlib.decompress(base64.urlsafe_b64decode(b64_payload)).decode('utf-8')
        parts = data.split('|', 2)
        if len(parts) >= 2:
            file_id = parts[0]
            token = parts[1]
            size = parts[2] if len(parts) > 2 else "0"
            return file_id, token, size
    except Exception as e:
        log.warning("Payload decode failed: %s", e)
    return None, None, None

async def handle_b64_media(request):
    b64_payload = request.match_info.get('payload')
    import urllib.parse
    filename = urllib.parse.unquote(request.match_info.get('filename', 'media.bin'))
    file_id, token, file_size = decode_payload(b64_payload)
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

        headers = {
            'Content-Type': 'application/octet-stream',
            'Content-Disposition': f'attachment; filename="{filename}"'
        }
        response = web.StreamResponse(
            status=200,
            reason='OK',
            headers=headers
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
    import urllib.parse
    filename = urllib.parse.unquote(request.match_info.get('filename', 'media.bin'))
    file_id, token, file_size = decode_payload(b64_payload)
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
        html_template = "<html><body><a href='{file_url_orig}'>Download {filename}</a></body></html>"
        
    try:
        size_bytes = int(file_size) if file_size else 0
        if size_bytes == 0:
            formatted_size = "Неизвестный размер"
        elif size_bytes < 1024:
            formatted_size = f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            formatted_size = f"{size_bytes / 1024:.1f} KB"
        else:
            formatted_size = f"{size_bytes / (1024 * 1024):.1f} MB"
    except Exception:
        formatted_size = "Неизвестный размер"

    from . import config
    import urllib.parse
    base_proxy = config.PROXY_MEDIA_URL.strip() if config.PROXY_MEDIA_URL else ""
    if base_proxy and not base_proxy.endswith("/"):
        base_proxy += "/"
        
    safe_name = urllib.parse.quote(filename)
    raw_file_url = f"/raw/{b64_payload}/{safe_name}"
    
    if base_proxy:
        host = request.headers.get('Host', f"{config.MEDIA_SERVER_HOST}:{config.MEDIA_SERVER_PORT}")
        scheme = request.headers.get('X-Forwarded-Proto', 'http')
        absolute_file_url = f"{scheme}://{host}{raw_file_url}"
        file_url_proxy = f"{base_proxy}{absolute_file_url}"
        file_url_orig = absolute_file_url
    else:
        file_url_proxy = raw_file_url
        file_url_orig = raw_file_url
        
    html = html_template.format(
        filename=filename,
        file_url_proxy=file_url_proxy,
        file_url_orig=file_url_orig,
        formatted_size=formatted_size
    )
    return web.Response(text=html, content_type='text/html')

async def start_server(gateway_instance, port=5050):
    global _gateway
    _gateway = gateway_instance
    app = web.Application()
    app.router.add_get(r'/get/{payload}/{filename:.+}', handle_b64_viewer)
    app.router.add_get(r'/raw/{payload}/{filename:.+}', handle_b64_media)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    log.info(f"Slidgram local media server started on port {port}")

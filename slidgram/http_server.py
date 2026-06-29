import logging
from aiohttp import web

log = logging.getLogger(__name__)

# Сюда мы передадим инстанс Gateway, чтобы иметь доступ к сессиям
_gateway = None

async def handle_media(request):
    file_id = request.match_info.get('file_id')
    
    from .session import Session
    
    if not Session.active_sessions:
        return web.Response(status=404, text="No active telegram sessions")

    # Берем первую активную сессию (XMPP-пользователя), чтобы скачать файл
    session = list(Session.active_sessions)[0]
    tg_client = session.tg

    try:
        # get_downloader возвращает асинхронный генератор байтов (поток)
        downloader = tg_client.get_downloader(file_id)
        if not downloader:
            return web.Response(status=404, text="File not found or expired")

        response = web.StreamResponse(
            status=200,
            reason='OK',
            headers={'Content-Type': 'application/octet-stream'}
        )
        await response.prepare(request)

        # Стримим файл кусками напрямую клиенту (без записи на диск)
        async for chunk in downloader:
            await response.write(chunk)

        await response.write_eof()
        return response

    except Exception as e:
        log.exception("Error streaming file: %s", file_id)
        return web.Response(status=500, text=str(e))

async def handle_viewer(request):
    file_id = request.match_info.get('file_id')
    filename = request.query.get('name', 'file')
    
    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{filename}</title>
    <style>
        body {{ background-color: #121212; color: #ffffff; text-align: center; font-family: sans-serif; margin: 0; padding: 20px; }}
        img, video {{ max-width: 100%; max-height: 70vh; object-fit: contain; margin-bottom: 20px; }}
        .btn {{ display: inline-block; padding: 15px 30px; background-color: #0088cc; color: white; text-decoration: none; border-radius: 8px; font-weight: bold; margin-top: 20px; }}
        .btn:hover {{ background-color: #006699; }}
        .container {{ display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 90vh; }}
    </style>
</head>
<body>
    <div class="container">
        <h3>{filename}</h3>
        <img src="/file/{file_id}" alt="Media" onerror="this.style.display='none';">
        <a href="/file/{file_id}" class="btn" download="{filename}">Скачать файл</a>
    </div>
</body>
</html>"""
    return web.Response(text=html, content_type='text/html')

async def start_server(gateway_instance, port=5050):
    global _gateway
    _gateway = gateway_instance
    
    app = web.Application()
    app.router.add_get('/{file_id}', handle_viewer)
    app.router.add_get('/file/{file_id}', handle_media)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    log.info(f"Slidgram local media server started on port {port}")

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

async def start_server(gateway_instance, port=5050):
    global _gateway
    _gateway = gateway_instance
    
    app = web.Application()
    app.router.add_get('/{file_id}', handle_media)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    log.info(f"Slidgram local media server started on port {port}")

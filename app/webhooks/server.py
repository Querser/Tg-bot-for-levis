from __future__ import annotations

from collections.abc import Awaitable, Callable

from aiohttp import web


class WebhookServer:
    def __init__(
        self,
        host: str,
        port: int,
        yookassa_path: str,
        yookassa_handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> None:
        self._host = host
        self._port = port
        self._yookassa_path = yookassa_path
        self._yookassa_handler = yookassa_handler
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post(self._yookassa_path, self._yookassa_handler)
        app.router.add_get("/health", self._health)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host=self._host, port=self._port)
        await self._site.start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None

    @staticmethod
    async def _health(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})


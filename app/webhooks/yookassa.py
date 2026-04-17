from __future__ import annotations

import logging
from typing import Awaitable, Callable

from aiohttp import web

from app.services.payment_service import PaymentService, WebhookValidationError

LOGGER = logging.getLogger(__name__)


async def _process_yookassa_webhook(
    *,
    request: web.Request,
    payment_service: PaymentService,
) -> web.Response:
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"status": "invalid_json"}, status=400)

    if not isinstance(payload, dict):
        return web.json_response({"status": "invalid_payload"}, status=400)

    try:
        await payment_service.process_webhook_event(payload)
    except WebhookValidationError:
        return web.json_response({"status": "ignored"}, status=200)
    except Exception:
        LOGGER.exception("Failed to process YooKassa webhook event.")
        return web.json_response({"status": "error"}, status=200)

    return web.json_response({"status": "ok"}, status=200)


def create_yookassa_webhook_handler(
    payment_service: PaymentService,
) -> Callable[[web.Request], Awaitable[web.Response]]:
    async def handler(request: web.Request) -> web.Response:
        return await _process_yookassa_webhook(request=request, payment_service=payment_service)

    return handler


async def yookassa_webhook(request: web.Request) -> web.Response:
    service_raw = request.app.get("payment_service")
    if not isinstance(service_raw, PaymentService):
        raise web.HTTPInternalServerError(reason="Payment service is not configured.")

    return await _process_yookassa_webhook(request=request, payment_service=service_raw)


def setup_yookassa_webhook_route(
    app: web.Application,
    *,
    payment_service: PaymentService,
    path: str,
) -> None:
    app.router.add_post(path, create_yookassa_webhook_handler(payment_service))


__all__ = [
    "create_yookassa_webhook_handler",
    "setup_yookassa_webhook_route",
    "yookassa_webhook",
]

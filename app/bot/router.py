from __future__ import annotations

from typing import Any, Iterable

from aiogram import Router

from app.bot.handlers import register_handlers


def create_bot_router(
    payment_service: Any,
    event_settings_service: Any,
    super_admin_ids: Iterable[int],
    ticket_admin_ids: Iterable[int],
) -> Router:
    router = Router(name="main_bot_router")
    register_handlers(
        router=router,
        payment_service=payment_service,
        event_settings_service=event_settings_service,
        super_admin_ids=super_admin_ids,
        ticket_admin_ids=ticket_admin_ids,
    )
    return router

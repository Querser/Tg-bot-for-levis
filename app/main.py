from __future__ import annotations

import asyncio
import logging
from urllib.request import getproxies

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError

from app.bot import create_bot_router
from app.config import load_settings
from app.integrations.yookassa_client import YooKassaClient
from app.logging_config import configure_logging
from app.services import EventSettingsService, PaymentService
from app.storage.db import build_engine, build_session_factory, init_database
from app.storage.repositories import SqlAlchemyAppSettingsRepository, SqlAlchemyPaymentRepository
from app.webhooks.server import WebhookServer
from app.webhooks.yookassa import create_yookassa_webhook_handler

LOGGER = logging.getLogger(__name__)


async def run_polling_with_retries(
    dispatcher: Dispatcher,
    bot: Bot,
    drop_pending_updates: bool,
) -> None:
    retry_delay_seconds = 5
    while True:
        try:
            await dispatcher.start_polling(
                bot,
                allowed_updates=dispatcher.resolve_used_update_types(),
                drop_pending_updates=drop_pending_updates,
            )
            return
        except TelegramNetworkError as exc:
            LOGGER.warning(
                "Сетевой сбой Telegram API: %s. Повторный запуск polling через %s сек.",
                exc,
                retry_delay_seconds,
            )
            await asyncio.sleep(retry_delay_seconds)
            retry_delay_seconds = min(retry_delay_seconds * 2, 60)


async def run() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)

    engine = build_engine(settings.database_url)
    await init_database(engine)
    session_factory = build_session_factory(engine)
    repository = SqlAlchemyPaymentRepository(session_factory)
    app_settings_repository = SqlAlchemyAppSettingsRepository(session_factory)

    yookassa_client = YooKassaClient(
        shop_id=settings.yookassa_shop_id,
        secret_key=settings.yookassa_secret_key,
        base_url=settings.yookassa_api_base_url,
        timeout_seconds=settings.request_timeout_seconds,
        trust_env=settings.yookassa_trust_env,
    )
    payment_service = PaymentService(repository=repository, yookassa_client=yookassa_client)
    event_settings_service = EventSettingsService(
        repository=app_settings_repository,
        default_event_address=settings.default_event_address,
    )

    proxies = getproxies() if settings.http_trust_env else {}
    telegram_proxy = settings.telegram_proxy_url.strip() or proxies.get("https") or proxies.get("http")
    telegram_session = AiohttpSession(proxy=telegram_proxy) if telegram_proxy else AiohttpSession()
    LOGGER.info("Telegram proxy enabled: %s", bool(telegram_proxy))
    bot = Bot(
        token=settings.telegram_bot_token,
        session=telegram_session,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(
        create_bot_router(
            payment_service=payment_service,
            event_settings_service=event_settings_service,
            super_admin_ids=settings.super_admin_ids,
            ticket_admin_ids=settings.ticket_admin_ids,
        )
    )

    webhook_server = WebhookServer(
        host=settings.webhook_host,
        port=settings.webhook_port,
        yookassa_path=settings.yookassa_webhook_path,
        yookassa_handler=create_yookassa_webhook_handler(payment_service),
    )
    await webhook_server.start()
    LOGGER.info(
        "Webhook server started on http://%s:%s%s",
        settings.webhook_host,
        settings.webhook_port,
        settings.yookassa_webhook_path,
    )

    try:
        await run_polling_with_retries(
            dispatcher=dispatcher,
            bot=bot,
            drop_pending_updates=settings.polling_drop_pending_updates,
        )
    finally:
        await webhook_server.stop()
        await bot.session.close()
        await yookassa_client.close()
        await engine.dispose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()

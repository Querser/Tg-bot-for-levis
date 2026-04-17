from __future__ import annotations

from decimal import Decimal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _parse_ids(raw: str) -> list[int]:
    clean = raw.strip()
    if not clean:
        return []
    return [int(item.strip()) for item in clean.split(",") if item.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    telegram_bot_token: str = Field(alias="TELEGRAM_BOT_TOKEN")
    yookassa_shop_id: str = Field(alias="YOOKASSA_SHOP_ID")
    yookassa_secret_key: str = Field(alias="YOOKASSA_SECRET_KEY")

    # Legacy variable kept for backward compatibility.
    admin_telegram_ids_raw: str = Field(default="", alias="ADMIN_TELEGRAM_IDS")
    super_admin_telegram_ids_raw: str = Field(default="", alias="SUPER_ADMIN_TELEGRAM_IDS")
    ticket_admin_telegram_ids_raw: str = Field(default="", alias="TICKET_ADMIN_TELEGRAM_IDS")

    database_url: str = Field(default="sqlite+aiosqlite:///./bot.db", alias="DATABASE_URL")

    yookassa_api_base_url: str = Field(default="https://api.yookassa.ru/v3", alias="YOOKASSA_API_BASE_URL")
    yookassa_webhook_path: str = Field(default="/webhooks/yookassa", alias="YOOKASSA_WEBHOOK_PATH")

    webhook_host: str = Field(default="127.0.0.1", alias="WEBHOOK_HOST")
    webhook_port: int = Field(default=8080, alias="WEBHOOK_PORT")

    payment_amount_rub: Decimal = Field(default=Decimal("299.00"), alias="PAYMENT_AMOUNT_RUB")
    payment_description: str = Field(default="Оплата билета на мероприятие", alias="PAYMENT_DESCRIPTION")
    payment_return_url: str = Field(default="https://t.me", alias="PAYMENT_RETURN_URL")
    default_event_address: str = Field(
        default="Адрес пока не задан. Напишите администратору.",
        alias="EVENT_ADDRESS",
    )

    request_timeout_seconds: float = Field(default=10.0, alias="REQUEST_TIMEOUT_SECONDS")
    http_trust_env: bool = Field(default=True, alias="HTTP_TRUST_ENV")
    telegram_proxy_url: str = Field(default="", alias="TELEGRAM_PROXY_URL")
    yookassa_trust_env: bool = Field(default=False, alias="YOOKASSA_TRUST_ENV")
    polling_drop_pending_updates: bool = Field(default=True, alias="POLLING_DROP_PENDING_UPDATES")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @property
    def super_admin_ids(self) -> list[int]:
        source = self.super_admin_telegram_ids_raw or self.admin_telegram_ids_raw
        return _parse_ids(source)

    @property
    def ticket_admin_ids(self) -> list[int]:
        return _parse_ids(self.ticket_admin_telegram_ids_raw)

    @property
    def all_admin_ids(self) -> list[int]:
        return sorted(set(self.super_admin_ids + self.ticket_admin_ids))


def load_settings() -> Settings:
    return Settings()

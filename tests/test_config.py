from __future__ import annotations

from app.config import Settings


def test_settings_parse_legacy_admin_ids_from_string() -> None:
    settings = Settings(
        TELEGRAM_BOT_TOKEN="token",
        YOOKASSA_SHOP_ID="shop",
        YOOKASSA_SECRET_KEY="secret",
        ADMIN_TELEGRAM_IDS="1, 2,3",
        SUPER_ADMIN_TELEGRAM_IDS="",
        TICKET_ADMIN_TELEGRAM_IDS="",
    )
    assert settings.super_admin_ids == [1, 2, 3]
    assert settings.ticket_admin_ids == []
    assert settings.all_admin_ids == [1, 2, 3]


def test_settings_parse_split_admin_roles() -> None:
    settings = Settings(
        TELEGRAM_BOT_TOKEN="token",
        YOOKASSA_SHOP_ID="shop",
        YOOKASSA_SECRET_KEY="secret",
        ADMIN_TELEGRAM_IDS="",
        SUPER_ADMIN_TELEGRAM_IDS="10, 20",
        TICKET_ADMIN_TELEGRAM_IDS="30,20",
    )
    assert settings.super_admin_ids == [10, 20]
    assert settings.ticket_admin_ids == [30, 20]
    assert settings.all_admin_ids == [10, 20, 30]

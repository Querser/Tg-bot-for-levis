from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

BUY_TICKET_BUTTON_TEXT = "🎟 Купить билет"
CHECK_PAYMENT_BUTTON_TEXT = "🔄 Проверить оплату"
MY_TICKETS_BUTTON_TEXT = "🎫 Мои билеты"
HELP_BUTTON_TEXT = "ℹ️ Помощь"
ADMIN_PANEL_BUTTON_TEXT = "🛠 Админ панель"

BUY_TICKET_CALLBACK = "buy_ticket"
CHECK_PAYMENT_CALLBACK = "check_payment"
MY_TICKETS_CALLBACK = "my_tickets"
HELP_CALLBACK = "help"
ADMIN_EXPORT_CALLBACK = "admin_export_purchases"
ADMIN_CHECK_TICKET_CALLBACK = "admin_check_ticket"
ADMIN_SET_EVENT_ADDRESS_CALLBACK = "admin_set_event_address"
ADMIN_BROADCAST_CALLBACK = "admin_broadcast"
ADMIN_TICKET_SKIP_CALLBACK = "admin_ticket_skip"
ADMIN_TICKET_BACK_CALLBACK = "admin_ticket_back"


def main_menu_keyboard(is_admin: bool) -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(text=BUY_TICKET_BUTTON_TEXT)],
        [KeyboardButton(text=MY_TICKETS_BUTTON_TEXT)],
        [KeyboardButton(text=HELP_BUTTON_TEXT)],
    ]
    if is_admin:
        keyboard.append([KeyboardButton(text=ADMIN_PANEL_BUTTON_TEXT)])
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        input_field_placeholder="Выберите действие 👇",
    )


def main_actions_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=BUY_TICKET_BUTTON_TEXT, callback_data=BUY_TICKET_CALLBACK)],
            [InlineKeyboardButton(text=MY_TICKETS_BUTTON_TEXT, callback_data=MY_TICKETS_CALLBACK)],
            [InlineKeyboardButton(text=HELP_BUTTON_TEXT, callback_data=HELP_CALLBACK)],
        ]
    )


def payment_inline_keyboard(confirmation_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Перейти к оплате", url=confirmation_url)],
            [InlineKeyboardButton(text=CHECK_PAYMENT_BUTTON_TEXT, callback_data=CHECK_PAYMENT_CALLBACK)],
            [InlineKeyboardButton(text=MY_TICKETS_BUTTON_TEXT, callback_data=MY_TICKETS_CALLBACK)],
        ]
    )


def admin_panel_inline_keyboard(
    *,
    can_export: bool,
    can_set_event_address: bool,
    can_broadcast: bool,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if can_export:
        rows.append([InlineKeyboardButton(text="📤 Выгрузить покупки (CSV)", callback_data=ADMIN_EXPORT_CALLBACK)])
    rows.append([InlineKeyboardButton(text="✅ Проверить билеты", callback_data=ADMIN_CHECK_TICKET_CALLBACK)])
    if can_set_event_address:
        rows.append(
            [InlineKeyboardButton(text="📍 Изменить адрес мероприятия", callback_data=ADMIN_SET_EVENT_ADDRESS_CALLBACK)]
        )
    if can_broadcast:
        rows.append([InlineKeyboardButton(text="📣 Сделать рассылку", callback_data=ADMIN_BROADCAST_CALLBACK)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_ticket_check_inline_keyboard(*, can_skip: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if can_skip:
        rows.append([InlineKeyboardButton(text="✅ Пропустить", callback_data=ADMIN_TICKET_SKIP_CALLBACK)])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=ADMIN_TICKET_BACK_CALLBACK)])
    return InlineKeyboardMarkup(inline_keyboard=rows)

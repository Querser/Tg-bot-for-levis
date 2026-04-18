from __future__ import annotations

import asyncio
import csv
import io
import logging
import re
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message, User

from app.bot.keyboards import (
    ADMIN_BROADCAST_CALLBACK,
    ADMIN_CHECK_TICKET_CALLBACK,
    ADMIN_EXPORT_CALLBACK,
    ADMIN_PANEL_BUTTON_TEXT,
    ADMIN_SET_EVENT_ADDRESS_CALLBACK,
    ADMIN_SET_TICKET_PRICE_CALLBACK,
    ADMIN_TICKET_BACK_CALLBACK,
    ADMIN_TICKET_SKIP_CALLBACK,
    BUY_TICKET_BUTTON_TEXT,
    BUY_TICKET_CALLBACK,
    CHECK_PAYMENT_BUTTON_TEXT,
    CHECK_PAYMENT_CALLBACK,
    HELP_BUTTON_TEXT,
    HELP_CALLBACK,
    MY_TICKETS_BUTTON_TEXT,
    MY_TICKETS_CALLBACK,
    admin_panel_inline_keyboard,
    admin_ticket_check_inline_keyboard,
    main_actions_inline_keyboard,
    main_menu_keyboard,
    payment_inline_keyboard,
)
from app.domain import PaymentRecord, PaymentStatus
from app.services import EventSettingsService, EventSettingsValidationError
from app.services.payment_service import PaymentService, PaymentServiceError, PaymentValidationError

LOGGER = logging.getLogger(__name__)
PHONE_PATTERN = re.compile(r"^\+?[0-9\-\s\(\)]{10,20}$")


class PurchaseFormState(StatesGroup):
    waiting_full_name = State()
    waiting_age = State()
    waiting_phone = State()


class AdminTicketCheckState(StatesGroup):
    waiting_ticket_number = State()


class AdminEventAddressState(StatesGroup):
    waiting_event_address = State()


class AdminTicketPriceState(StatesGroup):
    waiting_ticket_price = State()


class AdminBroadcastState(StatesGroup):
    waiting_broadcast_text = State()


def register_handlers(
    router: Router,
    payment_service: PaymentService,
    event_settings_service: EventSettingsService,
    super_admin_ids: Iterable[int],
    ticket_admin_ids: Iterable[int],
    payment_amount_rub: Decimal = Decimal("299.00"),
    payment_description: str = "Оплата билета",
    payment_return_url: str = "https://t.me",
) -> None:
    buy_inflight: set[int] = set()
    super_admin_set = {int(x) for x in super_admin_ids}
    ticket_admin_set = {int(x) for x in ticket_admin_ids}

    def is_super_admin(user_id: int) -> bool:
        return user_id in super_admin_set

    def can_check_tickets(user_id: int) -> bool:
        return user_id in super_admin_set or user_id in ticket_admin_set

    async def remember_user(user: User | None) -> None:
        if user is None:
            return
        try:
            await event_settings_service.register_known_user(user.id)
        except Exception:
            LOGGER.exception("Failed to remember user %s", user.id)

    async def send_to_callback_origin(callback: CallbackQuery, text: str, reply_markup=None) -> None:
        if callback.message:
            await callback.message.answer(text, reply_markup=reply_markup)
            return
        await callback.bot.send_message(callback.from_user.id, text, reply_markup=reply_markup)

    async def send_admin_panel(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        if not can_check_tickets(user_id):
            await message.answer("⛔ Доступ только для админов.")
            return
        role = "🫅 Главный админ" if is_super_admin(user_id) else "🛡 Контролер билетов"
        event_address = await event_settings_service.get_event_address()
        await message.answer(
            "🛠 Админ-панель\n" f"Роль: {role}\n" f"📍 Адрес: {event_address}",
            reply_markup=admin_panel_inline_keyboard(
                can_export=is_super_admin(user_id),
                can_set_event_address=is_super_admin(user_id),
                can_set_ticket_price=is_super_admin(user_id),
                can_broadcast=is_super_admin(user_id),
            ),
        )

    async def handle_payment_state(payment: PaymentRecord | None, sender) -> None:
        if payment is None:
            await sender("😕 Активных оплат пока нет. Нажмите «Купить билет».", reply_markup=main_actions_inline_keyboard())
            return
        if payment.status in {PaymentStatus.PENDING, PaymentStatus.WAITING_FOR_CAPTURE}:
            if payment.confirmation_url:
                await sender(
                    "💳 Оплата еще в процессе.\n"
                    f"Сумма: {payment.amount_rub} RUB\n"
                    f"Ссылка: {payment.confirmation_url}\n\n"
                    "После оплаты проверьте «Мои билеты» 🎫",
                    reply_markup=payment_inline_keyboard(payment.confirmation_url),
                )
            else:
                await sender("⌛ Платеж есть, но без ссылки. Нажмите «Купить билет» еще раз.", reply_markup=main_actions_inline_keyboard())
            return
        if payment.status == PaymentStatus.SUCCEEDED:
            p = await payment_service.ensure_ticket_for_payment(payment)
            event_address = await event_settings_service.get_event_address()
            emoji = "✅" if p.ticket_valid else "❌"
            status = "активен" if p.ticket_valid else "использован"
            await sender(
                "🎉 Оплата подтверждена!\n"
                f"🎟 Билет: {p.ticket_number}\n"
                f"Статус: {emoji} {status}\n"
                f"📍 Адрес: {event_address}",
                reply_markup=main_actions_inline_keyboard(),
            )
            return
        await sender("⚠️ Оплата не завершена. Можно создать новую попытку.", reply_markup=main_actions_inline_keyboard())

    async def start_profile_collection(user: User, state: FSMContext, sender) -> None:
        await state.clear()
        await state.set_state(PurchaseFormState.waiting_full_name)
        await sender(
            "🎟 Перед оплатой заполните данные.\nШаг 1/3: отправьте ФИО.",
            reply_markup=main_menu_keyboard(is_admin=can_check_tickets(user.id)),
        )

    async def attempt_payment_creation_with_profile(user: User, sender, state: FSMContext) -> None:
        if user.id in buy_inflight:
            await sender("⏳ Уже создаю платеж, подождите пару секунд.")
            return
        data = await state.get_data()
        full_name = str(data.get("full_name", "")).strip()
        phone = str(data.get("phone", "")).strip()
        age_raw = data.get("age")
        if not full_name or not phone or age_raw is None:
            await sender("⚠️ Не хватает данных анкеты. Нажмите «Купить билет» и заполните заново.", reply_markup=main_actions_inline_keyboard())
            return

        try:
            ticket_price_rub = await event_settings_service.get_ticket_price_rub()
        except Exception:
            LOGGER.exception("Failed to load ticket price from settings. Payment creation canceled.")
            await sender("⚠️ Не удалось получить актуальную цену билета. Попробуйте еще раз.")
            return

        buy_inflight.add(user.id)
        try:
            payment = await payment_service.create_payment(
                telegram_user_id=user.id,
                full_name=full_name,
                age=int(age_raw),
                phone=phone,
                amount_rub=ticket_price_rub,
                description=payment_description,
                metadata={"source": "telegram_bot", "product": "event_ticket", "full_name": full_name, "age": str(age_raw), "phone": phone},
                idempotency_key=f"tg-{user.id}-{uuid4().hex}",
                return_url=payment_return_url,
            )
            await state.clear()
            await handle_payment_state(payment, sender)
        except PaymentServiceError:
            LOGGER.exception("Payment service error user=%s", user.id)
            await sender("😵 Сервис оплаты временно недоступен.")
        except Exception:
            LOGGER.exception("Unexpected payment creation error user=%s", user.id)
            await sender("😬 Что-то пошло не так, попробуйте позже.")
        finally:
            buy_inflight.discard(user.id)

    async def process_buy(user: User, sender, state: FSMContext) -> None:
        latest = await payment_service.refresh_latest_user_payment(user.id)
        if latest and latest.status in {PaymentStatus.PENDING, PaymentStatus.WAITING_FOR_CAPTURE}:
            refreshed = latest
            if latest.yookassa_payment_id:
                candidate = await payment_service.refresh_payment_status(latest.yookassa_payment_id)
                if candidate is not None:
                    refreshed = candidate
            if refreshed.status == PaymentStatus.SUCCEEDED:
                await handle_payment_state(refreshed, sender)
                return
            if refreshed.full_name and refreshed.phone and refreshed.age is not None:
                await state.update_data(full_name=refreshed.full_name, age=refreshed.age, phone=refreshed.phone)
                await attempt_payment_creation_with_profile(user, sender, state)
                return
        if latest and latest.status == PaymentStatus.SUCCEEDED:
            await sender("🙂 У вас уже есть оплаченный билет, но можно купить еще один.")
        await start_profile_collection(user, state, sender)

    async def process_check_payment(user: User, sender, state: FSMContext) -> None:
        if await state.get_state() in {
            PurchaseFormState.waiting_full_name.state,
            PurchaseFormState.waiting_age.state,
            PurchaseFormState.waiting_phone.state,
        }:
            await sender("✌️ Сначала завершите анкету.")
            return
        payment = await payment_service.refresh_latest_user_payment(user.id)
        if payment and payment.status in {PaymentStatus.PENDING, PaymentStatus.WAITING_FOR_CAPTURE} and not payment.confirmation_url:
            await sender("⌛ У текущего платежа нет ссылки. Нажмите «Купить билет», создам новый.")
            return
        await handle_payment_state(payment, sender)

    async def process_my_tickets(user: User, sender, state: FSMContext) -> None:
        if await state.get_state() in {
            PurchaseFormState.waiting_full_name.state,
            PurchaseFormState.waiting_age.state,
            PurchaseFormState.waiting_phone.state,
        }:
            await sender("✌️ Сначала завершите анкету.")
            return
        latest = await payment_service.refresh_latest_user_payment(user.id)
        tickets = await payment_service.list_user_tickets(user.id)
        event_address = await event_settings_service.get_event_address()
        if not tickets:
            if latest and latest.status in {PaymentStatus.PENDING, PaymentStatus.WAITING_FOR_CAPTURE}:
                if latest.confirmation_url:
                    await sender(
                        "⌛ Оплата обрабатывается, билет появится здесь.\n"
                        f"Ссылка: {latest.confirmation_url}",
                        reply_markup=payment_inline_keyboard(latest.confirmation_url),
                    )
                    return
                await sender("⌛ Платеж есть, но без ссылки. Нажмите «Купить билет».")
                return
            await sender("🎫 Пока билетов нет. Нажмите «Купить билет».", reply_markup=main_actions_inline_keyboard())
            return
        lines = ["🎫 Ваши билеты:"]
        for t in tickets:
            emoji = "✅" if t.ticket_valid else "❌"
            status = "активен" if t.ticket_valid else "неактивен"
            lines.append(f"• {t.ticket_number} {emoji} {status}")
        lines.append(f"\n📍 Адрес: {event_address}")
        await sender("\n".join(lines), reply_markup=main_actions_inline_keyboard())

    def build_purchases_csv(purchases: list[PaymentRecord]) -> bytes:
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=";")
        writer.writerow(["ID", "TelegramUserID", "FullName", "Age", "Phone", "Amount", "Currency", "PaymentStatus", "TicketNumber", "TicketValid", "TicketUsedAtUTC", "YooKassaPaymentID", "PaymentLink", "CreatedUTC"])
        for p in purchases:
            writer.writerow([p.local_id, p.telegram_user_id, p.full_name or "", p.age if p.age is not None else "", p.phone or "", str(p.amount_rub), p.currency, p.status.value, p.ticket_number or "", "yes" if p.ticket_valid else "no", p.ticket_used_at.isoformat() if p.ticket_used_at else "", p.yookassa_payment_id or "", p.confirmation_url or "", p.created_at.isoformat()])
        return buffer.getvalue().encode("utf-8-sig")

    async def collect_broadcast_recipients(sender_user_id: int) -> list[int]:
        ids = set(await event_settings_service.list_known_user_ids())
        ids.update(item.telegram_user_id for item in await payment_service.list_purchases())
        ids.discard(sender_user_id)
        return sorted(ids)

    @router.message(CommandStart())
    async def on_start(message: Message) -> None:
        await remember_user(message.from_user)
        user = message.from_user
        name = user.first_name if user else "друг"
        await message.answer(
            f"Привет, {name}! 👋\nЯ помогу оформить билет.\nВыберите действие 👇",
            reply_markup=main_menu_keyboard(is_admin=can_check_tickets(user.id if user else 0)),
        )

    @router.message(Command("help"))
    async def on_help(message: Message) -> None:
        await remember_user(message.from_user)
        user_id = message.from_user.id if message.from_user else 0
        await message.answer(
            "ℹ️ Как это работает:\n1) «Купить билет»\n2) ФИО, возраст, телефон\n3) Оплата YooKassa\n4) Билет в «Мои билеты»\n\nМероприятие 18+ 🔞",
            reply_markup=main_menu_keyboard(is_admin=can_check_tickets(user_id)),
        )

    @router.message(Command("admin"))
    async def on_admin_command(message: Message) -> None:
        await remember_user(message.from_user)
        await send_admin_panel(message)

    @router.message(F.text.casefold() == ADMIN_PANEL_BUTTON_TEXT.casefold())
    async def on_admin_button(message: Message) -> None:
        await remember_user(message.from_user)
        await send_admin_panel(message)

    @router.message(F.text.casefold() == BUY_TICKET_BUTTON_TEXT.casefold())
    async def on_buy_message(message: Message, state: FSMContext) -> None:
        await remember_user(message.from_user)
        if message.from_user:
            await process_buy(message.from_user, message.answer, state)

    @router.message(F.text.casefold() == CHECK_PAYMENT_BUTTON_TEXT.casefold())
    async def on_check_payment_message(message: Message, state: FSMContext) -> None:
        await remember_user(message.from_user)
        if message.from_user:
            await process_check_payment(message.from_user, message.answer, state)

    @router.message(F.text.casefold() == MY_TICKETS_BUTTON_TEXT.casefold())
    async def on_my_tickets_message(message: Message, state: FSMContext) -> None:
        await remember_user(message.from_user)
        if message.from_user:
            await process_my_tickets(message.from_user, message.answer, state)

    @router.message(F.text.casefold() == HELP_BUTTON_TEXT.casefold())
    async def on_help_message(message: Message) -> None:
        await on_help(message)

    @router.callback_query(F.data == BUY_TICKET_CALLBACK)
    async def on_buy_callback(callback: CallbackQuery, state: FSMContext) -> None:
        await remember_user(callback.from_user)
        await callback.answer()
        await process_buy(callback.from_user, lambda text, **kwargs: send_to_callback_origin(callback, text, **kwargs), state)

    @router.callback_query(F.data == CHECK_PAYMENT_CALLBACK)
    async def on_check_payment_callback(callback: CallbackQuery, state: FSMContext) -> None:
        await remember_user(callback.from_user)
        await callback.answer()
        await process_check_payment(callback.from_user, lambda text, **kwargs: send_to_callback_origin(callback, text, **kwargs), state)

    @router.callback_query(F.data == MY_TICKETS_CALLBACK)
    async def on_my_tickets_callback(callback: CallbackQuery, state: FSMContext) -> None:
        await remember_user(callback.from_user)
        await callback.answer()
        await process_my_tickets(callback.from_user, lambda text, **kwargs: send_to_callback_origin(callback, text, **kwargs), state)

    @router.callback_query(F.data == HELP_CALLBACK)
    async def on_help_callback(callback: CallbackQuery) -> None:
        await remember_user(callback.from_user)
        await callback.answer()
        await send_to_callback_origin(callback, "Нужна помощь? Напишите администратору 🙌\nМероприятие строго 18+ 🔞", reply_markup=main_actions_inline_keyboard())

    @router.callback_query(F.data == ADMIN_EXPORT_CALLBACK)
    async def on_admin_export_callback(callback: CallbackQuery) -> None:
        await remember_user(callback.from_user)
        user_id = callback.from_user.id
        if not is_super_admin(user_id):
            await callback.answer("Недостаточно прав", show_alert=True)
            return
        await callback.answer("Формирую выгрузку…")
        purchases = await payment_service.list_purchases()
        if not purchases:
            await send_to_callback_origin(callback, "Пока нет покупок 📭")
            return
        filename = f"purchases_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        document = BufferedInputFile(build_purchases_csv(purchases), filename=filename)
        if callback.message is not None:
            await callback.message.answer_document(document=document, caption="Готово ✅")
            return
        await callback.bot.send_document(chat_id=user_id, document=document, caption="Готово ✅")

    @router.callback_query(F.data == ADMIN_BROADCAST_CALLBACK)
    async def on_admin_broadcast_callback(callback: CallbackQuery, state: FSMContext) -> None:
        await remember_user(callback.from_user)
        user_id = callback.from_user.id
        if not is_super_admin(user_id):
            await callback.answer("Недостаточно прав", show_alert=True)
            return
        await callback.answer()
        await state.set_state(AdminBroadcastState.waiting_broadcast_text)
        await send_to_callback_origin(callback, "📣 Отправьте текст рассылки одним сообщением.")

    @router.message(AdminBroadcastState.waiting_broadcast_text)
    async def on_admin_broadcast_message(message: Message, state: FSMContext) -> None:
        await remember_user(message.from_user)
        user_id = message.from_user.id if message.from_user else 0
        if not is_super_admin(user_id):
            await state.clear()
            await message.answer("⛔ Недостаточно прав.")
            return
        text = (message.text or "").strip()
        if not text:
            await message.answer("Текст пустой. Пришлите сообщение для рассылки.")
            return
        recipients = await collect_broadcast_recipients(user_id)
        if not recipients:
            await state.clear()
            await message.answer("Пока нет пользователей для рассылки 📭")
            return
        delivered, failed = 0, 0
        payload = f"📣 Сообщение от организаторов:\n\n{text}"
        for idx, recipient_id in enumerate(recipients, start=1):
            try:
                await message.bot.send_message(chat_id=recipient_id, text=payload)
                delivered += 1
            except Exception:
                failed += 1
                LOGGER.warning("Broadcast failed user=%s", recipient_id, exc_info=True)
            if idx % 20 == 0:
                await asyncio.sleep(0.2)
        await state.clear()
        await message.answer(
            f"Рассылка завершена ✅\nДоставлено: {delivered}\nНе доставлено: {failed}",
            reply_markup=main_menu_keyboard(is_admin=can_check_tickets(user_id)),
        )

    @router.callback_query(F.data == ADMIN_CHECK_TICKET_CALLBACK)
    async def on_admin_check_ticket_callback(callback: CallbackQuery, state: FSMContext) -> None:
        await remember_user(callback.from_user)
        user_id = callback.from_user.id
        if not can_check_tickets(user_id):
            await callback.answer("Недостаточно прав", show_alert=True)
            return
        await callback.answer()
        await state.set_state(AdminTicketCheckState.waiting_ticket_number)
        await state.update_data(pending_ticket_number="")
        await send_to_callback_origin(
            callback,
            "🎫 Режим проверки билетов включен.\nВводите 3-значный номер билета.",
            reply_markup=admin_ticket_check_inline_keyboard(can_skip=False),
        )

    @router.callback_query(F.data == ADMIN_TICKET_SKIP_CALLBACK)
    async def on_admin_ticket_skip_callback(callback: CallbackQuery, state: FSMContext) -> None:
        await remember_user(callback.from_user)
        user_id = callback.from_user.id
        if not can_check_tickets(user_id):
            await callback.answer("Недостаточно прав", show_alert=True)
            return
        pending_ticket_number = str((await state.get_data()).get("pending_ticket_number", "")).strip()
        if not pending_ticket_number:
            await callback.answer("Сначала проверьте билет", show_alert=True)
            return
        await callback.answer("Отмечаю билет…")
        result = await payment_service.check_and_consume_ticket(pending_ticket_number)
        await state.update_data(pending_ticket_number="")
        if result.status == "valid_consumed" and result.payment is not None:
            await send_to_callback_origin(
                callback,
                f"✅ Билет {result.payment.ticket_number} отмечен как использованный.\nВведите следующий номер.",
                reply_markup=admin_ticket_check_inline_keyboard(can_skip=False),
            )
            return
        if result.status == "already_used":
            text = "❌ Билет уже использован."
        elif result.status == "not_found":
            text = "❌ Билет не найден."
        elif result.status == "not_paid":
            text = "❌ Оплата по билету не подтверждена."
        else:
            text = "⚠️ Не удалось отметить билет."
        await send_to_callback_origin(callback, f"{text}\nВведите следующий номер.", reply_markup=admin_ticket_check_inline_keyboard(can_skip=False))

    @router.callback_query(F.data == ADMIN_TICKET_BACK_CALLBACK)
    async def on_admin_ticket_back_callback(callback: CallbackQuery, state: FSMContext) -> None:
        await remember_user(callback.from_user)
        user_id = callback.from_user.id
        if not can_check_tickets(user_id):
            await callback.answer("Недостаточно прав", show_alert=True)
            return
        await callback.answer()
        await state.clear()
        await send_to_callback_origin(callback, "↩️ Возвращаю в главное меню.", reply_markup=main_menu_keyboard(is_admin=can_check_tickets(user_id)))

    @router.callback_query(F.data == ADMIN_SET_EVENT_ADDRESS_CALLBACK)
    async def on_admin_set_event_address_callback(callback: CallbackQuery, state: FSMContext) -> None:
        await remember_user(callback.from_user)
        user_id = callback.from_user.id
        if not is_super_admin(user_id):
            await callback.answer("Недостаточно прав", show_alert=True)
            return
        await callback.answer()
        await state.set_state(AdminEventAddressState.waiting_event_address)
        await send_to_callback_origin(callback, "Введите новый адрес мероприятия 📍")

    @router.callback_query(F.data == ADMIN_SET_TICKET_PRICE_CALLBACK)
    async def on_admin_set_ticket_price_callback(callback: CallbackQuery, state: FSMContext) -> None:
        await remember_user(callback.from_user)
        user_id = callback.from_user.id
        if not is_super_admin(user_id):
            await callback.answer("Недостаточно прав", show_alert=True)
            return
        current_price = await event_settings_service.get_ticket_price_rub()
        await callback.answer()
        await state.set_state(AdminTicketPriceState.waiting_ticket_price)
        await send_to_callback_origin(
            callback,
            f"Текущая цена: {current_price} RUB\n"
            "Введите новую цену билета (например, 299 или 299.50).",
        )

    @router.message(AdminEventAddressState.waiting_event_address)
    async def on_admin_event_address(message: Message, state: FSMContext) -> None:
        await remember_user(message.from_user)
        user_id = message.from_user.id if message.from_user else 0
        if not is_super_admin(user_id):
            await state.clear()
            await message.answer("⛔ Недостаточно прав.")
            return
        value = (message.text or "").strip()
        if len(value) < 5:
            await message.answer("Слишком коротко. Введите нормальный адрес 🙏")
            return
        await event_settings_service.set_event_address(value)
        await state.clear()
        await message.answer(f"✅ Адрес сохранен:\n📍 {value}")

    @router.message(AdminTicketPriceState.waiting_ticket_price)
    async def on_admin_ticket_price(message: Message, state: FSMContext) -> None:
        await remember_user(message.from_user)
        user_id = message.from_user.id if message.from_user else 0
        if not is_super_admin(user_id):
            await state.clear()
            await message.answer("⛔ Недостаточно прав.")
            return

        raw_value = (message.text or "").strip()
        try:
            new_price = await event_settings_service.set_ticket_price_rub(raw_value)
        except EventSettingsValidationError:
            await message.answer(
                "Неверный формат цены. Пример: 299 или 299.50\n"
                "Цена должна быть больше нуля."
            )
            return

        await state.clear()
        await message.answer(f"✅ Новая цена билета: {new_price} RUB")

    @router.message(AdminTicketCheckState.waiting_ticket_number)
    async def on_admin_ticket_number(message: Message, state: FSMContext) -> None:
        await remember_user(message.from_user)
        user_id = message.from_user.id if message.from_user else 0
        if not can_check_tickets(user_id):
            await state.clear()
            await message.answer("⛔ Недостаточно прав.")
            return

        pending = str((await state.get_data()).get("pending_ticket_number", "")).strip()
        if pending:
            await message.answer(
                "Сначала нажмите «Пропустить» для текущего билета или «Назад».",
                reply_markup=admin_ticket_check_inline_keyboard(can_skip=True),
            )
            return

        try:
            result = await payment_service.check_ticket((message.text or "").strip())
        except PaymentValidationError:
            await message.answer("Нужны ровно 3 цифры. Пример: 123", reply_markup=admin_ticket_check_inline_keyboard(can_skip=False))
            return

        if result.status == "not_found":
            await message.answer("❌ Билет недействителен: номер не найден.\nВведите следующий номер.", reply_markup=admin_ticket_check_inline_keyboard(can_skip=False))
            return
        if result.status == "not_paid":
            await message.answer("❌ Билет недействителен: оплата не подтверждена.\nВведите следующий номер.", reply_markup=admin_ticket_check_inline_keyboard(can_skip=False))
            return
        if result.status == "already_used":
            await message.answer("❌ Билет уже использован.\nВведите следующий номер.", reply_markup=admin_ticket_check_inline_keyboard(can_skip=False))
            return
        if result.status == "valid" and result.payment and result.payment.ticket_number:
            await state.update_data(pending_ticket_number=result.payment.ticket_number)
            await message.answer(
                "✅ Билет действителен.\n"
                f"Номер: {result.payment.ticket_number}\n"
                f"Покупатель: {result.payment.full_name or '—'}\n"
                f"Телефон: {result.payment.phone or '—'}\n\n"
                "Нажмите «Пропустить», чтобы отметить его как использованный.",
                reply_markup=admin_ticket_check_inline_keyboard(can_skip=True),
            )
            return
        await message.answer("⚠️ Не удалось обработать билет. Введите следующий номер.", reply_markup=admin_ticket_check_inline_keyboard(can_skip=False))

    @router.message(PurchaseFormState.waiting_full_name)
    async def on_waiting_full_name(message: Message, state: FSMContext) -> None:
        await remember_user(message.from_user)
        value = (message.text or "").strip()
        if len(value) < 5:
            await message.answer("ФИО слишком короткое. Введите полное ФИО 🙏")
            return
        await state.update_data(full_name=value)
        await state.set_state(PurchaseFormState.waiting_age)
        await message.answer("Шаг 2/3: введите возраст цифрами 🔞")

    @router.message(PurchaseFormState.waiting_age)
    async def on_waiting_age(message: Message, state: FSMContext) -> None:
        await remember_user(message.from_user)
        value = (message.text or "").strip()
        if not value.isdigit():
            await message.answer("Возраст нужен цифрами. Пример: 24")
            return
        age = int(value)
        if age < 18:
            await message.answer("🚫 На это мероприятие только 18+.\nПродолжить оформление нельзя.")
            await state.clear()
            return
        if age > 120:
            await message.answer("Проверьте возраст, выглядит нереально 😅")
            return
        await state.update_data(age=age)
        await state.set_state(PurchaseFormState.waiting_phone)
        await message.answer("Шаг 3/3: отправьте номер телефона 📱 (например, +79991234567)")

    @router.message(PurchaseFormState.waiting_phone)
    async def on_waiting_phone(message: Message, state: FSMContext) -> None:
        await remember_user(message.from_user)
        value = (message.text or "").strip()
        if not PHONE_PATTERN.match(value):
            await message.answer("Формат телефона не подошел. Пример: +79991234567")
            return
        await state.update_data(phone=re.sub(r"\s+", "", value))
        if message.from_user is None:
            await message.answer("Не удалось определить пользователя. Нажмите /start")
            return
        await attempt_payment_creation_with_profile(message.from_user, message.answer, state)

    @router.message(F.text & ~F.text.startswith("/"))
    async def on_fallback(message: Message) -> None:
        await remember_user(message.from_user)
        user_id = message.from_user.id if message.from_user else 0
        await message.answer("🤖 Давайте по кнопкам, так надежнее 😌", reply_markup=main_menu_keyboard(is_admin=can_check_tickets(user_id)))

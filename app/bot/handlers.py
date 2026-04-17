from __future__ import annotations

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
    ADMIN_CHECK_TICKET_CALLBACK,
    ADMIN_EXPORT_CALLBACK,
    ADMIN_PANEL_BUTTON_TEXT,
    ADMIN_SET_EVENT_ADDRESS_CALLBACK,
    BUY_TICKET_BUTTON_TEXT,
    BUY_TICKET_CALLBACK,
    CHECK_PAYMENT_BUTTON_TEXT,
    CHECK_PAYMENT_CALLBACK,
    HELP_BUTTON_TEXT,
    HELP_CALLBACK,
    MY_TICKETS_BUTTON_TEXT,
    MY_TICKETS_CALLBACK,
    admin_panel_inline_keyboard,
    main_actions_inline_keyboard,
    main_menu_keyboard,
    payment_inline_keyboard,
)
from app.domain import PaymentRecord, PaymentStatus
from app.services import EventSettingsService
from app.services.payment_service import (
    PaymentService,
    PaymentServiceError,
    PaymentValidationError,
)

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
    super_admin_set = {int(admin_id) for admin_id in super_admin_ids}
    ticket_admin_set = {int(admin_id) for admin_id in ticket_admin_ids}

    def is_super_admin(user_id: int) -> bool:
        return user_id in super_admin_set

    def can_check_tickets(user_id: int) -> bool:
        return user_id in super_admin_set or user_id in ticket_admin_set

    async def send_to_callback_origin(
        callback: CallbackQuery,
        text: str,
        reply_markup=None,
    ) -> None:
        if callback.message:
            await callback.message.answer(text, reply_markup=reply_markup)
            return
        await callback.bot.send_message(callback.from_user.id, text, reply_markup=reply_markup)

    async def send_admin_panel(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        if not can_check_tickets(user_id):
            await message.answer("⛔ Доступ только для администраторов.")
            return

        event_address = await event_settings_service.get_event_address()
        role_text = "👑 Главный админ" if is_super_admin(user_id) else "🛡 Контролер билетов"
        await message.answer(
            "🛠 Админ-панель\n"
            f"Роль: {role_text}\n"
            f"📍 Адрес мероприятия: {event_address}",
            reply_markup=admin_panel_inline_keyboard(
                can_export=is_super_admin(user_id),
                can_set_event_address=is_super_admin(user_id),
            ),
        )

    async def handle_payment_state(
        payment: PaymentRecord | None,
        sender,
    ) -> None:
        if payment is None:
            await sender(
                "😕 Пока не вижу активных оплат. Нажмите «Купить билет», и начнем.",
                reply_markup=main_actions_inline_keyboard(),
            )
            return

        if payment.status in {PaymentStatus.PENDING, PaymentStatus.WAITING_FOR_CAPTURE}:
            if payment.confirmation_url:
                await sender(
                    "💳 Оплата ещё в процессе.\n"
                    f"Ссылка на оплату: {payment.confirmation_url}\n\n"
                    "После оплаты загляните в «Мои билеты» 🎫",
                    reply_markup=payment_inline_keyboard(payment.confirmation_url),
                )
            else:
                await sender(
                    "⌛ Платеж создан, но ссылка еще не пришла. Нажмите «Купить билет» повторно — создам новый.",
                    reply_markup=main_actions_inline_keyboard(),
                )
            return

        if payment.status == PaymentStatus.SUCCEEDED:
            payment_with_ticket = await payment_service.ensure_ticket_for_payment(payment)
            event_address = await event_settings_service.get_event_address()
            ticket_emoji = "✅" if payment_with_ticket.ticket_valid else "❌"
            ticket_status = "активен" if payment_with_ticket.ticket_valid else "уже использован"
            await sender(
                "🎉 Оплата подтверждена!\n"
                f"🎟 Номер билета: {payment_with_ticket.ticket_number}\n"
                f"Статус: {ticket_emoji} {ticket_status}\n"
                f"📍 Адрес: {event_address}\n\n"
                "Покажите номер билета на входе 🙌",
                reply_markup=main_actions_inline_keyboard(),
            )
            return

        await sender(
            "⚠️ Оплата не завершена. Можно создать новую попытку через «Купить билет».",
            reply_markup=main_actions_inline_keyboard(),
        )

    async def start_profile_collection(user: User, state: FSMContext, sender) -> None:
        await state.clear()
        await state.set_state(PurchaseFormState.waiting_full_name)
        await sender(
            "🎟 Перед оплатой заполните данные.\n"
            "Шаг 1/3: отправьте ФИО.",
            reply_markup=main_menu_keyboard(is_admin=can_check_tickets(user.id)),
        )

    async def attempt_payment_creation_with_profile(user: User, sender, state: FSMContext) -> None:
        if user.id in buy_inflight:
            await sender("⏳ Уже создаю платеж, подождите пару секунд.")
            return

        form_data = await state.get_data()
        full_name = str(form_data.get("full_name", "")).strip()
        phone = str(form_data.get("phone", "")).strip()
        age_raw = form_data.get("age")

        if not full_name or not phone or age_raw is None:
            await sender(
                "⚠️ Не хватает данных анкеты. Нажмите «Купить билет» и заполните форму заново.",
                reply_markup=main_actions_inline_keyboard(),
            )
            return

        age = int(age_raw)
        buy_inflight.add(user.id)
        try:
            metadata = {
                "source": "telegram_bot",
                "product": "event_ticket",
                "full_name": full_name,
                "age": str(age),
                "phone": phone,
            }
            payment = await payment_service.create_payment(
                telegram_user_id=user.id,
                full_name=full_name,
                age=age,
                phone=phone,
                amount_rub=payment_amount_rub,
                description=payment_description,
                metadata=metadata,
                idempotency_key=f"tg-{user.id}-{uuid4().hex}",
                return_url=payment_return_url,
            )
            await state.clear()
            await handle_payment_state(payment, sender)
        except PaymentServiceError:
            LOGGER.exception("Payment service error for user_id=%s", user.id)
            await sender("😵 Сервис оплаты временно недоступен. Попробуйте чуть позже.")
        except Exception:
            LOGGER.exception("Unexpected payment creation error for user_id=%s", user.id)
            await sender("😬 Что-то пошло не так. Попробуйте снова через минуту.")
        finally:
            buy_inflight.discard(user.id)

    async def process_buy(user: User, sender, state: FSMContext) -> None:
        latest = await payment_service.refresh_latest_user_payment(user.id)
        if latest is not None and latest.status in {PaymentStatus.PENDING, PaymentStatus.WAITING_FOR_CAPTURE}:
            if latest.confirmation_url:
                await handle_payment_state(latest, sender)
                return

            refreshed = latest
            if latest.yookassa_payment_id:
                refreshed_candidate = await payment_service.refresh_payment_status(latest.yookassa_payment_id)
                if refreshed_candidate is not None:
                    refreshed = refreshed_candidate
                if refreshed.confirmation_url:
                    await handle_payment_state(refreshed, sender)
                    return

            if refreshed.full_name and refreshed.phone and refreshed.age is not None:
                await state.update_data(
                    full_name=refreshed.full_name,
                    age=refreshed.age,
                    phone=refreshed.phone,
                )
                await attempt_payment_creation_with_profile(user, sender, state)
                return

        if latest is not None and latest.status == PaymentStatus.SUCCEEDED:
            await sender("🙂 У вас уже есть оплаченный билет. Если хотите, можно купить ещё один.")
        await start_profile_collection(user, state, sender)

    async def process_check_payment(user: User, sender, state: FSMContext) -> None:
        current_state = await state.get_state()
        if current_state in {
            PurchaseFormState.waiting_full_name.state,
            PurchaseFormState.waiting_age.state,
            PurchaseFormState.waiting_phone.state,
        }:
            await sender("✍️ Сначала завершите анкету, потом проверим оплату.")
            return

        payment = await payment_service.refresh_latest_user_payment(user.id)
        if (
            payment is not None
            and payment.status in {PaymentStatus.PENDING, PaymentStatus.WAITING_FOR_CAPTURE}
            and not payment.confirmation_url
        ):
            await sender("⌛ У текущего платежа нет ссылки. Нажмите «Купить билет», создам новый платеж.")
            return
        await handle_payment_state(payment, sender)

    async def process_my_tickets(user: User, sender, state: FSMContext) -> None:
        current_state = await state.get_state()
        if current_state in {
            PurchaseFormState.waiting_full_name.state,
            PurchaseFormState.waiting_age.state,
            PurchaseFormState.waiting_phone.state,
        }:
            await sender("✍️ Сначала завершите анкету, потом покажу билеты.")
            return

        latest = await payment_service.refresh_latest_user_payment(user.id)
        tickets = await payment_service.list_user_tickets(user.id)
        event_address = await event_settings_service.get_event_address()

        if not tickets:
            if latest is not None and latest.status in {PaymentStatus.PENDING, PaymentStatus.WAITING_FOR_CAPTURE}:
                if latest.confirmation_url:
                    await sender(
                        "⌛ Оплата еще обрабатывается. Как только пройдет, билет появится здесь.\n"
                        f"Ссылка на оплату: {latest.confirmation_url}",
                        reply_markup=payment_inline_keyboard(latest.confirmation_url),
                    )
                    return
                await sender("⌛ Платеж есть, но без ссылки. Нажмите «Купить билет», создам новый.")
                return

            await sender(
                "🎫 Пока билетов нет.\n"
                "Нажмите «Купить билет», и всё оформим 🚀",
                reply_markup=main_actions_inline_keyboard(),
            )
            return

        lines = ["🎫 Ваши билеты:"]
        for ticket in tickets:
            status_emoji = "✅" if ticket.ticket_valid else "❌"
            status_text = "активен" if ticket.ticket_valid else "неактивен"
            lines.append(f"• {ticket.ticket_number} {status_emoji} {status_text}")
        lines.append(f"\n📍 Адрес мероприятия: {event_address}")
        await sender("\n".join(lines), reply_markup=main_actions_inline_keyboard())

    def build_purchases_csv(purchases: list[PaymentRecord]) -> bytes:
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=";")
        writer.writerow(
            [
                "ID",
                "TelegramUserID",
                "FullName",
                "Age",
                "Phone",
                "Amount",
                "Currency",
                "PaymentStatus",
                "TicketNumber",
                "TicketValid",
                "TicketUsedAtUTC",
                "YooKassaPaymentID",
                "PaymentLink",
                "CreatedUTC",
            ]
        )
        for payment in purchases:
            writer.writerow(
                [
                    payment.local_id,
                    payment.telegram_user_id,
                    payment.full_name or "",
                    payment.age if payment.age is not None else "",
                    payment.phone or "",
                    str(payment.amount_rub),
                    payment.currency,
                    payment.status.value,
                    payment.ticket_number or "",
                    "yes" if payment.ticket_valid else "no",
                    payment.ticket_used_at.isoformat() if payment.ticket_used_at else "",
                    payment.yookassa_payment_id or "",
                    payment.confirmation_url or "",
                    payment.created_at.isoformat(),
                ]
            )
        return buffer.getvalue().encode("utf-8-sig")

    @router.message(CommandStart())
    async def on_start(message: Message) -> None:
        user = message.from_user
        username = user.first_name if user else "друг"
        await message.answer(
            f"Привет, {username}! 👋\n"
            "Я помогу оформить билет на мероприятие.\n"
            "Выбирайте действие ниже 👇",
            reply_markup=main_menu_keyboard(is_admin=can_check_tickets(user.id if user else 0)),
        )

    @router.message(Command("help"))
    async def on_help(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        await message.answer(
            "ℹ️ Как это работает:\n"
            "1) Нажмите «Купить билет» 🎟\n"
            "2) Введите ФИО, возраст и телефон\n"
            "3) Оплатите по ссылке YooKassa 💳\n"
            "4) Билет появится в разделе «Мои билеты» 🎫\n\n"
            "Важно: мероприятие 18+ 🔞",
            reply_markup=main_menu_keyboard(is_admin=can_check_tickets(user_id)),
        )

    @router.message(Command("admin"))
    async def on_admin_command(message: Message) -> None:
        await send_admin_panel(message)

    @router.message(F.text.casefold() == ADMIN_PANEL_BUTTON_TEXT.casefold())
    async def on_admin_button(message: Message) -> None:
        await send_admin_panel(message)

    @router.message(F.text.casefold() == BUY_TICKET_BUTTON_TEXT.casefold())
    async def on_buy_message(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        await process_buy(message.from_user, message.answer, state)

    @router.message(F.text.casefold() == CHECK_PAYMENT_BUTTON_TEXT.casefold())
    async def on_check_payment_message(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        await process_check_payment(message.from_user, message.answer, state)

    @router.message(F.text.casefold() == MY_TICKETS_BUTTON_TEXT.casefold())
    async def on_my_tickets_message(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        await process_my_tickets(message.from_user, message.answer, state)

    @router.message(F.text.casefold() == HELP_BUTTON_TEXT.casefold())
    async def on_help_message(message: Message) -> None:
        await on_help(message)

    @router.callback_query(F.data == BUY_TICKET_CALLBACK)
    async def on_buy_callback(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await process_buy(
            callback.from_user,
            lambda text, **kwargs: send_to_callback_origin(callback, text, **kwargs),
            state,
        )

    @router.callback_query(F.data == CHECK_PAYMENT_CALLBACK)
    async def on_check_payment_callback(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await process_check_payment(
            callback.from_user,
            lambda text, **kwargs: send_to_callback_origin(callback, text, **kwargs),
            state,
        )

    @router.callback_query(F.data == MY_TICKETS_CALLBACK)
    async def on_my_tickets_callback(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await process_my_tickets(
            callback.from_user,
            lambda text, **kwargs: send_to_callback_origin(callback, text, **kwargs),
            state,
        )

    @router.callback_query(F.data == HELP_CALLBACK)
    async def on_help_callback(callback: CallbackQuery) -> None:
        await callback.answer()
        await send_to_callback_origin(
            callback,
            "Нужна помощь? Напишите администратору 🙌\n"
            "И да, мероприятие строго 18+ 🔞",
            reply_markup=main_actions_inline_keyboard(),
        )

    @router.callback_query(F.data == ADMIN_EXPORT_CALLBACK)
    async def on_admin_export_callback(callback: CallbackQuery) -> None:
        user_id = callback.from_user.id
        if not is_super_admin(user_id):
            await callback.answer("Недостаточно прав", show_alert=True)
            return
        await callback.answer("Формирую выгрузку…")
        purchases = await payment_service.list_purchases()
        if not purchases:
            await send_to_callback_origin(callback, "Пока нет покупок 📭")
            return

        csv_bytes = build_purchases_csv(purchases)
        filename = f"purchases_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        document = BufferedInputFile(csv_bytes, filename=filename)
        target_message = callback.message
        if target_message is not None:
            await target_message.answer_document(document=document, caption="Готово ✅")
            return
        await callback.bot.send_document(chat_id=user_id, document=document, caption="Готово ✅")

    @router.callback_query(F.data == ADMIN_CHECK_TICKET_CALLBACK)
    async def on_admin_check_ticket_callback(callback: CallbackQuery, state: FSMContext) -> None:
        user_id = callback.from_user.id
        if not can_check_tickets(user_id):
            await callback.answer("Недостаточно прав", show_alert=True)
            return
        await callback.answer()
        await state.set_state(AdminTicketCheckState.waiting_ticket_number)
        await send_to_callback_origin(callback, "Введите 3-значный номер билета для проверки 🎫")

    @router.callback_query(F.data == ADMIN_SET_EVENT_ADDRESS_CALLBACK)
    async def on_admin_set_event_address_callback(callback: CallbackQuery, state: FSMContext) -> None:
        user_id = callback.from_user.id
        if not is_super_admin(user_id):
            await callback.answer("Недостаточно прав", show_alert=True)
            return
        await callback.answer()
        await state.set_state(AdminEventAddressState.waiting_event_address)
        await send_to_callback_origin(callback, "Введите новый адрес мероприятия 📍")

    @router.message(AdminEventAddressState.waiting_event_address)
    async def on_admin_event_address(message: Message, state: FSMContext) -> None:
        user_id = message.from_user.id if message.from_user else 0
        if not is_super_admin(user_id):
            await state.clear()
            await message.answer("⛔ Недостаточно прав.")
            return

        value = (message.text or "").strip()
        if len(value) < 5:
            await message.answer("Слишком коротко. Введите нормальный адрес, пожалуйста 🙏")
            return
        await event_settings_service.set_event_address(value)
        await state.clear()
        await message.answer(f"✅ Адрес сохранен:\n📍 {value}")

    @router.message(AdminTicketCheckState.waiting_ticket_number)
    async def on_admin_ticket_number(message: Message, state: FSMContext) -> None:
        user_id = message.from_user.id if message.from_user else 0
        if not can_check_tickets(user_id):
            await state.clear()
            await message.answer("⛔ Недостаточно прав.")
            return

        ticket_number = (message.text or "").strip()
        try:
            result = await payment_service.check_and_consume_ticket(ticket_number)
        except PaymentValidationError:
            await message.answer("Нужны ровно 3 цифры. Пример: 123")
            return

        if result.status == "not_found":
            await state.clear()
            await message.answer("❌ Билет недействителен: номер не найден.")
            return

        if result.status == "not_paid":
            await state.clear()
            await message.answer("❌ Билет недействителен: оплата не подтверждена.")
            return

        if result.status == "already_used":
            await state.clear()
            await message.answer("❌ Билет уже использован.")
            return

        payment = result.payment
        await state.clear()
        await message.answer(
            "✅ Билет действителен и сейчас помечен как использованный.\n"
            f"Номер: {payment.ticket_number}\n"
            f"Покупатель: {payment.full_name}\n"
            f"Телефон: {payment.phone}",
        )

    @router.message(PurchaseFormState.waiting_full_name)
    async def on_waiting_full_name(message: Message, state: FSMContext) -> None:
        value = (message.text or "").strip()
        if len(value) < 5:
            await message.answer("ФИО слишком короткое. Введите полное ФИО 🙏")
            return
        await state.update_data(full_name=value)
        await state.set_state(PurchaseFormState.waiting_age)
        await message.answer("Шаг 2/3: введите возраст цифрами 🔞")

    @router.message(PurchaseFormState.waiting_age)
    async def on_waiting_age(message: Message, state: FSMContext) -> None:
        value = (message.text or "").strip()
        if not value.isdigit():
            await message.answer("Возраст нужен цифрами. Пример: 24")
            return
        age = int(value)
        if age < 18:
            await message.answer("🚫 На это мероприятие только 18+.\nК сожалению, продолжить оформление нельзя.")
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
        value = (message.text or "").strip()
        if not PHONE_PATTERN.match(value):
            await message.answer("Формат телефона не подошел. Пример: +79991234567")
            return
        normalized = re.sub(r"\s+", "", value)
        await state.update_data(phone=normalized)
        if message.from_user is None:
            await message.answer("Не удалось определить пользователя. Нажмите /start")
            return
        await attempt_payment_creation_with_profile(message.from_user, message.answer, state)

    @router.message(F.text & ~F.text.startswith("/"))
    async def on_fallback(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        await message.answer(
            "🤖 Давайте по кнопкам, так надежнее 😌",
            reply_markup=main_menu_keyboard(is_admin=can_check_tickets(user_id)),
        )

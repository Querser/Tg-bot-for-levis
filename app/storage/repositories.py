from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.domain import PaymentRecord, PaymentStatus
from app.storage.models import AppSetting, Payment


def _to_record(model: Payment) -> PaymentRecord:
    return PaymentRecord(
        local_id=model.id,
        telegram_user_id=model.telegram_user_id,
        full_name=model.full_name,
        address=model.address,
        age=model.age,
        phone=model.phone,
        amount_rub=Decimal(str(model.amount_rub)),
        currency=model.currency,
        description=model.description,
        status=PaymentStatus(model.status),
        idempotency_key=model.idempotency_key,
        yookassa_payment_id=model.yookassa_payment_id,
        confirmation_url=model.confirmation_url,
        ticket_number=model.ticket_number,
        ticket_valid=bool(model.ticket_valid),
        ticket_used_at=model.ticket_used_at,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class SqlAlchemyPaymentRepository:
    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._session_factory = session_factory

    async def create_payment_record(
        self,
        *,
        telegram_user_id: int,
        full_name: str | None,
        address: str | None,
        age: int | None,
        phone: str | None,
        amount_rub: Decimal,
        currency: str,
        description: str,
        status: PaymentStatus,
        idempotency_key: str,
        yookassa_payment_id: str | None,
        confirmation_url: str | None,
        raw_payload: str | None,
        last_error: str | None,
    ) -> PaymentRecord:
        async with self._session_factory() as session:
            payment = Payment(
                telegram_user_id=telegram_user_id,
                full_name=full_name,
                address=address,
                age=age,
                phone=phone,
                amount_rub=amount_rub,
                currency=currency,
                description=description,
                status=status.value,
                idempotency_key=idempotency_key,
                yookassa_payment_id=yookassa_payment_id,
                confirmation_url=confirmation_url,
                raw_payload=raw_payload,
                last_error=last_error,
            )
            session.add(payment)
            await session.commit()
            await session.refresh(payment)
            return _to_record(payment)

    async def get_payment_by_idempotency_key(self, idempotency_key: str) -> PaymentRecord | None:
        async with self._session_factory() as session:
            stmt: Select[tuple[Payment]] = (
                select(Payment).where(Payment.idempotency_key == idempotency_key).limit(1)
            )
            model = await session.scalar(stmt)
            return _to_record(model) if model else None

    async def get_payment_by_yookassa_payment_id(self, yookassa_payment_id: str) -> PaymentRecord | None:
        async with self._session_factory() as session:
            stmt: Select[tuple[Payment]] = (
                select(Payment).where(Payment.yookassa_payment_id == yookassa_payment_id).limit(1)
            )
            model = await session.scalar(stmt)
            return _to_record(model) if model else None

    async def get_latest_payment_for_user(self, telegram_user_id: int) -> PaymentRecord | None:
        async with self._session_factory() as session:
            stmt: Select[tuple[Payment]] = (
                select(Payment)
                .where(Payment.telegram_user_id == telegram_user_id)
                .order_by(Payment.id.desc())
                .limit(1)
            )
            model = await session.scalar(stmt)
            return _to_record(model) if model else None

    async def get_payment_by_ticket_number(self, ticket_number: str) -> PaymentRecord | None:
        async with self._session_factory() as session:
            stmt: Select[tuple[Payment]] = (
                select(Payment).where(Payment.ticket_number == ticket_number).limit(1)
            )
            model = await session.scalar(stmt)
            return _to_record(model) if model else None

    async def update_payment_record(
        self,
        local_id: int,
        *,
        status: PaymentStatus | None = None,
        yookassa_payment_id: str | None = None,
        confirmation_url: str | None = None,
        raw_payload: str | None = None,
        last_error: str | None = None,
    ) -> PaymentRecord:
        async with self._session_factory() as session:
            payment = await session.get(Payment, local_id)
            if payment is None:
                raise ValueError(f"Payment {local_id} not found")
            if status is not None:
                payment.status = status.value
            if yookassa_payment_id is not None:
                payment.yookassa_payment_id = yookassa_payment_id
            if confirmation_url is not None:
                payment.confirmation_url = confirmation_url
            if raw_payload is not None:
                payment.raw_payload = raw_payload
            if last_error is not None:
                payment.last_error = last_error
            await session.commit()
            await session.refresh(payment)
            return _to_record(payment)

    async def assign_ticket_number(self, local_id: int, ticket_number: str) -> PaymentRecord:
        async with self._session_factory() as session:
            payment = await session.get(Payment, local_id)
            if payment is None:
                raise ValueError(f"Payment {local_id} not found")
            payment.ticket_number = ticket_number
            payment.ticket_valid = True
            payment.ticket_used_at = None
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ValueError("ticket_number_not_unique") from exc
            await session.refresh(payment)
            return _to_record(payment)

    async def mark_ticket_as_used(self, local_id: int) -> PaymentRecord:
        async with self._session_factory() as session:
            payment = await session.get(Payment, local_id)
            if payment is None:
                raise ValueError(f"Payment {local_id} not found")
            payment.ticket_valid = False
            payment.ticket_used_at = datetime.now(timezone.utc)
            await session.commit()
            await session.refresh(payment)
            return _to_record(payment)

    async def list_all(self) -> list[PaymentRecord]:
        async with self._session_factory() as session:
            stmt: Select[tuple[Payment]] = select(Payment).order_by(Payment.id.desc())
            rows = await session.scalars(stmt)
            return [_to_record(item) for item in rows]

    async def list_recent(self, limit: int = 20) -> list[PaymentRecord]:
        async with self._session_factory() as session:
            stmt: Select[tuple[Payment]] = select(Payment).order_by(Payment.id.desc()).limit(limit)
            rows = await session.scalars(stmt)
            return [_to_record(item) for item in rows]

    async def list_payments_for_user(self, telegram_user_id: int) -> list[PaymentRecord]:
        async with self._session_factory() as session:
            stmt: Select[tuple[Payment]] = (
                select(Payment)
                .where(Payment.telegram_user_id == telegram_user_id)
                .order_by(Payment.id.desc())
            )
            rows = await session.scalars(stmt)
            return [_to_record(item) for item in rows]


class SqlAlchemyAppSettingsRepository:
    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._session_factory = session_factory

    async def get_value(self, key: str) -> str | None:
        async with self._session_factory() as session:
            model = await session.get(AppSetting, key)
            if model is None:
                return None
            return model.value

    async def set_value(self, key: str, value: str) -> None:
        async with self._session_factory() as session:
            model = await session.get(AppSetting, key)
            if model is None:
                model = AppSetting(key=key, value=value)
                session.add(model)
            else:
                model.value = value
            await session.commit()

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.domain import PaymentRecord, PaymentStatus
from app.integrations.yookassa_client import YooKassaAPIError, YooKassaPayment
from app.services.payment_service import PaymentService


class InMemoryPaymentRepository:
    def __init__(self) -> None:
        self._items: dict[int, PaymentRecord] = {}
        self._id_seq = 0

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
        self._id_seq += 1
        now = datetime.now(timezone.utc)
        item = PaymentRecord(
            local_id=self._id_seq,
            telegram_user_id=telegram_user_id,
            full_name=full_name,
            address=address,
            age=age,
            phone=phone,
            amount_rub=amount_rub,
            currency=currency,
            description=description,
            status=status,
            idempotency_key=idempotency_key,
            yookassa_payment_id=yookassa_payment_id,
            confirmation_url=confirmation_url,
            ticket_number=None,
            ticket_valid=False,
            ticket_used_at=None,
            created_at=now,
            updated_at=now,
        )
        self._items[item.local_id] = item
        return item

    async def get_payment_by_idempotency_key(self, idempotency_key: str) -> PaymentRecord | None:
        for item in self._items.values():
            if item.idempotency_key == idempotency_key:
                return item
        return None

    async def get_payment_by_yookassa_payment_id(self, yookassa_payment_id: str) -> PaymentRecord | None:
        for item in self._items.values():
            if item.yookassa_payment_id == yookassa_payment_id:
                return item
        return None

    async def get_latest_payment_for_user(self, telegram_user_id: int) -> PaymentRecord | None:
        candidates = [item for item in self._items.values() if item.telegram_user_id == telegram_user_id]
        if not candidates:
            return None
        return sorted(candidates, key=lambda item: item.local_id, reverse=True)[0]

    async def list_payments_for_user(self, telegram_user_id: int) -> list[PaymentRecord]:
        return list(
            sorted(
                [item for item in self._items.values() if item.telegram_user_id == telegram_user_id],
                key=lambda item: item.local_id,
                reverse=True,
            )
        )

    async def get_payment_by_ticket_number(self, ticket_number: str) -> PaymentRecord | None:
        for item in self._items.values():
            if item.ticket_number == ticket_number:
                return item
        return None

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
        item = self._items[local_id]
        updated = replace(
            item,
            status=status or item.status,
            yookassa_payment_id=yookassa_payment_id or item.yookassa_payment_id,
            confirmation_url=confirmation_url or item.confirmation_url,
            updated_at=datetime.now(timezone.utc),
        )
        self._items[local_id] = updated
        return updated

    async def assign_ticket_number(self, local_id: int, ticket_number: str) -> PaymentRecord:
        for item in self._items.values():
            if item.ticket_number == ticket_number:
                raise ValueError("ticket_number_not_unique")
        payment = self._items[local_id]
        updated = replace(
            payment,
            ticket_number=ticket_number,
            ticket_valid=True,
            ticket_used_at=None,
            updated_at=datetime.now(timezone.utc),
        )
        self._items[local_id] = updated
        return updated

    async def mark_ticket_as_used(self, local_id: int) -> PaymentRecord:
        payment = self._items[local_id]
        updated = replace(
            payment,
            ticket_valid=False,
            ticket_used_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        self._items[local_id] = updated
        return updated

    async def list_all(self) -> list[PaymentRecord]:
        return list(sorted(self._items.values(), key=lambda item: item.local_id, reverse=True))


class FakeYooKassaClient:
    def __init__(self) -> None:
        self.create_calls = 0
        self.get_error: Exception | None = None
        self._remote_payment = YooKassaPayment(
            payment_id="pay_001",
            status="pending",
            amount=Decimal("299.00"),
            currency="RUB",
            description="Ticket payment",
            confirmation_url="https://pay.example/1",
            metadata={},
            paid=False,
            refundable=False,
            test=True,
            raw_payload={"id": "pay_001", "status": "pending", "amount": {"value": "299.00", "currency": "RUB"}},
        )

    async def create_payment(self, **_: object) -> YooKassaPayment:
        self.create_calls += 1
        return self._remote_payment

    async def get_payment(self, _: str) -> YooKassaPayment:
        if self.get_error is not None:
            raise self.get_error
        return self._remote_payment

    def set_status(self, status: str) -> None:
        self._remote_payment = replace(self._remote_payment, status=status)


@pytest.mark.asyncio
async def test_create_payment_is_idempotent() -> None:
    repo = InMemoryPaymentRepository()
    client = FakeYooKassaClient()
    service = PaymentService(repository=repo, yookassa_client=client)

    first = await service.create_payment(
        telegram_user_id=123,
        full_name="Ivan Ivanov",
        age=30,
        phone="+79991234567",
        amount_rub=Decimal("299.00"),
        description="Ticket",
        metadata={"source": "test"},
        idempotency_key="key-1",
        return_url="https://t.me",
    )
    second = await service.create_payment(
        telegram_user_id=123,
        full_name="Ivan Ivanov",
        age=30,
        phone="+79991234567",
        amount_rub=Decimal("299.00"),
        description="Ticket",
        metadata={"source": "test"},
        idempotency_key="key-1",
        return_url="https://t.me",
    )

    assert first.local_id == second.local_id
    assert client.create_calls == 1
    assert first.status == PaymentStatus.PENDING
    assert first.ticket_number is None


@pytest.mark.asyncio
async def test_ticket_is_issued_only_after_success() -> None:
    repo = InMemoryPaymentRepository()
    client = FakeYooKassaClient()
    service = PaymentService(repository=repo, yookassa_client=client)

    created = await service.create_payment(
        telegram_user_id=777,
        full_name="Petr Petrov",
        age=27,
        phone="+79990000000",
        amount_rub="300.00",
        description="Ticket",
        metadata={},
        idempotency_key="key-2",
        return_url="https://t.me",
    )
    assert created.status == PaymentStatus.PENDING
    assert created.ticket_number is None

    unchanged = await service.ensure_ticket_for_payment(created)
    assert unchanged.ticket_number is None

    client.set_status("succeeded")
    refreshed = await service.refresh_latest_user_payment(777)
    assert refreshed is not None
    assert refreshed.status == PaymentStatus.SUCCEEDED
    assert refreshed.ticket_number is None

    with_ticket = await service.ensure_ticket_for_payment(refreshed)
    assert with_ticket.ticket_number is not None
    assert len(with_ticket.ticket_number) == 3
    assert with_ticket.ticket_number.isdigit()
    assert with_ticket.ticket_valid is True


@pytest.mark.asyncio
async def test_webhook_updates_payment_status_and_export_list() -> None:
    repo = InMemoryPaymentRepository()
    client = FakeYooKassaClient()
    service = PaymentService(repository=repo, yookassa_client=client)

    created = await service.create_payment(
        telegram_user_id=42,
        full_name="Sidor Sidorov",
        age=35,
        phone="+79991111111",
        amount_rub=Decimal("100.00"),
        description="Ticket",
        metadata={},
        idempotency_key="key-3",
        return_url="https://t.me",
    )
    assert created.yookassa_payment_id == "pay_001"

    result = await service.process_webhook_event(
        {
            "event": "payment.succeeded",
            "object": {
                "type": "payment",
                "id": "pay_001",
                "status": "succeeded",
                "metadata": {"idempotency_key": "key-3"},
            },
        }
    )
    assert result is not None
    assert result.status == PaymentStatus.SUCCEEDED

    purchases = await service.list_purchases()
    assert purchases
    assert purchases[0].full_name == "Sidor Sidorov"
    assert purchases[0].age == 35


@pytest.mark.asyncio
async def test_check_and_consume_ticket_lifecycle() -> None:
    repo = InMemoryPaymentRepository()
    client = FakeYooKassaClient()
    service = PaymentService(repository=repo, yookassa_client=client)

    created = await service.create_payment(
        telegram_user_id=77,
        full_name="Maria Petrova",
        age=24,
        phone="+79992223344",
        amount_rub=Decimal("450.00"),
        description="Ticket",
        metadata={},
        idempotency_key="key-4",
        return_url="https://t.me",
    )

    client.set_status("succeeded")
    refreshed = await service.refresh_latest_user_payment(77)
    assert refreshed is not None
    paid = await service.ensure_ticket_for_payment(refreshed)
    assert paid.ticket_number is not None
    ticket = paid.ticket_number

    first_check = await service.check_and_consume_ticket(ticket)
    assert first_check.status == "valid_consumed"
    assert first_check.payment is not None
    assert first_check.payment.ticket_valid is False

    second_check = await service.check_and_consume_ticket(ticket)
    assert second_check.status == "already_used"

    missing = await service.check_and_consume_ticket("999")
    assert missing.status == "not_found"

    client.set_status("pending")
    pending_created = await service.create_payment(
        telegram_user_id=78,
        full_name="Alex Smirnov",
        age=31,
        phone="+79995556677",
        amount_rub=Decimal("450.00"),
        description="Ticket",
        metadata={},
        idempotency_key="key-5",
        return_url="https://t.me",
    )
    pending_ticket = await repo.assign_ticket_number(pending_created.local_id, "998")
    assert pending_ticket.status == PaymentStatus.PENDING
    pending_check = await service.check_and_consume_ticket("998")
    assert pending_check.status == "not_paid"


@pytest.mark.asyncio
async def test_refresh_latest_user_payment_falls_back_when_remote_payment_inaccessible() -> None:
    repo = InMemoryPaymentRepository()
    client = FakeYooKassaClient()
    service = PaymentService(repository=repo, yookassa_client=client)

    created = await service.create_payment(
        telegram_user_id=321,
        full_name="Legacy User",
        age=28,
        phone="+79990001122",
        amount_rub=Decimal("299.00"),
        description="Ticket",
        metadata={},
        idempotency_key="key-legacy-1",
        return_url="https://t.me",
    )
    assert created.status == PaymentStatus.PENDING

    client.get_error = YooKassaAPIError(
        message="Incorrect payment_id. Payment doesn't exist or access denied.",
        status_code=400,
        error_code="invalid_request",
        retryable=False,
    )

    refreshed = await service.refresh_latest_user_payment(321)
    assert refreshed is not None
    assert refreshed.local_id == created.local_id
    assert refreshed.status == PaymentStatus.CANCELED

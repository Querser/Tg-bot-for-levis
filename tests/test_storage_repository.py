from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain import PaymentStatus
from app.storage.db import build_engine, build_session_factory, init_database
from app.storage.repositories import SqlAlchemyPaymentRepository


@pytest.mark.asyncio
async def test_sqlalchemy_repository_crud() -> None:
    engine = build_engine("sqlite+aiosqlite:///:memory:")
    await init_database(engine)
    session_factory = build_session_factory(engine)
    repository = SqlAlchemyPaymentRepository(session_factory)

    created = await repository.create_payment_record(
        telegram_user_id=555,
        full_name="Ivan Ivanov",
        address="Moscow, Testovaya 1",
        age=29,
        phone="+79991234567",
        amount_rub=Decimal("199.00"),
        currency="RUB",
        description="Test payment",
        status=PaymentStatus.PENDING,
        idempotency_key="repo-key-1",
        yookassa_payment_id=None,
        confirmation_url=None,
        raw_payload=None,
        last_error=None,
    )
    assert created.local_id > 0
    assert created.status == PaymentStatus.PENDING
    assert created.full_name == "Ivan Ivanov"
    assert created.age == 29

    updated = await repository.update_payment_record(
        created.local_id,
        status=PaymentStatus.SUCCEEDED,
        yookassa_payment_id="pay-test-001",
        confirmation_url="https://pay.example/success",
    )
    assert updated.status == PaymentStatus.SUCCEEDED
    assert updated.yookassa_payment_id == "pay-test-001"

    by_key = await repository.get_payment_by_idempotency_key("repo-key-1")
    assert by_key is not None
    assert by_key.local_id == created.local_id

    by_remote = await repository.get_payment_by_yookassa_payment_id("pay-test-001")
    assert by_remote is not None
    assert by_remote.local_id == created.local_id

    latest = await repository.get_latest_payment_for_user(555)
    assert latest is not None
    assert latest.local_id == created.local_id

    all_items = await repository.list_all()
    assert len(all_items) == 1
    assert all_items[0].phone == "+79991234567"

    assigned = await repository.assign_ticket_number(created.local_id, "123")
    assert assigned.ticket_number == "123"
    assert assigned.ticket_valid is True

    fetched_by_ticket = await repository.get_payment_by_ticket_number("123")
    assert fetched_by_ticket is not None
    assert fetched_by_ticket.local_id == created.local_id

    consumed = await repository.mark_ticket_as_used(created.local_id)
    assert consumed.ticket_valid is False
    assert consumed.ticket_used_at is not None

    await engine.dispose()

from __future__ import annotations

from decimal import Decimal
from datetime import datetime, timezone

from app.bot.router import create_bot_router
from app.domain import PaymentRecord, PaymentStatus
from app.services.payment_service import TicketCheckResult


class DummyPaymentService:
    async def refresh_latest_user_payment(self, telegram_user_id: int) -> PaymentRecord | None:
        return None

    async def refresh_payment_status(self, yookassa_payment_id: str) -> PaymentRecord | None:
        return None

    async def create_payment(
        self,
        *,
        telegram_user_id: int,
        full_name: str,
        age: int,
        phone: str,
        amount_rub: Decimal | str | float | int,
        description: str,
        metadata,
        idempotency_key: str,
        return_url: str,
    ) -> PaymentRecord:
        raise RuntimeError("Not expected in router creation test")

    async def list_purchases(self) -> list[PaymentRecord]:
        return []

    async def ensure_ticket_for_payment(self, payment: PaymentRecord) -> PaymentRecord:
        return payment

    async def check_and_consume_ticket(self, ticket_number: str) -> TicketCheckResult:
        now = datetime.now(timezone.utc)
        payment = PaymentRecord(
            local_id=1,
            telegram_user_id=1,
            full_name="Test User",
            address="Test Address",
            age=30,
            phone="+79990000000",
            amount_rub=Decimal("100.00"),
            currency="RUB",
            description="Test",
            status=PaymentStatus.SUCCEEDED,
            idempotency_key="idem",
            yookassa_payment_id="pay_1",
            confirmation_url="https://pay.example",
            ticket_number=ticket_number,
            ticket_valid=False,
            ticket_used_at=now,
            created_at=now,
            updated_at=now,
        )
        return TicketCheckResult(status="already_used", payment=payment)


class DummyEventSettingsService:
    async def get_event_address(self) -> str:
        return "Test address"

    async def set_event_address(self, value: str) -> str:
        return value


def test_create_bot_router_smoke() -> None:
    router = create_bot_router(
        payment_service=DummyPaymentService(),
        event_settings_service=DummyEventSettingsService(),
        super_admin_ids=[1, 2, 3],
        ticket_admin_ids=[4, 5],
    )
    assert router.name == "main_bot_router"
    assert router.message.handlers
    assert router.callback_query.handlers

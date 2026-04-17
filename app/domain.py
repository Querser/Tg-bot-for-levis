from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum


class PaymentStatus(str, Enum):
    PENDING = "pending"
    WAITING_FOR_CAPTURE = "waiting_for_capture"
    SUCCEEDED = "succeeded"
    CANCELED = "canceled"


@dataclass(slots=True)
class PaymentRecord:
    local_id: int
    telegram_user_id: int
    full_name: str | None
    address: str | None
    age: int | None
    phone: str | None
    amount_rub: Decimal
    currency: str
    description: str
    status: PaymentStatus
    idempotency_key: str
    yookassa_payment_id: str | None
    confirmation_url: str | None
    ticket_number: str | None
    ticket_valid: bool
    ticket_used_at: datetime | None
    created_at: datetime
    updated_at: datetime


from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping, Protocol, runtime_checkable

from app.domain import PaymentRecord, PaymentStatus
from app.integrations.yookassa_client import (
    YooKassaAPIError,
    YooKassaClient,
    YooKassaError,
    YooKassaPayment,
)

LOGGER = logging.getLogger(__name__)


class PaymentServiceError(RuntimeError):
    """Base service error for payment operations."""


class PaymentValidationError(PaymentServiceError, ValueError):
    """Raised when user or integration payload is invalid."""


class WebhookValidationError(PaymentValidationError):
    """Raised when incoming webhook event has invalid schema."""


@dataclass(slots=True, frozen=True)
class ParsedWebhookEvent:
    event_type: str
    payment_id: str
    status: PaymentStatus
    confirmation_url: str | None
    metadata: dict[str, str]
    raw_payload: dict[str, Any]


@dataclass(slots=True, frozen=True)
class TicketCheckResult:
    status: str
    payment: PaymentRecord | None


@runtime_checkable
class PaymentRepositoryProtocol(Protocol):
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
    ) -> PaymentRecord: ...

    async def get_payment_by_idempotency_key(self, idempotency_key: str) -> PaymentRecord | None: ...

    async def get_payment_by_yookassa_payment_id(
        self, yookassa_payment_id: str
    ) -> PaymentRecord | None: ...

    async def get_latest_payment_for_user(self, telegram_user_id: int) -> PaymentRecord | None: ...

    async def list_payments_for_user(self, telegram_user_id: int) -> list[PaymentRecord]: ...

    async def get_payment_by_ticket_number(self, ticket_number: str) -> PaymentRecord | None: ...

    async def update_payment_record(
        self,
        local_id: int,
        *,
        status: PaymentStatus | None = None,
        yookassa_payment_id: str | None = None,
        confirmation_url: str | None = None,
        raw_payload: str | None = None,
        last_error: str | None = None,
    ) -> PaymentRecord: ...

    async def assign_ticket_number(self, local_id: int, ticket_number: str) -> PaymentRecord: ...

    async def mark_ticket_as_used(self, local_id: int) -> PaymentRecord: ...

    async def list_all(self) -> list[PaymentRecord]: ...


class PaymentService:
    _SUPPORTED_WEBHOOK_EVENTS = frozenset(
        {
            "payment.pending",
            "payment.waiting_for_capture",
            "payment.succeeded",
            "payment.canceled",
        }
    )

    def __init__(
        self,
        *,
        repository: PaymentRepositoryProtocol,
        yookassa_client: YooKassaClient,
    ) -> None:
        self._repository = repository
        self._yookassa_client = yookassa_client

    async def create_payment(
        self,
        *,
        telegram_user_id: int,
        full_name: str,
        age: int,
        phone: str,
        amount_rub: Decimal | str | float | int,
        description: str,
        metadata: Mapping[str, Any] | None,
        idempotency_key: str,
        return_url: str,
    ) -> PaymentRecord:
        clean_telegram_user_id = self._normalize_telegram_user_id(telegram_user_id)
        clean_full_name = self._require_non_empty(full_name, "full_name")
        clean_age = self._normalize_age(age)
        clean_phone = self._require_non_empty(phone, "phone")
        clean_amount = self._normalize_amount(amount_rub)
        clean_description = self._require_non_empty(description, "description")
        clean_idempotency_key = self._validate_idempotency_key(idempotency_key)
        clean_return_url = self._require_non_empty(return_url, "return_url")

        existing = await self._repository.get_payment_by_idempotency_key(clean_idempotency_key)
        if existing is not None:
            return existing

        local_record = await self._repository.create_payment_record(
            telegram_user_id=clean_telegram_user_id,
            full_name=clean_full_name,
            address=None,
            age=clean_age,
            phone=clean_phone,
            amount_rub=clean_amount,
            currency="RUB",
            description=clean_description,
            status=PaymentStatus.PENDING,
            idempotency_key=clean_idempotency_key,
            yookassa_payment_id=None,
            confirmation_url=None,
            raw_payload=None,
            last_error=None,
        )

        payment_metadata = self._build_metadata(
            metadata=metadata,
            telegram_user_id=clean_telegram_user_id,
            local_payment_id=local_record.local_id,
            idempotency_key=clean_idempotency_key,
            full_name=clean_full_name,
            age=clean_age,
            phone=clean_phone,
        )

        try:
            remote_payment = await self._yookassa_client.create_payment(
                amount_rub=clean_amount,
                description=clean_description,
                metadata=payment_metadata,
                idempotency_key=clean_idempotency_key,
                return_url=clean_return_url,
            )
        except Exception as exc:
            await self._mark_payment_error(local_record.local_id, exc)
            raise

        return await self._apply_remote_payment(
            local_record=local_record,
            remote_payment=remote_payment,
            clear_error=True,
        )

    async def refresh_payment_status(self, yookassa_payment_id: str) -> PaymentRecord | None:
        clean_payment_id = self._require_non_empty(yookassa_payment_id, "yookassa_payment_id")
        local_record = await self._repository.get_payment_by_yookassa_payment_id(clean_payment_id)

        try:
            remote_payment = await self._yookassa_client.get_payment(clean_payment_id)
        except YooKassaAPIError as exc:
            if local_record is None:
                raise
            await self._mark_payment_error(local_record.local_id, exc)
            if exc.status_code in {400, 404}:
                LOGGER.warning(
                    "Marking payment local_id=%s as canceled: YooKassa no longer allows access to payment_id=%s",
                    local_record.local_id,
                    clean_payment_id,
                )
                return await self._repository.update_payment_record(
                    local_id=local_record.local_id,
                    status=PaymentStatus.CANCELED,
                )
            return local_record
        except YooKassaError as exc:
            if local_record is None:
                raise
            await self._mark_payment_error(local_record.local_id, exc)
            return local_record

        if local_record is None:
            idempotency_key = remote_payment.metadata.get("idempotency_key")
            if idempotency_key:
                local_record = await self._repository.get_payment_by_idempotency_key(idempotency_key)
        if local_record is None:
            return None

        return await self._apply_remote_payment(
            local_record=local_record,
            remote_payment=remote_payment,
            clear_error=True,
        )

    async def refresh_latest_user_payment(self, telegram_user_id: int) -> PaymentRecord | None:
        clean_telegram_user_id = self._normalize_telegram_user_id(telegram_user_id)
        local_record = await self._repository.get_latest_payment_for_user(clean_telegram_user_id)
        if local_record is None:
            return None
        if local_record.yookassa_payment_id is None:
            return local_record
        refreshed = await self.refresh_payment_status(local_record.yookassa_payment_id)
        return refreshed or local_record

    async def ensure_ticket_for_payment(self, payment: PaymentRecord) -> PaymentRecord:
        if payment.status != PaymentStatus.SUCCEEDED:
            return payment
        if payment.ticket_number and payment.ticket_valid:
            return payment

        for ticket_number in self._ticket_candidates():
            try:
                return await self._repository.assign_ticket_number(payment.local_id, ticket_number)
            except ValueError as exc:
                if str(exc) != "ticket_number_not_unique":
                    raise
        raise PaymentServiceError("Не удалось сгенерировать уникальный 3-значный билет.")

    async def check_and_consume_ticket(self, ticket_number: str) -> TicketCheckResult:
        checked = await self.check_ticket(ticket_number)
        if checked.status != "valid":
            return checked

        if checked.payment is None or checked.payment.ticket_number is None:
            return TicketCheckResult(status="not_found", payment=None)

        updated = await self._repository.mark_ticket_as_used(checked.payment.local_id)
        return TicketCheckResult(status="valid_consumed", payment=updated)

    async def check_ticket(self, ticket_number: str) -> TicketCheckResult:
        clean_ticket = self._normalize_ticket_number(ticket_number)
        payment = await self._repository.get_payment_by_ticket_number(clean_ticket)
        if payment is None:
            return TicketCheckResult(status="not_found", payment=None)

        if payment.status != PaymentStatus.SUCCEEDED:
            return TicketCheckResult(status="not_paid", payment=payment)

        if not payment.ticket_valid:
            return TicketCheckResult(status="already_used", payment=payment)

        return TicketCheckResult(status="valid", payment=payment)

    async def list_purchases(self) -> list[PaymentRecord]:
        return await self._repository.list_all()

    async def list_user_tickets(self, telegram_user_id: int) -> list[PaymentRecord]:
        clean_telegram_user_id = self._normalize_telegram_user_id(telegram_user_id)
        payments = await self._repository.list_payments_for_user(clean_telegram_user_id)

        result: list[PaymentRecord] = []
        for payment in payments:
            current = payment
            if current.status == PaymentStatus.SUCCEEDED and not current.ticket_number:
                current = await self.ensure_ticket_for_payment(current)
            if current.ticket_number:
                result.append(current)
        return result

    async def process_webhook_event(self, payload: Mapping[str, Any]) -> PaymentRecord | None:
        parsed_event = self.parse_webhook_event(payload)

        local_record = await self._repository.get_payment_by_yookassa_payment_id(parsed_event.payment_id)
        if local_record is None:
            idempotency_key = parsed_event.metadata.get("idempotency_key")
            if idempotency_key:
                local_record = await self._repository.get_payment_by_idempotency_key(idempotency_key)
        if local_record is None:
            return None

        return await self._repository.update_payment_record(
            local_id=local_record.local_id,
            status=parsed_event.status,
            yookassa_payment_id=parsed_event.payment_id,
            confirmation_url=parsed_event.confirmation_url,
            raw_payload=self._serialize_payload(parsed_event.raw_payload),
            last_error=None,
        )

    def parse_webhook_event(self, payload: Mapping[str, Any]) -> ParsedWebhookEvent:
        if not isinstance(payload, Mapping):
            raise WebhookValidationError("Webhook payload must be a JSON object.")

        event_type_raw = payload.get("event")
        event_type = event_type_raw.strip() if isinstance(event_type_raw, str) else ""
        if not event_type:
            raise WebhookValidationError("Webhook payload does not contain event type.")
        if event_type not in self._SUPPORTED_WEBHOOK_EVENTS:
            raise WebhookValidationError(f"Unsupported webhook event: {event_type}.")

        object_payload = payload.get("object")
        if not isinstance(object_payload, Mapping):
            raise WebhookValidationError("Webhook payload does not contain object payload.")

        object_type_raw = object_payload.get("type")
        object_type = object_type_raw.strip() if isinstance(object_type_raw, str) else ""
        if object_type != "payment":
            raise WebhookValidationError("Webhook object type must be 'payment'.")

        payment_id_raw = object_payload.get("id")
        payment_id = payment_id_raw.strip() if isinstance(payment_id_raw, str) else ""
        if not payment_id:
            raise WebhookValidationError("Webhook object does not contain payment id.")

        status_raw = object_payload.get("status")
        status = self._parse_status(status_raw)

        confirmation_url = self._extract_confirmation_url(object_payload)
        metadata = self._stringify_metadata(object_payload.get("metadata"))

        return ParsedWebhookEvent(
            event_type=event_type,
            payment_id=payment_id,
            status=status,
            confirmation_url=confirmation_url,
            metadata=metadata,
            raw_payload=dict(payload),
        )

    async def _apply_remote_payment(
        self,
        *,
        local_record: PaymentRecord,
        remote_payment: YooKassaPayment,
        clear_error: bool,
    ) -> PaymentRecord:
        update_kwargs: dict[str, Any] = {
            "status": self._parse_status(remote_payment.status),
            "yookassa_payment_id": remote_payment.payment_id,
            "confirmation_url": remote_payment.confirmation_url,
            "raw_payload": self._serialize_payload(remote_payment.raw_payload),
        }
        if clear_error:
            update_kwargs["last_error"] = None

        return await self._repository.update_payment_record(local_record.local_id, **update_kwargs)

    async def _mark_payment_error(self, local_id: int, exc: Exception) -> None:
        error_text = str(exc).strip() or exc.__class__.__name__
        safe_error = error_text[:1000]
        try:
            await self._repository.update_payment_record(local_id, last_error=safe_error)
        except Exception:
            return

    @staticmethod
    def _build_metadata(
        *,
        metadata: Mapping[str, Any] | None,
        telegram_user_id: int,
        local_payment_id: int,
        idempotency_key: str,
        full_name: str,
        age: int,
        phone: str,
    ) -> dict[str, str]:
        merged = PaymentService._stringify_metadata(metadata)
        merged.setdefault("telegram_user_id", str(telegram_user_id))
        merged.setdefault("local_payment_id", str(local_payment_id))
        merged.setdefault("idempotency_key", idempotency_key)
        merged.setdefault("full_name", full_name)
        merged.setdefault("age", str(age))
        merged.setdefault("phone", phone)
        return merged

    @staticmethod
    def _extract_confirmation_url(payload: Mapping[str, Any]) -> str | None:
        confirmation_payload = payload.get("confirmation")
        if not isinstance(confirmation_payload, Mapping):
            return None

        url_raw = confirmation_payload.get("confirmation_url")
        if not isinstance(url_raw, str):
            return None
        url = url_raw.strip()
        return url or None

    @staticmethod
    def _normalize_amount(amount_rub: Decimal | str | float | int) -> Decimal:
        try:
            amount = Decimal(str(amount_rub))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise PaymentValidationError("amount_rub must be a valid number.") from exc
        normalized = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if normalized <= Decimal("0.00"):
            raise PaymentValidationError("amount_rub must be greater than zero.")
        return normalized

    @staticmethod
    def _normalize_telegram_user_id(telegram_user_id: int) -> int:
        try:
            normalized = int(telegram_user_id)
        except (TypeError, ValueError) as exc:
            raise PaymentValidationError("telegram_user_id must be an integer.") from exc
        if normalized <= 0:
            raise PaymentValidationError("telegram_user_id must be greater than zero.")
        return normalized

    @staticmethod
    def _normalize_age(age: int | str) -> int:
        try:
            normalized = int(age)
        except (TypeError, ValueError) as exc:
            raise PaymentValidationError("age must be an integer.") from exc
        if normalized < 1 or normalized > 120:
            raise PaymentValidationError("age must be between 1 and 120.")
        return normalized

    @staticmethod
    def _validate_idempotency_key(idempotency_key: str) -> str:
        clean_key = idempotency_key.strip()
        if not clean_key:
            raise PaymentValidationError("idempotency_key must not be empty.")
        if len(clean_key) > 64:
            raise PaymentValidationError("idempotency_key length must not exceed 64 chars.")
        return clean_key

    @staticmethod
    def _require_non_empty(value: str, field_name: str) -> str:
        if not isinstance(value, str):
            raise PaymentValidationError(f"{field_name} must be a string.")
        clean_value = value.strip()
        if not clean_value:
            raise PaymentValidationError(f"{field_name} must not be empty.")
        return clean_value

    @staticmethod
    def _parse_status(status_raw: Any) -> PaymentStatus:
        if not isinstance(status_raw, str):
            raise PaymentValidationError("Payment status must be a string.")
        status_value = status_raw.strip().lower()
        mapping = {
            "pending": PaymentStatus.PENDING,
            "waiting_for_capture": PaymentStatus.WAITING_FOR_CAPTURE,
            "succeeded": PaymentStatus.SUCCEEDED,
            "canceled": PaymentStatus.CANCELED,
        }
        try:
            return mapping[status_value]
        except KeyError as exc:
            raise PaymentValidationError(f"Unsupported payment status: {status_value}.") from exc

    @staticmethod
    def _serialize_payload(payload: Mapping[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _stringify_metadata(metadata: Any) -> dict[str, str]:
        if not isinstance(metadata, Mapping):
            return {}

        clean_metadata: dict[str, str] = {}
        for key, value in metadata.items():
            clean_key = str(key)
            if isinstance(value, str):
                clean_metadata[clean_key] = value
            elif value is None:
                clean_metadata[clean_key] = ""
            else:
                clean_metadata[clean_key] = str(value)
        return clean_metadata

    @staticmethod
    def _normalize_ticket_number(value: str) -> str:
        if not isinstance(value, str):
            raise PaymentValidationError("ticket_number must be a string.")
        clean = value.strip()
        if len(clean) != 3 or not clean.isdigit():
            raise PaymentValidationError("ticket_number must be exactly 3 digits.")
        return clean

    @staticmethod
    def _ticket_candidates() -> list[str]:
        values = [f"{value:03d}" for value in range(100, 1000)]
        random.SystemRandom().shuffle(values)
        return values


__all__ = [
    "ParsedWebhookEvent",
    "PaymentRepositoryProtocol",
    "PaymentService",
    "PaymentServiceError",
    "PaymentValidationError",
    "TicketCheckResult",
    "WebhookValidationError",
]

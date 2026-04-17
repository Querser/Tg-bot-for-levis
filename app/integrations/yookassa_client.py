from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping
from urllib.parse import quote

import aiohttp

DEFAULT_YOOKASSA_BASE_URL = "https://api.yookassa.ru/v3"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 10.0
DEFAULT_CURRENCY = "RUB"


class YooKassaError(RuntimeError):
    """Base YooKassa integration error."""


class YooKassaNetworkError(YooKassaError):
    """Raised when HTTP request cannot reach YooKassa."""


class YooKassaResponseError(YooKassaError):
    """Raised when YooKassa returns invalid or incomplete response payload."""


class YooKassaAPIError(YooKassaError):
    """Raised when YooKassa returns non-2xx response."""

    def __init__(
        self,
        *,
        message: str,
        status_code: int,
        error_code: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.retryable = retryable


@dataclass(slots=True, frozen=True)
class YooKassaPayment:
    payment_id: str
    status: str
    amount: Decimal
    currency: str
    description: str | None
    confirmation_url: str | None
    metadata: dict[str, str]
    paid: bool | None
    refundable: bool | None
    test: bool | None
    raw_payload: dict[str, Any]


def normalize_amount_rub(amount_rub: Decimal | str | float | int) -> Decimal:
    try:
        amount = Decimal(str(amount_rub))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Amount must be a valid number.") from exc

    normalized = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if normalized <= Decimal("0.00"):
        raise ValueError("Amount must be greater than zero.")
    return normalized


class YooKassaClient:
    def __init__(
        self,
        *,
        shop_id: str,
        secret_key: str,
        base_url: str = DEFAULT_YOOKASSA_BASE_URL,
        timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        trust_env: bool = True,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        if not shop_id or not shop_id.strip():
            raise ValueError("shop_id must not be empty.")
        if not secret_key or not secret_key.strip():
            raise ValueError("secret_key must not be empty.")
        if not base_url or not base_url.strip():
            raise ValueError("base_url must not be empty.")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero.")

        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._provided_session = session is not None
        self._session = session
        self._trust_env = trust_env

        auth_raw = f"{shop_id}:{secret_key}".encode("utf-8")
        auth_token = base64.b64encode(auth_raw).decode("ascii")
        self._default_headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Basic {auth_token}",
        }

    async def __aenter__(self) -> "YooKassaClient":
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._session is None:
            return
        if self._provided_session:
            return
        if self._session.closed:
            return
        await self._session.close()

    async def create_payment(
        self,
        amount_rub: Decimal | str | float | int,
        description: str,
        metadata: Mapping[str, Any] | None,
        idempotency_key: str,
        return_url: str,
    ) -> YooKassaPayment:
        amount = normalize_amount_rub(amount_rub)
        clean_description = description.strip()
        clean_return_url = return_url.strip()
        clean_idempotency_key = idempotency_key.strip()

        if not clean_description:
            raise ValueError("description must not be empty.")
        if not clean_return_url:
            raise ValueError("return_url must not be empty.")
        if not clean_idempotency_key:
            raise ValueError("idempotency_key must not be empty.")

        payload: dict[str, Any] = {
            "amount": {
                "value": format(amount, "f"),
                "currency": DEFAULT_CURRENCY,
            },
            "capture": True,
            "confirmation": {
                "type": "redirect",
                "return_url": clean_return_url,
            },
            "description": clean_description,
        }

        metadata_payload = self._stringify_metadata(metadata)
        if metadata_payload:
            payload["metadata"] = metadata_payload

        response_payload = await self._request_json(
            method="POST",
            path="/payments",
            json_payload=payload,
            headers={"Idempotence-Key": clean_idempotency_key},
        )
        return self._parse_payment(response_payload)

    async def get_payment(self, payment_id: str) -> YooKassaPayment:
        clean_payment_id = payment_id.strip()
        if not clean_payment_id:
            raise ValueError("payment_id must not be empty.")

        response_payload = await self._request_json(
            method="GET",
            path=f"/payments/{quote(clean_payment_id, safe='')}",
        )
        return self._parse_payment(response_payload)

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is not None:
            if self._session.closed:
                raise YooKassaNetworkError("Configured aiohttp session is already closed.")
            return self._session

        self._session = aiohttp.ClientSession(
            timeout=self._timeout,
            headers=self._default_headers,
            trust_env=self._trust_env,
        )
        return self._session

    async def _request_json(
        self,
        *,
        method: str,
        path: str,
        json_payload: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        session = await self._ensure_session()
        response_status: int | None = None
        response_text = ""
        merged_headers = dict(self._default_headers)
        merged_headers.update(dict(headers or {}))

        try:
            async with session.request(
                method=method,
                url=f"{self._base_url}{path}",
                json=json_payload,
                headers=merged_headers,
            ) as response:
                response_status = response.status
                response_text = await response.text()
        except asyncio.TimeoutError as exc:
            raise YooKassaNetworkError("Request to YooKassa timed out.") from exc
        except aiohttp.ClientError as exc:
            raise YooKassaNetworkError("Request to YooKassa failed.") from exc

        if response_status is None:
            raise YooKassaNetworkError("Failed to receive HTTP status from YooKassa.")

        if response_text:
            try:
                parsed_payload = json.loads(response_text)
            except json.JSONDecodeError as exc:
                raise YooKassaResponseError("YooKassa returned a non-JSON response.") from exc
        else:
            parsed_payload = {}

        if not isinstance(parsed_payload, Mapping):
            raise YooKassaResponseError("YooKassa response JSON must be an object.")

        parsed_dict = dict(parsed_payload)
        if response_status >= 400:
            raise self._build_api_error(status_code=response_status, payload=parsed_dict)
        return parsed_dict

    @staticmethod
    def _build_api_error(*, status_code: int, payload: Mapping[str, Any]) -> YooKassaAPIError:
        error_code = payload.get("code")
        description = payload.get("description")

        message = f"YooKassa API returned HTTP {status_code}."
        if isinstance(description, str) and description.strip():
            message = description.strip()

        retryable = status_code in {408, 429} or status_code >= 500
        return YooKassaAPIError(
            message=message,
            status_code=status_code,
            error_code=str(error_code) if error_code is not None else None,
            retryable=retryable,
        )

    @staticmethod
    def _parse_payment(payload: Mapping[str, Any]) -> YooKassaPayment:
        payment_id_raw = payload.get("id")
        status_raw = payload.get("status")
        amount_payload = payload.get("amount")

        if not isinstance(payment_id_raw, str) or not payment_id_raw.strip():
            raise YooKassaResponseError("YooKassa response does not contain valid payment id.")
        if not isinstance(status_raw, str) or not status_raw.strip():
            raise YooKassaResponseError("YooKassa response does not contain valid payment status.")
        if not isinstance(amount_payload, Mapping):
            raise YooKassaResponseError("YooKassa response does not contain amount object.")

        amount_value_raw = amount_payload.get("value")
        currency_raw = amount_payload.get("currency")

        if amount_value_raw is None:
            raise YooKassaResponseError("YooKassa response does not contain amount value.")
        if not isinstance(currency_raw, str) or not currency_raw.strip():
            raise YooKassaResponseError("YooKassa response does not contain currency.")

        try:
            amount = normalize_amount_rub(str(amount_value_raw))
        except ValueError as exc:
            raise YooKassaResponseError("YooKassa response contains invalid amount value.") from exc

        confirmation_url: str | None = None
        confirmation_payload = payload.get("confirmation")
        if isinstance(confirmation_payload, Mapping):
            url_raw = confirmation_payload.get("confirmation_url")
            if isinstance(url_raw, str) and url_raw.strip():
                confirmation_url = url_raw.strip()

        metadata = YooKassaClient._stringify_metadata(payload.get("metadata"))

        description_raw = payload.get("description")
        description = description_raw.strip() if isinstance(description_raw, str) else None

        paid_raw = payload.get("paid")
        refundable_raw = payload.get("refundable")
        test_raw = payload.get("test")

        return YooKassaPayment(
            payment_id=payment_id_raw.strip(),
            status=status_raw.strip(),
            amount=amount,
            currency=currency_raw.strip(),
            description=description,
            confirmation_url=confirmation_url,
            metadata=metadata,
            paid=paid_raw if isinstance(paid_raw, bool) else None,
            refundable=refundable_raw if isinstance(refundable_raw, bool) else None,
            test=test_raw if isinstance(test_raw, bool) else None,
            raw_payload=dict(payload),
        )

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


__all__ = [
    "DEFAULT_CURRENCY",
    "DEFAULT_REQUEST_TIMEOUT_SECONDS",
    "DEFAULT_YOOKASSA_BASE_URL",
    "YooKassaAPIError",
    "YooKassaClient",
    "YooKassaError",
    "YooKassaNetworkError",
    "YooKassaPayment",
    "YooKassaResponseError",
    "normalize_amount_rub",
]

from __future__ import annotations

import json
from typing import Protocol, runtime_checkable


class EventSettingsValidationError(ValueError):
    """Raised when event settings payload is invalid."""


@runtime_checkable
class AppSettingsRepositoryProtocol(Protocol):
    async def get_value(self, key: str) -> str | None: ...

    async def set_value(self, key: str, value: str) -> None: ...


class EventSettingsService:
    EVENT_ADDRESS_KEY = "event_address"
    KNOWN_USER_IDS_KEY = "known_user_ids"

    def __init__(
        self,
        *,
        repository: AppSettingsRepositoryProtocol,
        default_event_address: str,
    ) -> None:
        self._repository = repository
        self._default_event_address = self._require_non_empty(default_event_address, "default_event_address")

    async def get_event_address(self) -> str:
        value = await self._repository.get_value(self.EVENT_ADDRESS_KEY)
        if value is None or not value.strip():
            return self._default_event_address
        return value.strip()

    async def set_event_address(self, value: str) -> str:
        clean = self._require_non_empty(value, "event_address")
        if len(clean) > 500:
            raise EventSettingsValidationError("event_address is too long (max 500 chars).")
        await self._repository.set_value(self.EVENT_ADDRESS_KEY, clean)
        return clean

    async def register_known_user(self, telegram_user_id: int) -> None:
        user_id = self._normalize_user_id(telegram_user_id)
        existing = await self.list_known_user_ids()
        if user_id in existing:
            return
        updated = sorted(existing + [user_id])
        await self._repository.set_value(self.KNOWN_USER_IDS_KEY, json.dumps(updated))

    async def list_known_user_ids(self) -> list[int]:
        raw_value = await self._repository.get_value(self.KNOWN_USER_IDS_KEY)
        if raw_value is None or not raw_value.strip():
            return []
        try:
            payload = json.loads(raw_value)
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        clean_ids: list[int] = []
        for value in payload:
            try:
                normalized = self._normalize_user_id(value)
            except EventSettingsValidationError:
                continue
            if normalized not in clean_ids:
                clean_ids.append(normalized)
        return sorted(clean_ids)

    @staticmethod
    def _require_non_empty(value: str, field_name: str) -> str:
        if not isinstance(value, str):
            raise EventSettingsValidationError(f"{field_name} must be a string.")
        clean = value.strip()
        if not clean:
            raise EventSettingsValidationError(f"{field_name} must not be empty.")
        return clean

    @staticmethod
    def _normalize_user_id(value: object) -> int:
        try:
            user_id = int(value)
        except (TypeError, ValueError) as exc:
            raise EventSettingsValidationError("telegram_user_id must be an integer.") from exc
        if user_id <= 0:
            raise EventSettingsValidationError("telegram_user_id must be greater than zero.")
        return user_id

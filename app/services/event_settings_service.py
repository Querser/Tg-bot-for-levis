from __future__ import annotations

from typing import Protocol, runtime_checkable


class EventSettingsValidationError(ValueError):
    """Raised when event settings payload is invalid."""


@runtime_checkable
class AppSettingsRepositoryProtocol(Protocol):
    async def get_value(self, key: str) -> str | None: ...

    async def set_value(self, key: str, value: str) -> None: ...


class EventSettingsService:
    EVENT_ADDRESS_KEY = "event_address"

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

    @staticmethod
    def _require_non_empty(value: str, field_name: str) -> str:
        if not isinstance(value, str):
            raise EventSettingsValidationError(f"{field_name} must be a string.")
        clean = value.strip()
        if not clean:
            raise EventSettingsValidationError(f"{field_name} must not be empty.")
        return clean

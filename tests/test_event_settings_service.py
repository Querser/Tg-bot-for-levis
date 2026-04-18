from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.event_settings_service import EventSettingsService, EventSettingsValidationError


class InMemoryAppSettingsRepository:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    async def get_value(self, key: str) -> str | None:
        return self._data.get(key)

    async def set_value(self, key: str, value: str) -> None:
        self._data[key] = value


@pytest.mark.asyncio
async def test_ticket_price_uses_default_and_can_be_updated() -> None:
    repository = InMemoryAppSettingsRepository()
    service = EventSettingsService(
        repository=repository,
        default_event_address="Test address",
        default_ticket_price_rub=Decimal("299.00"),
    )

    assert await service.get_ticket_price_rub() == Decimal("299.00")

    updated = await service.set_ticket_price_rub("450,50")
    assert updated == Decimal("450.50")
    assert await service.get_ticket_price_rub() == Decimal("450.50")


@pytest.mark.asyncio
async def test_ticket_price_validation_rejects_invalid_values() -> None:
    repository = InMemoryAppSettingsRepository()
    service = EventSettingsService(
        repository=repository,
        default_event_address="Test address",
        default_ticket_price_rub=Decimal("299.00"),
    )

    with pytest.raises(EventSettingsValidationError):
        await service.set_ticket_price_rub("abc")

    with pytest.raises(EventSettingsValidationError):
        await service.set_ticket_price_rub("0")

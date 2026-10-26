"""Switch platform for SVC Richcomm UPS (Beeper Control)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import SvcUpsConfigEntry, SvcUpsCoordinator
from .entity import SvcUpsEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SvcUpsConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Создание переключателя звука бипера."""
    async_add_entities([SvcUpsBeeperSwitch(entry.runtime_data)])


class SvcUpsBeeperSwitch(SvcUpsEntity, SwitchEntity):
    """Переключатель звуковой сигнализации (бит b0 ответа Q1)."""

    _attr_translation_key = "beeper"

    def __init__(self, coordinator: SvcUpsCoordinator) -> None:
        super().__init__(coordinator, "beeper_switch")  # прежний unique_id

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.beeper_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_beeper(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_beeper(False)

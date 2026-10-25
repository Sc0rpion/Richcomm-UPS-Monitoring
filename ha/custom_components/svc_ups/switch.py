"""Switch platform for SVC Richcomm UPS (Beeper Control)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SvcUpsCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Создание переключателя звука бипера."""
    coordinator: SvcUpsCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([SvcUpsBeeperSwitch(coordinator, entry)])


class SvcUpsBeeperSwitch(CoordinatorEntity[SvcUpsCoordinator], SwitchEntity):
    """Переключатель звуковой сигнализации (пищалки) ИБП."""

    def __init__(self, coordinator: SvcUpsCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_beeper_switch"
        self._attr_name = "SVC UPS Звуковая сигнализация (Бипер)"
        self._attr_icon = "mdi:volume-high"

    @property
    def device_info(self) -> DeviceInfo:
        """Информация об устройстве: Версия ПО и Версия аппаратного обеспечения."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.entry.entry_id)},
            name=f"{self.coordinator.company} {self.coordinator.model}",
            manufacturer=self.coordinator.company,
            model=self.coordinator.model,
            sw_version=self.coordinator.sw_version,
            hw_version=self.coordinator.hw_version,
        )

    @property
    def is_on(self) -> bool:
        """Включена ли пищалка (бит b0 ответа Q1)."""
        return bool(self.coordinator.data.get("beeper"))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Включение звука с проверкой изменения бита."""
        await self.coordinator.async_set_beeper(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Выключение звука (тихий режим) с проверкой изменения бита."""
        await self.coordinator.async_set_beeper(False)

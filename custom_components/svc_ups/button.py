"""Button platform for SVC Richcomm UPS (Self-Test Controls)."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
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
    """Создание кнопок управления ИБП."""
    coordinator: SvcUpsCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        [
            SvcUpsStartTestButton(coordinator, entry),
            SvcUpsCancelTestButton(coordinator, entry),
        ]
    )


class SvcUpsStartTestButton(CoordinatorEntity[SvcUpsCoordinator], ButtonEntity):
    """Кнопка запуска 10-секундного теста батареи."""

    def __init__(self, coordinator: SvcUpsCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_start_self_test"
        self._attr_name = "SVC UPS Запустить самотестирование (10 сек)"
        self._attr_icon = "mdi:battery-heart-variant"

    @property
    def device_info(self) -> DeviceInfo:
        """Динамическая информация об устройстве из чипа ИБП."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.entry.entry_id)},
            name=f"{self.coordinator.company} {self.coordinator.model}",
            manufacturer=self.coordinator.company,
            model=self.coordinator.model,
            sw_version=self.coordinator.firmware_version,
        )

    async def async_press(self) -> None:
        """Нажатие кнопки: отправка T\\r."""
        await self.coordinator.async_start_self_test()


class SvcUpsCancelTestButton(CoordinatorEntity[SvcUpsCoordinator], ButtonEntity):
    """Кнопка отмены самотестирования."""

    def __init__(self, coordinator: SvcUpsCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_cancel_self_test"
        self._attr_name = "SVC UPS Отменить тестирование"
        self._attr_icon = "mdi:cancel"

    @property
    def device_info(self) -> DeviceInfo:
        """Динамическая информация об устройстве из чипа ИБП."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.entry.entry_id)},
            name=f"{self.coordinator.company} {self.coordinator.model}",
            manufacturer=self.coordinator.company,
            model=self.coordinator.model,
            sw_version=self.coordinator.firmware_version,
        )

    async def async_press(self) -> None:
        """Нажатие кнопки: отправка CT\\r."""
        await self.coordinator.async_cancel_self_test()

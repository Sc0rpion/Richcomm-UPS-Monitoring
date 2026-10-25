"""Binary sensor platform for SVC Richcomm UPS."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SvcUpsCoordinator


@dataclass
class SvcUpsBinarySensorEntityDescription(BinarySensorEntityDescription):
    is_on_fn: Callable[[dict[str, Any]], bool] = lambda data: False


BINARY_SENSOR_TYPES: tuple[SvcUpsBinarySensorEntityDescription, ...] = (
    SvcUpsBinarySensorEntityDescription(
        key="ac_power",
        name="Сеть 220V подключена",
        device_class=BinarySensorDeviceClass.PLUG,
        is_on_fn=lambda data: data.get("ac_status") == "Normal",
    ),
    SvcUpsBinarySensorEntityDescription(
        key="battery_low",
        name="Батарея разряжена",
        device_class=BinarySensorDeviceClass.BATTERY,
        is_on_fn=lambda data: data.get("battery_status") == "Battery Low",
    ),
    SvcUpsBinarySensorEntityDescription(
        key="boost_buck",
        name="Стабилизатор AVR (Boost/Buck)",
        icon="mdi:sine-wave",
        is_on_fn=lambda data: bool(data.get("boost_buck")),
    ),
    SvcUpsBinarySensorEntityDescription(
        key="ups_problem",
        name="Авария / Отказ ИБП",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda data: data.get("ups_status") == "Failed",
    ),
    SvcUpsBinarySensorEntityDescription(
        key="test_in_progress",
        name="Самотестирование активно",
        icon="mdi:progress-check",
        is_on_fn=lambda data: bool(data.get("test_in_progress")),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Создание бинарных сенсоров ИБП."""
    coordinator: SvcUpsCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        SvcUpsBinarySensor(coordinator, entry, description)
        for description in BINARY_SENSOR_TYPES
    ]
    async_add_entities(entities)


class SvcUpsBinarySensor(CoordinatorEntity[SvcUpsCoordinator], BinarySensorEntity):
    """Сущность бинарного сенсора SVC UPS."""

    entity_description: SvcUpsBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: SvcUpsCoordinator,
        entry: ConfigEntry,
        description: SvcUpsBinarySensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entry = entry
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_name = f"SVC UPS {description.name}"

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
        return self.entity_description.is_on_fn(self.coordinator.data)

"""Binary sensor platform for SVC Richcomm UPS."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import SvcUpsConfigEntry
from .entity import SvcUpsEntity
from .model import SvcUpsData

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class SvcUpsBinarySensorDescription(BinarySensorEntityDescription):
    is_on_fn: Callable[[SvcUpsData], bool]


BINARY_SENSOR_TYPES: tuple[SvcUpsBinarySensorDescription, ...] = (
    SvcUpsBinarySensorDescription(
        key="ac_power",
        translation_key="ac_power",
        device_class=BinarySensorDeviceClass.PLUG,
        is_on_fn=lambda d: d.ac_ok,
    ),
    SvcUpsBinarySensorDescription(
        key="battery_low",
        translation_key="battery_low",
        device_class=BinarySensorDeviceClass.BATTERY,
        is_on_fn=lambda d: d.battery_low,
    ),
    SvcUpsBinarySensorDescription(key="boost_buck", translation_key="boost_buck", is_on_fn=lambda d: d.bypass),
    SvcUpsBinarySensorDescription(
        key="ups_problem",
        translation_key="ups_problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda d: d.ups_failed,
    ),
    SvcUpsBinarySensorDescription(
        key="test_in_progress",
        translation_key="test_in_progress",
        is_on_fn=lambda d: d.test_in_progress,
    ),
    SvcUpsBinarySensorDescription(
        key="shutdown_active",
        translation_key="shutdown_active",
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda d: d.shutdown_active,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SvcUpsConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Создание бинарных сенсоров ИБП."""
    coordinator = entry.runtime_data
    async_add_entities(SvcUpsBinarySensor(coordinator, d) for d in BINARY_SENSOR_TYPES)


class SvcUpsBinarySensor(SvcUpsEntity, BinarySensorEntity):
    """Сущность бинарного сенсора SVC UPS."""

    entity_description: SvcUpsBinarySensorDescription

    def __init__(self, coordinator, description: SvcUpsBinarySensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool:
        return self.entity_description.is_on_fn(self.coordinator.data)

"""Sensor platform for SVC Richcomm UPS."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfFrequency,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType

from .coordinator import SvcUpsConfigEntry
from .entity import SvcUpsEntity
from .model import SvcUpsData

PARALLEL_UPDATES = 0  # данные берутся из координатора, обращений к устройству нет


@dataclass(frozen=True, kw_only=True)
class SvcUpsSensorDescription(SensorEntityDescription):
    value_fn: Callable[[SvcUpsData], StateType]


def _measure(key: str, device_class, unit, fn, precision: int | None = 1, **kw) -> SvcUpsSensorDescription:
    return SvcUpsSensorDescription(
        key=key,
        translation_key=key,
        device_class=device_class,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=unit,
        suggested_display_precision=precision,
        value_fn=fn,
        **kw,
    )


_DIAG = {"entity_category": EntityCategory.DIAGNOSTIC}
_DIAG_OFF = {**_DIAG, "entity_registry_enabled_default": False}

SENSOR_TYPES: tuple[SvcUpsSensorDescription, ...] = (
    _measure("input_voltage", SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT, lambda d: d.input_voltage),
    _measure("output_voltage", SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT, lambda d: d.output_voltage),
    _measure("battery_voltage", SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT, lambda d: d.battery_voltage, 2),
    _measure("battery_level", SensorDeviceClass.BATTERY, PERCENTAGE, lambda d: d.battery_level, 0),
    _measure("load_percent", None, PERCENTAGE, lambda d: d.load_percent, 0),
    _measure("frequency", SensorDeviceClass.FREQUENCY, UnitOfFrequency.HERTZ, lambda d: d.frequency),
    _measure("temperature", SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS, lambda d: d.temperature),
    SvcUpsSensorDescription(
        key="ac_status",
        translation_key="ac_status",
        device_class=SensorDeviceClass.ENUM,
        options=["normal", "utility_fail"],
        value_fn=lambda d: d.ac_state,
    ),
    SvcUpsSensorDescription(
        key="ups_status",
        translation_key="ups_status",
        device_class=SensorDeviceClass.ENUM,
        options=["normal", "failed"],
        value_fn=lambda d: d.ups_state,
    ),
    SvcUpsSensorDescription(key="model", translation_key="model", value_fn=lambda d: d.identity.model, **_DIAG),
    SvcUpsSensorDescription(
        key="firmware_version",
        translation_key="firmware_version",
        value_fn=lambda d: d.identity.version or None,
        **_DIAG,
    ),
    # номиналы из ответа F
    _measure(
        "rated_voltage",
        SensorDeviceClass.VOLTAGE,
        UnitOfElectricPotential.VOLT,
        lambda d: d.rating.voltage if d.rating else None,
        **_DIAG_OFF,
    ),
    _measure(
        "rated_current",
        SensorDeviceClass.CURRENT,
        UnitOfElectricCurrent.AMPERE,
        lambda d: d.rating.current if d.rating else None,
        **_DIAG_OFF,
    ),
    _measure(
        "rated_battery_voltage",
        SensorDeviceClass.VOLTAGE,
        UnitOfElectricPotential.VOLT,
        lambda d: d.rating.battery_voltage if d.rating else None,
        **_DIAG_OFF,
    ),
    _measure(
        "rated_frequency",
        SensorDeviceClass.FREQUENCY,
        UnitOfFrequency.HERTZ,
        lambda d: d.rating.frequency if d.rating else None,
        **_DIAG_OFF,
    ),
    SvcUpsSensorDescription(
        key="raw_response",
        translation_key="raw_response",
        value_fn=lambda d: d.raw_response,
        **_DIAG_OFF,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SvcUpsConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Создание сенсоров ИБП."""
    coordinator = entry.runtime_data
    async_add_entities(SvcUpsSensor(coordinator, description) for description in SENSOR_TYPES)


class SvcUpsSensor(SvcUpsEntity, SensorEntity):
    """Сущность сенсора SVC UPS."""

    entity_description: SvcUpsSensorDescription

    def __init__(self, coordinator, description: SvcUpsSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> StateType:
        return self.entity_description.value_fn(self.coordinator.data)

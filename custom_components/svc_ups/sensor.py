"""Sensor platform for SVC Richcomm UPS."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricPotential,
    UnitOfFrequency,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SvcUpsCoordinator


@dataclass
class SvcUpsSensorEntityDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any] = lambda data: None


SENSOR_TYPES: tuple[SvcUpsSensorEntityDescription, ...] = (
    SvcUpsSensorEntityDescription(
        key="input_voltage",
        name="Входное напряжение",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        value_fn=lambda data: data.get("input_voltage"),
    ),
    SvcUpsSensorEntityDescription(
        key="output_voltage",
        name="Выходное напряжение",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        value_fn=lambda data: data.get("output_voltage"),
    ),
    SvcUpsSensorEntityDescription(
        key="battery_voltage",
        name="Напряжение батареи",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        value_fn=lambda data: data.get("battery_voltage"),
    ),
    SvcUpsSensorEntityDescription(
        key="battery_level",
        name="Заряд батареи",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda data: data.get("battery_level"),
    ),
    SvcUpsSensorEntityDescription(
        key="load_percent",
        name="Нагрузка",
        icon="mdi:gauge",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda data: data.get("load_percent"),
    ),
    SvcUpsSensorEntityDescription(
        key="frequency",
        name="Частота сети",
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        value_fn=lambda data: data.get("frequency"),
    ),
    SvcUpsSensorEntityDescription(
        key="temperature",
        name="Температура",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        value_fn=lambda data: data.get("temperature"),
    ),
    SvcUpsSensorEntityDescription(
        key="ac_status",
        name="Статус сети 220V",
        icon="mdi:power-plug",
        value_fn=lambda data: data.get("ac_status"),
    ),
    SvcUpsSensorEntityDescription(
        key="ups_status",
        name="Статус ИБП",
        icon="mdi:information-outline",
        value_fn=lambda data: data.get("ups_status"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Создание сенсоров ИБП."""
    coordinator: SvcUpsCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        SvcUpsSensor(coordinator, entry, description)
        for description in SENSOR_TYPES
    ]
    async_add_entities(entities)


class SvcUpsSensor(CoordinatorEntity[SvcUpsCoordinator], SensorEntity):
    """Сущность сенсора SVC UPS."""

    entity_description: SvcUpsSensorEntityDescription

    def __init__(
        self,
        coordinator: SvcUpsCoordinator,
        entry: ConfigEntry,
        description: SvcUpsSensorEntityDescription,
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
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)

"""Base entity for SVC Richcomm UPS."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SvcUpsCoordinator


class SvcUpsEntity(CoordinatorEntity[SvcUpsCoordinator]):
    """Общая база: устройство, unique_id, имена через translation_key."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SvcUpsCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        identity = coordinator.identity
        # unique_id сохранён прежним: {entry_id}_{key} - история и настройки не теряются
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"{identity.company} {identity.model}",
            manufacturer=identity.company,
            model=identity.model,
            sw_version=identity.version or None,
        )

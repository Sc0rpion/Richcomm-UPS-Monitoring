"""Diagnostics for SVC Richcomm UPS."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_PHYS
from .coordinator import SvcUpsConfigEntry

TO_REDACT = {CONF_PHYS}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: SvcUpsConfigEntry) -> dict[str, Any]:
    coordinator = entry.runtime_data
    data = coordinator.data
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "driver": async_redact_data(coordinator.driver.diagnostics(), TO_REDACT),
        "glitch_filter": coordinator.glitch_filter.stats,
        "identity": asdict(coordinator.identity),
        "rating": asdict(coordinator.rating) if coordinator.rating else None,
        "last_update_success": coordinator.last_update_success,
        "reading": asdict(data.reading) if data else None,
        "status_byte": f"0x{data.status_byte:02X} ({data.status_byte:08b})" if data else None,
        "battery_level": data.battery_level if data else None,
    }

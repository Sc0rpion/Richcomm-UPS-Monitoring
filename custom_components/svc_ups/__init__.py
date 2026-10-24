"""The SVC Richcomm UPS Home Assistant Integration."""

from __future__ import annotations

import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN, CONF_DEVICE, CONF_BAUD, CONF_SCAN_INTERVAL, DEFAULT_DEVICE, DEFAULT_BAUD, DEFAULT_SCAN_INTERVAL
from .coordinator import SvcUpsCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.BUTTON,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Настройка интеграции из UI Config Flow."""
    device = entry.data.get(CONF_DEVICE, DEFAULT_DEVICE)
    baud = entry.data.get(CONF_BAUD, DEFAULT_BAUD)
    scan_interval = entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)

    coordinator = SvcUpsCoordinator(hass, device, baud, scan_interval)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Регистрация служб
    async def handle_toggle_beeper(call):
        await coordinator.async_toggle_beeper()

    async def handle_start_self_test(call):
        await coordinator.async_start_self_test()

    async def handle_cancel_self_test(call):
        await coordinator.async_cancel_self_test()

    hass.services.async_register(DOMAIN, "toggle_beeper", handle_toggle_beeper)
    hass.services.async_register(DOMAIN, "start_self_test", handle_start_self_test)
    hass.services.async_register(DOMAIN, "cancel_self_test", handle_cancel_self_test)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Выгрузка интеграции."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await hass.async_add_executor_job(coordinator.close)

    return unload_ok

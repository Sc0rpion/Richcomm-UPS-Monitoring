"""The SVC Richcomm UPS Home Assistant Integration."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_ENTRY_ID,
    DOMAIN,
    SERVICE_CANCEL_SELF_TEST,
    SERVICE_START_SELF_TEST,
    SERVICE_TOGGLE_BEEPER,
)
from .coordinator import SvcUpsConfigEntry, SvcUpsCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.SWITCH,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
SERVICE_SCHEMA = vol.Schema({vol.Optional(ATTR_ENTRY_ID): cv.string})

# service -> метод координатора
_SERVICES = {
    SERVICE_TOGGLE_BEEPER: "async_toggle_beeper",
    SERVICE_START_SELF_TEST: "async_start_self_test",
    SERVICE_CANCEL_SELF_TEST: "async_cancel_self_test",
}


def _target_coordinators(hass: HomeAssistant, call: ServiceCall) -> list[SvcUpsCoordinator]:
    entries = [e for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED]
    wanted = call.data.get(ATTR_ENTRY_ID)
    if wanted:
        entries = [e for e in entries if e.entry_id == wanted]
        if not entries:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="entry_not_found",
                translation_placeholders={"entry_id": wanted},
            )
    return [e.runtime_data for e in entries]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Службы регистрируются один раз на домен (раньше - на каждую запись, с общим замыканием
    на последний координатор, и не снимались при выгрузке)."""

    def _make_handler(method: str):
        async def _handler(call: ServiceCall) -> None:
            for coordinator in _target_coordinators(hass, call):
                await getattr(coordinator, method)()

        return _handler

    for service, method in _SERVICES.items():
        hass.services.async_register(DOMAIN, service, _make_handler(method), schema=SERVICE_SCHEMA)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SvcUpsConfigEntry) -> bool:
    """Настройка интеграции из UI Config Flow."""
    coordinator = SvcUpsCoordinator(hass, entry)
    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady:
        await coordinator.async_shutdown()  # не оставлять открытый fd и поток-читатель
        raise

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: SvcUpsConfigEntry) -> None:
    """Перезагрузка при смене параметров (интервал опроса и т.п.). schedule_reload объединяет
    перезагрузки, если одновременно сработало и «Перенастроить»."""
    hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: SvcUpsConfigEntry) -> bool:
    """Выгрузка интеграции."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_shutdown()
    return unload_ok

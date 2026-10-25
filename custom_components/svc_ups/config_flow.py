"""Config flow for SVC Richcomm UPS integration."""

from __future__ import annotations

import glob
import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .const import (
    DOMAIN,
    DEFAULT_NAME,
    DEFAULT_DEVICE,
    DEFAULT_BAUD,
    DEFAULT_SCAN_INTERVAL,
    CONF_DEVICE,
    CONF_BAUD,
    CONF_SCAN_INTERVAL,
)
from .driver import SvcUpsHardware

_LOGGER = logging.getLogger(__name__)


def _find_candidate_devices() -> list[str]:
    devs = sorted(glob.glob("/dev/usb/hiddev*") + glob.glob("/dev/hiddev*"))
    if not devs:
        devs = [DEFAULT_DEVICE]
    return devs


async def _validate_device(hass: HomeAssistant, device: str, baud: int) -> bool:
    def _test():
        hw = SvcUpsHardware(dev_path=device, baud=baud)
        res = hw.query_status()
        hw.close()
        return res is not None

    return await hass.async_add_executor_job(_test)


class SvcUpsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Конфигурационный мастер добавления SVC UPS через UI."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Первый шаг настройки пользователем."""
        errors: dict[str, str] = {}

        if user_input is not None:
            device = user_input[CONF_DEVICE]
            baud = user_input.get(CONF_BAUD, DEFAULT_BAUD)

            # Проверка соединения в пуле потоков executor
            is_valid = await _validate_device(self.hass, device, baud)
            if is_valid:
                await self.async_set_unique_id(f"svc_ups_{device}")
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=f"SVC UPS ({device})",
                    data=user_input,
                )
            else:
                errors["base"] = "cannot_connect"

        # Сканирование файловой системы строго в executor, чтобы не блокировать event loop
        candidate_devices = await self.hass.async_add_executor_job(_find_candidate_devices)

        schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE, default=candidate_devices[0]): str,
                vol.Optional(CONF_BAUD, default=DEFAULT_BAUD): int,
                vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): int,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

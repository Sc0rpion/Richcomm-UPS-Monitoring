"""Config flow for SVC Richcomm UPS integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_BAUD,
    CONF_DEVICE,
    CONF_PHYS,
    CONF_SCAN_INTERVAL,
    DEFAULT_BAUD,
    DEFAULT_DEVICE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .coordinator import scan_interval_of
from .driver import PID, VID, Candidate, Identity, SvcUpsHardware, discover

_LOGGER = logging.getLogger(__name__)


@dataclass
class ProbeResult:
    error: Optional[str] = None
    phys: str = ""
    identity: Optional[Identity] = None


def probe_ups(device: str, baud: int) -> ProbeResult:
    """Блокирующая проверка: узел открывается, это именно мост Richcomm и ИБП отвечает на Q1.
    (Раньше проверка всегда проходила: драйвер подставлял выдуманный статус.)"""
    hw = SvcUpsHardware(dev_path=device, baud=baud)
    try:
        if not hw.open():
            return ProbeResult(error="cannot_connect")
        if hw.query_status(attempts=3) is None:
            return ProbeResult(error="no_response")
        return ProbeResult(phys=hw.phys, identity=hw.query_identity())
    finally:
        hw.close()


def _unique_id(phys: str, device: str) -> str:
    # phys стабилен между перезагрузками (порт USB), путь hiddevN - нет
    return f"{VID:04x}_{PID:04x}_{phys}" if phys else f"svc_ups_{device}"


def _device_schema(candidates: list[Candidate], device: str, baud: int, with_interval: bool, interval: int = DEFAULT_SCAN_INTERVAL) -> vol.Schema:
    options = [SelectOptionDict(value=c.path, label=f"{c.path} ({c.phys})" if c.phys else c.path) for c in candidates]
    fields: dict[Any, Any] = {
        vol.Required(CONF_DEVICE, default=device): SelectSelector(
            SelectSelectorConfig(options=options, custom_value=True, mode=SelectSelectorMode.DROPDOWN)
        ),
        vol.Optional(CONF_BAUD, default=baud): NumberSelector(
            NumberSelectorConfig(min=300, max=115200, step=1, mode=NumberSelectorMode.BOX)
        ),
    }
    if with_interval:
        fields[vol.Optional(CONF_SCAN_INTERVAL, default=interval)] = _interval_selector()
    return vol.Schema(fields)


def _interval_selector() -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=MIN_SCAN_INTERVAL,
            max=MAX_SCAN_INTERVAL,
            step=1,
            unit_of_measurement="s",
            mode=NumberSelectorMode.BOX,
        )
    )


class SvcUpsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Конфигурационный мастер добавления SVC UPS через UI."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "SvcUpsOptionsFlow":
        return SvcUpsOptionsFlow()

    async def async_step_user(self, user_input: Optional[dict[str, Any]] = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        # Сканирование открывает узлы /dev -> только в executor (раньше glob блокировал цикл событий)
        candidates = await self.hass.async_add_executor_job(discover)

        if user_input is not None:
            device = str(user_input[CONF_DEVICE]).strip()
            baud = int(user_input.get(CONF_BAUD, DEFAULT_BAUD))
            interval = int(user_input.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
            if any(e.data.get(CONF_DEVICE) == device for e in self._async_current_entries()):
                return self.async_abort(reason="already_configured")
            result = await self.hass.async_add_executor_job(probe_ups, device, baud)
            if result.error is None:
                await self.async_set_unique_id(_unique_id(result.phys, device))
                self._abort_if_unique_id_configured(updates={CONF_DEVICE: device})
                ident = result.identity
                return self.async_create_entry(
                    title=f"{ident.company} {ident.model}" if ident else f"SVC UPS ({device})",
                    data={CONF_DEVICE: device, CONF_BAUD: baud, CONF_PHYS: result.phys},
                    options={CONF_SCAN_INTERVAL: interval},
                )
            errors["base"] = result.error

        default = candidates[0].path if candidates else DEFAULT_DEVICE
        return self.async_show_form(
            step_id="user",
            data_schema=_device_schema(candidates, default, DEFAULT_BAUD, True),
            errors=errors,
            description_placeholders={"found": ", ".join(c.path for c in candidates) or "—"},
        )

    async def async_step_reconfigure(self, user_input: Optional[dict[str, Any]] = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        candidates = await self.hass.async_add_executor_job(discover)

        if user_input is not None:
            device = str(user_input[CONF_DEVICE]).strip()
            baud = int(user_input.get(CONF_BAUD, DEFAULT_BAUD))
            result = await self.hass.async_add_executor_job(probe_ups, device, baud)
            if result.error is None:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={CONF_DEVICE: device, CONF_BAUD: baud, CONF_PHYS: result.phys},
                )
            errors["base"] = result.error

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_device_schema(
                candidates,
                entry.data.get(CONF_DEVICE, DEFAULT_DEVICE),
                int(entry.data.get(CONF_BAUD, DEFAULT_BAUD)),
                False,
            ),
            errors=errors,
            description_placeholders={"found": ", ".join(c.path for c in candidates) or "—"},
        )


class SvcUpsOptionsFlow(OptionsFlow):
    """Параметры: интервал опроса."""

    async def async_step_init(self, user_input: Optional[dict[str, Any]] = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data={CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])})
        schema = vol.Schema(
            {vol.Optional(CONF_SCAN_INTERVAL, default=scan_interval_of(self.config_entry)): _interval_selector()}
        )
        return self.async_show_form(step_id="init", data_schema=schema)

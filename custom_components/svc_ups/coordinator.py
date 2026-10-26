"""DataUpdateCoordinator for SVC Richcomm UPS."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Callable, Optional

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_BAUD,
    CONF_DEVICE,
    CONF_PHYS,
    CONF_SCAN_INTERVAL,
    DEFAULT_BAUD,
    DEFAULT_DEVICE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SOFT_FAILURES,
    MIN_SCAN_INTERVAL,
)
from .driver import Identity, Rating, StatusReading, SvcUpsHardware, UpsError
from .model import GlitchFilter, SvcUpsData, build_data

_LOGGER = logging.getLogger(__name__)

SvcUpsConfigEntry = ConfigEntry["SvcUpsCoordinator"]

_META_MAX_TRIES = 3  # паспорт (I) и номиналы (F) запрашиваем не более 3 раз каждый
_META_EVERY = 6  # ...и не чаще раза в 6 опросов


def scan_interval_of(entry: ConfigEntry) -> int:
    value = entry.options.get(CONF_SCAN_INTERVAL, entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
    return max(MIN_SCAN_INTERVAL, int(value))


class SvcUpsCoordinator(DataUpdateCoordinator[SvcUpsData]):
    """Опрос ИБП (Q1), разовый запрос паспорта (I) и номиналов (F), команды управления."""

    config_entry: SvcUpsConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: SvcUpsConfigEntry,
        driver: Optional[SvcUpsHardware] = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(seconds=scan_interval_of(entry)),
        )
        self.driver = driver or SvcUpsHardware(
            dev_path=entry.data.get(CONF_DEVICE, DEFAULT_DEVICE),
            baud=int(entry.data.get(CONF_BAUD, DEFAULT_BAUD)),
            phys=entry.data.get(CONF_PHYS),
        )
        self.identity = Identity()
        self.rating: Optional[Rating] = None
        self._identity_tries = 0
        self._rating_tries = 0
        self._updates = 0
        self._failures = 0
        self._meta_dirty = False
        self.glitch_filter = GlitchFilter()

    # ----- опрос -----
    async def _async_update_data(self) -> SvcUpsData:
        data = await self.hass.async_add_executor_job(self._update_sync)
        if self._meta_dirty:
            self._meta_dirty = False
            self._async_update_device_registry()
        return data

    def _load_metadata(self) -> None:
        if self.rating is None and self._rating_tries < _META_MAX_TRIES:
            self._rating_tries += 1
            rating = self.driver.query_rating()
            if rating is not None:
                self.rating = rating
                self._meta_dirty = True
        if self._identity_tries < _META_MAX_TRIES and self.identity == Identity():
            self._identity_tries += 1
            identity = self.driver.query_identity()
            if identity is not None and identity != self.identity:
                self.identity = identity
                self._meta_dirty = True
                _LOGGER.info(
                    "Паспорт ИБП: производитель=%s, модель=%s, прошивка=%s",
                    identity.company,
                    identity.model,
                    identity.version or "?",
                )

    def _update_sync(self) -> SvcUpsData:
        reading = self.driver.query_status()
        if reading is None:
            self._failures += 1
            if self.data is not None and self._failures < MAX_SOFT_FAILURES:
                _LOGGER.debug("ИБП не ответил (%d/%d), оставляю прошлые значения", self._failures, MAX_SOFT_FAILURES)
                return self.data
            raise UpdateFailed("ИБП не вернул корректный статус (Q1)")
        self._failures = 0
        reading = self.glitch_filter.accept(reading, lambda: self.driver.query_status(attempts=1))
        if reading is None:  # выброс не подтверждён: ИБП отвечает, но показание сомнительное
            if self.data is not None:
                return self.data
            raise UpdateFailed("Первое показание ИБП не подтвердилось повторным чтением")
        if self._updates % _META_EVERY == 0:
            self._load_metadata()
        self._updates += 1
        return build_data(reading, self.identity, self.rating)

    @callback
    def _async_update_device_registry(self) -> None:
        registry = dr.async_get(self.hass)
        device = registry.async_get_device(identifiers={(DOMAIN, self.config_entry.entry_id)})
        if device is not None:
            registry.async_update_device(
                device.id,
                manufacturer=self.identity.company,
                model=self.identity.model,
                sw_version=self.identity.version or None,
            )

    # ----- команды -----
    async def _async_command(self, func: Callable[[], Optional[StatusReading]]) -> None:
        """Выполнить команду в executor и сразу опубликовать подтверждённое состояние.
        async_request_refresh не годится: у него дебаунс 10 с, и интерфейс показывал
        старое состояние бипера."""
        try:
            reading = await self.hass.async_add_executor_job(func)
        except UpsError as err:
            await self.async_refresh()  # показать реальное состояние
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        if reading is None:
            await self.async_refresh()
            return
        self._failures = 0
        self.async_set_updated_data(build_data(reading, self.identity, self.rating))

    async def async_set_beeper(self, enable: bool) -> None:
        await self._async_command(lambda: self.driver.set_beeper(enable))

    async def async_toggle_beeper(self) -> None:
        await self._async_command(self.driver.toggle_beeper)

    async def async_start_self_test(self) -> None:
        await self._async_command(self.driver.start_self_test)

    async def async_cancel_self_test(self) -> None:
        await self._async_command(self.driver.cancel_self_test)

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        await self.hass.async_add_executor_job(self.driver.close)

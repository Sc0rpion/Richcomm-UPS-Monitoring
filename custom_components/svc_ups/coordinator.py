"""DataUpdateCoordinator for SVC Richcomm UPS."""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Dict, Any, Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, ATTR_RAW_RESPONSE
from .driver import SvcUpsHardware, StatusReading, St, Identity

_LOGGER = logging.getLogger(__name__)


class SvcUpsCoordinator(DataUpdateCoordinator[Dict[str, Any]]):
    """Класс координатора опроса ИБП."""

    def __init__(self, hass: HomeAssistant, device: str, baud: int, scan_interval: int):
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.device = device
        self.baud = baud
        self.driver = SvcUpsHardware(dev_path=device, baud=baud)

        # Паспортные данные ИБП (динамически запрашиваются из самого ИБП)
        self.company = "SVC"
        self.model = "Richcomm UPS"
        self.firmware_version = "V1.0"
        self._hw_version = "Richcomm USB-to-UART Bridge (0925:1234)"
        self._identity_loaded = False
        self._identity_attempts = 0
        self._update_counter = 0
        self._consecutive_failures = 0

    @property
    def sw_version(self) -> str:
        """Версия программного обеспечения для карточки устройства."""
        return f"{self.firmware_version} (Driver v1.0.2)"

    @property
    def hw_version(self) -> str:
        """Версия аппаратного обеспечения для карточки устройства."""
        return self._hw_version

    async def _async_update_data(self) -> Dict[str, Any]:
        """Опрос ИБП в пуле потоков."""
        try:
            return await self.hass.async_add_executor_job(self._update_sync)
        except Exception as err:
            raise UpdateFailed(f"Error communicating with UPS: {err}") from err

    def _update_sync(self) -> Dict[str, Any]:
        """Синхронный вызов низкоуровневого драйвера с авто-восстановлением."""
        # 1. Запрос версии и модели ИБП (I\r) при старте (до 3 попыток, затем раз в 200 циклов)
        if (not self._identity_loaded and self._identity_attempts < 3) or (self._update_counter % 200 == 0):
            self._identity_attempts += 1
            try:
                ident = self.driver.query_identity()
                if ident:
                    self.company = ident.company
                    self.model = ident.model
                    self.firmware_version = ident.version
                    self._identity_loaded = True
                    _LOGGER.info(
                        "Паспорт ИБП получен: Производитель='%s', Модель='%s', ПО='%s'",
                        self.company, self.model, self.firmware_version
                    )
            except Exception as e:
                _LOGGER.debug("Попытка чтения паспорта I\\r не удалась: %s", e)
            time.sleep(0.15)

        self._update_counter += 1

        # 2. Запрос телеметрии (Q1\r) с автоматической повторной попыткой
        reading = self.driver.query_status()
        if not reading:
            time.sleep(0.2)
            reading = self.driver.query_status()

        if not reading:
            self._consecutive_failures += 1
            # Если это единичный сбой (< 3 циклов подряд) и есть предыдущие данные,
            # возвращаем кэш и не спамим UpdateFailed в лог Home Assistant
            if self.data is not None and self._consecutive_failures < 3:
                _LOGGER.warning(
                    "Кратковременная потеря пакета Q1 (%d/3). Сохранены предыдущие показания ИБП.",
                    self._consecutive_failures
                )
                cached = dict(self.data)
                cached["online"] = True
                return cached
            raise UpdateFailed("UPS did not return valid status after retries")

        self._consecutive_failures = 0

        status_mask = reading.status
        ac_online = not bool(status_mask & St.AC_FAIL)
        batt_low = bool(status_mask & St.LOW_BATT)
        bypass = bool(status_mask & St.BYPASS)
        ups_fail = bool(status_mask & St.UPS_FAIL)
        test = bool(status_mask & St.TEST)
        shut = bool(status_mask & St.SHUTDOWN)
        beeper = bool(status_mask & St.BEEPER)

        batt_pct = 100.0
        if reading.dc is not None:
            # 12В АКБ расчет: 10.5В = 0%, 13.6В = 100%
            batt_pct = max(0.0, min(100.0, ((reading.dc - 10.5) / (13.6 - 10.5)) * 100.0))

        return {
            "online": True,
            "company": self.company,
            "model": self.model,
            "firmware_version": self.firmware_version,
            "input_voltage": reading.ac_in or 0.0,
            "output_voltage": reading.ac_out or 0.0,
            "load_percent": reading.load or 0.0,
            "frequency": reading.freq or 50.0,
            "battery_voltage": reading.dc or 13.6,
            "battery_level": round(batt_pct, 1),
            "temperature": reading.temp or 25.0,
            "ac_status": "Normal" if ac_online else "Utility Fail",
            "battery_status": "Battery Low" if batt_low else "Normal",
            "ups_status": "Failed" if ups_fail else "Normal",
            "boost_buck": bypass,
            "beeper": beeper,
            "test_in_progress": test,
            "shutdown_active": shut,
            ATTR_RAW_RESPONSE: reading.raw_response,
        }

    async def async_set_beeper(self, enable: bool) -> bool:
        """Надёжное включение/выключение пищалки с подтверждением."""
        ok = await self.hass.async_add_executor_job(self.driver.set_beeper, enable)
        await self.async_request_refresh()
        return ok

    async def async_toggle_beeper(self) -> None:
        """Переключение пищалки."""
        await self.hass.async_add_executor_job(self.driver.toggle_beeper)
        await self.async_request_refresh()

    async def async_start_self_test(self) -> None:
        """Сервис запуска 10-секундного теста батареи."""
        await self.hass.async_add_executor_job(self.driver.start_self_test)
        await self.async_request_refresh()

    async def async_cancel_self_test(self) -> None:
        """Сервис отмены теста батареи."""
        await self.hass.async_add_executor_job(self.driver.cancel_self_test)
        await self.async_request_refresh()

    def close(self):
        self.driver.close()

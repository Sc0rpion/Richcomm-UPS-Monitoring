"""Модель данных ИБП (без зависимостей от Home Assistant)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Optional

from .driver import Identity, Rating, St, StatusReading

_LOGGER = logging.getLogger(__name__)


def estimate_battery_level(dc: Optional[float], rated_dc: Optional[float]) -> Optional[float]:
    """Оценка заряда по напряжению батареи, % (None, если напряжение неизвестно).

    * Q1 у части ИБП отдаёт напряжение НА ЯЧЕЙКУ (~2.0-2.3 В) - тогда шкала 1.75..2.27 В;
    * иначе это напряжение блока: 10.5 В = 0 %, 13.6 В = 100 % на каждые 12 В
      номинала (номинал берётся из ответа F; нет ответа - считаем 12 В).
    Это грубая оценка: под нагрузкой и при заряде напряжение «плавает».
    """
    if dc is None or dc <= 0:
        return None
    if dc < 5.0:
        empty, full = 1.75, 2.27
    else:
        cells = max(1, round(rated_dc / 12.0)) if rated_dc and rated_dc >= 6 else 1
        empty, full = 10.5 * cells, 13.6 * cells
    return round(max(0.0, min(100.0, (dc - empty) / (full - empty) * 100.0)), 1)


@dataclass(frozen=True)
class SvcUpsData:
    reading: StatusReading
    identity: Identity
    rating: Optional[Rating]
    battery_level: Optional[float]

    # --- измерения (None = ИБП не прислал годное значение) ---
    @property
    def input_voltage(self) -> Optional[float]:
        return self.reading.ac_in

    @property
    def output_voltage(self) -> Optional[float]:
        return self.reading.ac_out

    @property
    def load_percent(self) -> Optional[float]:
        return self.reading.load

    @property
    def frequency(self) -> Optional[float]:
        return self.reading.freq

    @property
    def battery_voltage(self) -> Optional[float]:
        return self.reading.dc

    @property
    def temperature(self) -> Optional[float]:
        return self.reading.temp

    @property
    def raw_response(self) -> str:
        return self.reading.raw_response

    # --- биты статуса ---
    @property
    def status_byte(self) -> int:
        return self.reading.status

    @property
    def ac_ok(self) -> bool:
        return not self.reading.status & St.AC_FAIL

    @property
    def battery_low(self) -> bool:
        return bool(self.reading.status & St.LOW_BATT)

    @property
    def bypass(self) -> bool:
        return bool(self.reading.status & St.BYPASS)

    @property
    def ups_failed(self) -> bool:
        return bool(self.reading.status & St.UPS_FAIL)

    @property
    def standby_type(self) -> bool:
        return bool(self.reading.status & St.STANDBY)

    @property
    def test_in_progress(self) -> bool:
        return bool(self.reading.status & St.TEST)

    @property
    def shutdown_active(self) -> bool:
        return bool(self.reading.status & St.SHUTDOWN)

    @property
    def beeper_on(self) -> bool:
        return bool(self.reading.status & St.BEEPER)

    # --- состояния для ENUM-сенсоров ---
    @property
    def ac_state(self) -> str:
        return "normal" if self.ac_ok else "utility_fail"

    @property
    def battery_state(self) -> str:
        return "low" if self.battery_low else "normal"

    @property
    def ups_state(self) -> str:
        return "failed" if self.ups_failed else "normal"


def build_data(reading: StatusReading, identity: Identity, rating: Optional[Rating]) -> SvcUpsData:
    rated_dc = rating.battery_voltage if rating else None
    return SvcUpsData(
        reading=reading,
        identity=identity,
        rating=rating,
        battery_level=estimate_battery_level(reading.dc, rated_dc),
    )


# --------------------------------------------------------------------------- #
# Защита от одиночных выбросов
# --------------------------------------------------------------------------- #
CRITICAL_BITS = St.AC_FAIL | St.LOW_BATT | St.UPS_FAIL
MIN_PLAUSIBLE_INPUT = 50.0  # ниже - вход «умер»; ИБП обязан при этом показывать Utility Fail (b7)


def _rel_jump(a: Optional[float], b: Optional[float], frac: float, floor: float) -> bool:
    if a is None or b is None:
        return (a is None) != (b is None)
    return abs(a - b) > frac * max(abs(a), abs(b), floor)


def _abs_jump(a: Optional[float], b: Optional[float], limit: float) -> bool:
    if a is None or b is None:
        return (a is None) != (b is None)
    return abs(a - b) > limit


def readings_differ(a: StatusReading, b: StatusReading) -> bool:
    """Показания расходятся настолько, что это не обычный дрейф сети/нагрузки."""
    if (a.status ^ b.status) & CRITICAL_BITS:
        return True
    return (
        _rel_jump(a.ac_in, b.ac_in, 0.30, 50)
        or _rel_jump(a.ac_out, b.ac_out, 0.30, 50)
        or _rel_jump(a.freq, b.freq, 0.10, 40)
        or _rel_jump(a.dc, b.dc, 0.25, 5)
        or _abs_jump(a.load, b.load, 40)
        or _abs_jump(a.temp, b.temp, 15)
    )


def is_inconsistent(r: StatusReading) -> bool:
    """Вход < 50 В при сброшенном флаге Utility Fail: ИБП противоречит сам себе (обычно битый кадр)."""
    return r.ac_in is not None and r.ac_in < MIN_PLAUSIBLE_INPUT and not r.status & St.AC_FAIL


class GlitchFilter:
    """Не пускает в Home Assistant одиночные «0 В»/скачки, но не скрывает настоящие события.

    Показание подозрительно, если резко отличается от предыдущего принятого, меняет критичные
    биты статуса (сеть/батарея/отказ) или противоречит само себе. Подозрительное показание
    перепроверяется (до ``confirm_reads`` быстрых повторных чтений): принимается то значение,
    которое подтвердили два подряд чтения. Настоящая авария (b7 + 0 В) подтверждается сразу
    повторным чтением и публикуется; одиночный выброс - нет. Если ИБП «дребезжит» и подтверждения
    нет ``max_rejects`` раз подряд, показание принимается как есть.
    """

    def __init__(self, confirm_reads: int = 2, max_rejects: int = 3) -> None:
        self.confirm_reads = confirm_reads
        self.max_rejects = max_rejects
        self.last_good: Optional[StatusReading] = None
        self._rejects_in_row = 0
        self.stats: dict = {"glitches_discarded": 0, "changes_confirmed": 0, "unconfirmed": 0, "last_discarded": None}

    def _accept(self, reading: StatusReading) -> StatusReading:
        if is_inconsistent(reading):
            # подтверждено, но физически невозможно: не засорять статистику «23 В» при сети в норме
            _LOGGER.warning("ИБП сообщает вход %.1f В при флаге «сеть в норме» - значение скрыто: %s",
                            reading.ac_in, reading.raw_response)
            reading = replace(reading, ac_in=None)
        self.last_good = reading
        self._rejects_in_row = 0
        return reading

    def _confirm(
        self, first: StatusReading, read_again: Callable[[], Optional[StatusReading]]
    ) -> Optional[StatusReading]:
        prev = first
        for _ in range(self.confirm_reads):
            again = read_again()
            if again is None:
                continue
            if not readings_differ(prev, again):
                return again  # два подряд согласны
            prev = again
        return None

    def accept(
        self, reading: StatusReading, read_again: Callable[[], Optional[StatusReading]]
    ) -> Optional[StatusReading]:
        """Вернуть принятое показание или None, если выброс не подтверждён."""
        suspicious = is_inconsistent(reading) or (
            self.last_good is not None and readings_differ(reading, self.last_good)
        )
        if not suspicious:
            return self._accept(reading)

        confirmed = self._confirm(reading, read_again)
        if confirmed is not None:
            if readings_differ(confirmed, reading):
                self.stats["glitches_discarded"] += 1
                self.stats["last_discarded"] = reading.raw_response
                _LOGGER.info("Отброшен одиночный выброс ИБП: %s (подтверждено: %s)",
                             reading.raw_response, confirmed.raw_response)
            else:
                self.stats["changes_confirmed"] += 1
            return self._accept(confirmed)

        self.stats["unconfirmed"] += 1
        self._rejects_in_row += 1
        _LOGGER.debug("Показание не подтверждено (%d/%d): %s", self._rejects_in_row, self.max_rejects,
                      reading.raw_response)
        if self._rejects_in_row >= self.max_rejects:
            return self._accept(reading)  # ИБП нестабилен - доверяем тому, что он показывает
        return None

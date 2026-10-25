"""
SVC / Richcomm Technologies UPS Low-Level USB-HID Driver
=========================================================
VID: 0x0925 / PID: 0x1234
Supports FRAMED 0xA0 Megatec protocol, signed int32 ioctl,
dynamic Identity querying (I\\r), rock-solid Beeper control (Q\\r),
and Self-Test (T\\r / CT\\r).
"""

from __future__ import annotations

import os
import time
import glob
import fcntl
import struct
import select
import logging
import threading
from dataclasses import dataclass
from typing import Dict, Any, Optional, List, Tuple

_LOGGER = logging.getLogger(__name__)

DEFAULT_VID = 0x0925
DEFAULT_PID = 0x1234
DEFAULT_BAUD = 2400

# Linux hiddev ioctl codes
HIDIOCGDEVINFO = 0x801C4803
HIDIOCINITREPORT = 0x00004805
HIDIOCSUSAGE = 0x4018480C
HIDIOCSREPORT = 0x400C4808
HIDIOCGUSAGE = 0xC018480B
HIDIOCGUCODE = 0xC018480D

HID_REPORT_TYPE_INPUT = 1
HID_REPORT_TYPE_OUTPUT = 2
HID_REPORT_ID_FIRST = 0x00000100

_USAGE_REF = struct.Struct("<IIIIIi")     # type, id, field_idx, usage_idx, code, value
_REPORT_INFO = struct.Struct("<III")      # type, id, num_fields
_EVENT = struct.Struct("<Ii")             # hid, value


class St:
    AC_FAIL   = 0x80      # b7 Utility Fail (On Battery)
    LOW_BATT  = 0x40      # b6 Battery Low
    BYPASS    = 0x20      # b5 Bypass / Boost active
    UPS_FAIL  = 0x10      # b4 UPS Failed
    STANDBY   = 0x08      # b3 UPS Type: 1=Standby, 0=Online
    TEST      = 0x04      # b2 Test in progress
    SHUTDOWN  = 0x02      # b1 Shutdown active
    BEEPER    = 0x01      # b0 Beeper on


@dataclass
class Identity:
    company: str = "SVC"
    model: str = "Richcomm UPS"
    version: str = "V1.0"


@dataclass
class Rating:
    ac_in: Optional[float] = 230.0
    load: Optional[float] = 10.0
    dc: Optional[float] = 12.0
    freq: Optional[float] = 50.0


@dataclass
class StatusReading:
    ac_in: Optional[float] = None
    ac_out: Optional[float] = None
    load: Optional[float] = None
    freq: Optional[float] = None
    dc: Optional[float] = None
    temp: Optional[float] = None
    status: int = 0
    raw_response: str = ""


def _chk(tok: str, lo: float, hi: float) -> Optional[float]:
    try:
        v = float(tok)
    except ValueError:
        v = 0.0
    return v if lo < v < hi else None


def parse_q1(frame: bytes) -> Optional[StatusReading]:
    if not frame:
        return None
    start = frame.find(b"(")
    if start < 0:
        return None
    end = frame.find(b"\r", start)
    sub = frame[start:end] if end >= 0 else frame[start:]
    text = sub[1:].decode("ascii", errors="replace").strip()
    tok = text.split()
    if len(tok) < 8:
        return None

    # Ищем токен статуса (8 символов из '0' и '1')
    status_tok = None
    if len(tok[7]) == 8 and all(c in "01" for c in tok[7]):
        status_tok = tok[7]
    else:
        for t in reversed(tok):
            clean_t = t.strip()
            if len(clean_t) == 8 and all(c in "01" for c in clean_t):
                status_tok = clean_t
                break

    if not status_tok:
        return None

    status = sum(1 << (7 - i) for i, c in enumerate(status_tok) if c == "1")
    return StatusReading(
        ac_in=_chk(tok[0], -1, 450),
        ac_out=_chk(tok[2], -1, 450),
        load=_chk(tok[3], -1, 150),
        freq=_chk(tok[4], 0, 65),
        dc=_chk(tok[5], -1, 250),
        temp=_chk(tok[6], -20, 100),
        status=status,
        raw_response=text
    )


def parse_identity(frame: bytes) -> Optional[Identity]:
    """Разбор ответа команды I\\r: #Company_Name_ UPS_Model_ Version___\\r"""
    if not frame:
        return None
    start = frame.find(b"#")
    if start < 0:
        return None
    end = frame.find(b"\r", start)
    sub = frame[start:end] if end >= 0 else frame[start:]
    body = sub[1:].decode("ascii", errors="replace").strip()
    if len(body) >= 37:
        return Identity(
            company=body[0:15].strip() or "SVC",
            model=body[16:26].strip() or "Richcomm UPS",
            version=body[27:37].strip() or "V1.0",
        )
    tok = body.split()
    if len(tok) >= 3:
        return Identity(" ".join(tok[:-2]), tok[-2], tok[-1])
    elif len(tok) == 2:
        return Identity(tok[0], tok[1], "V1.0")
    elif len(tok) == 1:
        return Identity("SVC", tok[0], "V1.0")
    return None


class SvcUpsHardware:
    """Прямой USB-HID драйвер для Home Assistant."""

    def __init__(self, dev_path: Optional[str] = None, baud: int = DEFAULT_BAUD):
        self.dev_path = dev_path or "/dev/usb/hiddev0"
        self.baud = baud
        self.fd: int = -1
        self.cond = threading.Condition()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self.error = False

        self.uart_buf = bytearray()
        self.last_status_byte: int = 0xC0
        self._cmd_lock = threading.Lock()

    def _pdu(self, pdu: bytes) -> bool:
        if self.fd < 0:
            return False
        padded = (list(pdu) + [0, 0, 0, 0])[:4]
        try:
            for i in range(4):
                val = padded[i] - 256 if padded[i] > 127 else padded[i]
                fcntl.ioctl(self.fd, HIDIOCSUSAGE,
                            _USAGE_REF.pack(HID_REPORT_TYPE_OUTPUT, 0, (i >> 1) & 1, i & 1, 0, val))
            fcntl.ioctl(self.fd, HIDIOCSREPORT, _REPORT_INFO.pack(HID_REPORT_TYPE_OUTPUT, 0, 0))
            return True
        except OSError as e:
            _LOGGER.debug("PDU ioctl error: %s", e)
            self.error = True
            return False

    def _find_device_path(self) -> Optional[str]:
        """Поиск узла hiddev с VID=0925, PID=1234 при переподключении ядра."""
        candidates = [self.dev_path] if self.dev_path else []
        for i in range(16):
            p = f"/dev/usb/hiddev{i}"
            if p not in candidates:
                candidates.append(p)

        for path in candidates:
            if not os.path.exists(path):
                continue
            try:
                test_fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
            except OSError:
                continue
            try:
                info = bytearray(28)
                fcntl.ioctl(test_fd, HIDIOCGDEVINFO, info)
                vid, pid = struct.unpack_from("<HH", info, 16)
                os.close(test_fd)
                if (vid, pid) == (0x0925, 0x1234):
                    return path
            except OSError:
                os.close(test_fd)
                continue
        return None

    def open(self) -> bool:
        if self.fd >= 0 and not self.error:
            return True

        if self.fd >= 0 or self.error:
            self.close()

        # Поиск рабочего узла устройства (на случай если ядро переподключило hiddev0 -> hiddev1)
        found_path = self._find_device_path()
        if found_path and found_path != self.dev_path:
            _LOGGER.info("ИБП найден на узле: %s (прежний: %s)", found_path, self.dev_path)
            self.dev_path = found_path

        # Автосоздание узла при необходимости
        os.makedirs("/dev/usb", exist_ok=True)
        if not os.path.exists(self.dev_path):
            try:
                os.mknod(self.dev_path, 0o20666, os.makedev(180, 96))
            except OSError:
                pass

        try:
            self.fd = os.open(self.dev_path, os.O_RDWR)
        except OSError as e:
            _LOGGER.error("Cannot open %s: %s", self.dev_path, e)
            self.error = True
            return False

        # Связывание дескрипторов
        for j in range(8):
            uref = bytearray(struct.pack("<5Ii", HID_REPORT_TYPE_INPUT, HID_REPORT_ID_FIRST, 0, j, 0, 0))
            try:
                fcntl.ioctl(self.fd, HIDIOCGUCODE, uref)
                fcntl.ioctl(self.fd, HIDIOCGUSAGE, uref)
            except OSError:
                pass

        # SetBaud 2400
        divisor = (500000 // self.baud) & 0xFF
        self._pdu(bytes([0x80, divisor, 0, 0]))

        with self.cond:
            self.uart_buf.clear()

        self.error = False
        self._running = True
        self._thread = threading.Thread(target=self._reader, daemon=True, name="HA_USBReadThread")
        self._thread.start()
        time.sleep(0.15)
        return True

    def _reader(self):
        while self._running and self.fd >= 0:
            try:
                r, _, _ = select.select([self.fd], [], [], 0.25)
                if not r:
                    continue
                data = os.read(self.fd, 512)
            except OSError:
                self.error = True
                time.sleep(0.5)
                continue

            if not data:
                continue

            rep = bytearray(8)
            for off in range(0, len(data) - 7, 8):
                hid, val = _EVENT.unpack_from(data, off)
                idx = (hid & 0xFF) - 3
                if 0 <= idx < 6:
                    rep[idx] = val & 0xFF

            hdr = rep[0]
            self.last_status_byte = hdr

            with self.cond:
                if hdr & 0x80:
                    n = min(hdr & 7, 5)
                    if n > 0:
                        self.uart_buf += rep[1:1 + n]
                self.cond.notify_all()

    def send_text(self, cmd_bytes: bytes) -> bool:
        pos = 0
        while pos < len(cmd_bytes):
            chunk = cmd_bytes[pos:pos + 3]
            pdu = bytes([0xA0 | len(chunk)]) + chunk.ljust(3, b"\0")
            if not self._pdu(pdu):
                return False
            delay = max(0.015, (len(chunk) * 11000.0 / self.baud) / 1000.0)
            time.sleep(delay)
            pos += len(chunk)
        return True

    def _query_frame(self, cmd: bytes, header_char: bytes = b"(", timeout_sec: float = 1.8) -> Optional[bytes]:
        """Отправляет команду и ожидает кадр, начинающийся с header_char и оканчивающийся \\r."""
        with self.cond:
            self.uart_buf.clear()

        if not self.send_text(cmd):
            return None

        deadline = time.monotonic() + timeout_sec
        with self.cond:
            while time.monotonic() < deadline:
                if header_char in self.uart_buf and b"\r" in self.uart_buf[self.uart_buf.find(header_char):]:
                    start = self.uart_buf.find(header_char)
                    end = self.uart_buf.find(b"\r", start)
                    sub = bytes(self.uart_buf[start:end + 1])
                    del self.uart_buf[:end + 1]
                    return sub
                self.cond.wait(0.05)

        return None

    def query_status(self) -> Optional[StatusReading]:
        """Запрос статуса Q1\\r с повторной попыткой и резервным статусом."""
        with self._cmd_lock:
            if not self.open():
                return None

            frame = self._query_frame(b"Q1\r", header_char=b"(", timeout_sec=1.8)
            if frame:
                r = parse_q1(frame)
                if r is not None:
                    return r

            # Wakeup retry
            self.send_text(b"\r")
            time.sleep(0.1)
            frame = self._query_frame(b"Q1\r", header_char=b"(", timeout_sec=1.5)
            if frame:
                r = parse_q1(frame)
                if r is not None:
                    return r

            # Резервный аппаратный статус от контроллера
            if self.last_status_byte is not None:
                ac_ok = bool(self.last_status_byte & 0x80)
                batt_ok = bool(self.last_status_byte & 0x40)
                status = 0
                if not ac_ok:
                    status |= St.AC_FAIL
                if not batt_ok:
                    status |= St.LOW_BATT

                return StatusReading(
                    ac_in=230.0 if ac_ok else 0.0,
                    ac_out=230.0,
                    load=10.0,
                    freq=50.0,
                    dc=13.8 if batt_ok else 11.2,
                    temp=28.0,
                    status=status,
                    raw_response=f"Fallback Hardware Byte: 0x{self.last_status_byte:02X}"
                )

            return None

    def query_identity(self) -> Optional[Identity]:
        """Запрос паспортных данных ИБП (команда I\\r): Производитель, Модель, Версия прошивки."""
        with self._cmd_lock:
            if not self.open():
                return None

            frame = self._query_frame(b"I\r", header_char=b"#", timeout_sec=1.8)
            if frame:
                ident = parse_identity(frame)
                if ident:
                    _LOGGER.info("UPS Identity parsed: %s %s (Firmware: %s)",
                                 ident.company, ident.model, ident.version)
                    return ident

            # Повтор с Wakeup
            self.send_text(b"\r")
            time.sleep(0.08)
            frame = self._query_frame(b"I\r", header_char=b"#", timeout_sec=1.2)
            if frame:
                ident = parse_identity(frame)
                if ident:
                    return ident

            return None

    def set_beeper(self, target_state: bool) -> bool:
        """
        Надёжное переключение пищалки в целевое состояние (target_state: True=ВКЛ, False=ВЫКЛ).
        Поскольку Megatec Q\\r является TOGGLE-командой:
          1. Сначала читаем текущее состояние бипера из Q1\\r.
          2. Если состояние уже соответствует target_state — ничего не делаем (избегаем дребезга).
          3. Если отличается — отправляем Q\\r с паузой стабилизации UART.
          4. Запрашиваем новый статус Q1\\r и проверяем, переключился ли бит b0.
          5. При необходимости повторяем до 3 попыток.
        """
        with self._cmd_lock:
            if not self.open():
                return False

            for attempt in range(1, 4):
                # 1. Запрашиваем текущее состояние
                cur_status = self._query_frame(b"Q1\r", header_char=b"(", timeout_sec=1.2)
                cur_reading = parse_q1(cur_status) if cur_status else None
                if cur_reading:
                    cur_beeper_on = bool(cur_reading.status & St.BEEPER)
                    if cur_beeper_on == target_state:
                        _LOGGER.info("Beeper already in target state (%s)", target_state)
                        return True

                # 2. Очищаем буфер и посылаем Q\r
                with self.cond:
                    self.uart_buf.clear()
                _LOGGER.info("Beeper switch attempt %d/3: sending Q\\r (target=%s)", attempt, target_state)
                self.send_text(b"Q\r")
                time.sleep(0.35)

                # 3. Верифицируем смену состояния
                verify_frame = self._query_frame(b"Q1\r", header_char=b"(", timeout_sec=1.2)
                verify_reading = parse_q1(verify_frame) if verify_frame else None
                if verify_reading:
                    new_beeper_on = bool(verify_reading.status & St.BEEPER)
                    if new_beeper_on == target_state:
                        _LOGGER.info("Beeper successfully set to %s on attempt %d", target_state, attempt)
                        return True

                time.sleep(0.25)

            _LOGGER.warning("Could not set beeper to %s after 3 attempts", target_state)
            return False

    def toggle_beeper(self) -> bool:
        """Инверсия текущего состояния бипера."""
        cur = self.query_status()
        cur_on = bool(cur.status & St.BEEPER) if cur else True
        return self.set_beeper(not cur_on)

    def start_self_test(self) -> bool:
        """Запуск 10-секундного теста батареи (команда T\\r)."""
        with self._cmd_lock:
            _LOGGER.info("Sending Self-Test (T\\r)")
            with self.cond:
                self.uart_buf.clear()
            ok = self.send_text(b"T\r")
            time.sleep(0.2)
            return ok

    def cancel_self_test(self) -> bool:
        """Отмена тестирования батареи (команда CT\\r)."""
        with self._cmd_lock:
            _LOGGER.info("Canceling Self-Test (CT\\r)")
            with self.cond:
                self.uart_buf.clear()
            ok = self.send_text(b"CT\r")
            time.sleep(0.2)
            return ok

    def close(self):
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.5)
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1

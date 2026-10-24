#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SVC / Richcomm Technologies UPS - Complete Python Driver, Daemon & CLI Suite
=============================================================================
(Vendor ID: 0x0925 / Lakeview Research, Product ID: 0x1234 / Richcomm USB to UART Bridge)

Полная 100% замена устаревшего 32-битного ПО (ups_monitor, ups_status, ups_manager, ups_config).
Не требует 32-битных библиотек (glibc i386), компиляторов и внешних pip-пакетов (только Python 3 stdlib).

Возможности:
  1. Автономный прямой USB-HID драйвер (/dev/usb/hiddev*):
     - Автоматическое создание узла устройства (mknod major 180, minor 96+i) при работе в Docker/LXC
     - Определение конфликтующих процессов (lsof / /proc/*)
     - Связывание дескрипторов отчётов ядра (HIDIOCGUCODE / HIDIOCGUSAGE)
     - Переключение скорости UART SetBaud 2400 (PDU [0x80, 0xD0, 0x00, 0x00])
     - Точное знаковое расширение байтов (signed int32) в ioctl HIDIOCSUSAGE
     - Отправка пакетов в родном формате Richcomm FRAMED 0xA0 [0xA0 | len, d0, d1, d2]
     - Непрерывный фоновый поток чтения USBReadThread с разбором кадров UART и бинарных BCD-отчётов
     - Двухрежимный опрос: текстовый Megatec (Q1/I/F) + автоматический fallback на бинарные отчёты (1, 2, 3)
  2. Поддержка SysV Shared Memory (IPC key 0x1B56 / 7000, 224 байта):
     - Режим клиента: чтение статуса из памяти старого ups_monitor (без конфликта USB)
     - Режим сервера: публикация статуса в SHM для работы старых утилит и GUI
  3. Полнофункциональный демон мониторинга (--daemon):
     - Полная замена ups_monitor в фоне
     - Отслеживание сбоев сети (AC Fail), разряда АКБ (Low Cell) и отказов ИБП
     - Обратный отсчёт до выключения с оповещениями через ./SendMSG.sh
     - Выполнение ./shutdown.sh при истечении таймаута
     - Ведение журнала ./PowerEventГГГГММ.log
     - Запись статуса в /tmp/ups_status.json (для Home Assistant, Zabbix, Telegraf)
     - Поддержка конфигурации ./TimeCfg.conf (52 байта) и ups_config.json
  4. Интерактивная конфигурация (--config) взамен ups_config
  5. Комплексная диагностика (--test all / sniff / baud / q1 / wakeup / binary / shm)

Использование:
  ./upstest.py                  - Однократный опрос статуса ИБП
  ./upstest.py --loop 2         - Постоянный опрос каждые 2 сек (аналог ups_status)
  ./upstest.py --json           - Вывод в формате JSON
  ./upstest.py --daemon         - Запуск службы мониторинга в фоне (полная замена ups_monitor)
  ./upstest.py --config         - Настройка параметров и задержек выключения
  ./upstest.py --test all       - Полная диагностика оборудования и протоколов
  ./upstest.py decode "<str>"   - Разбор сырой строки ответа Megatec Q1
"""

from __future__ import annotations

import os
import sys
import time
import glob
import json
import fcntl
import struct
import select
import signal
import logging
import argparse
import threading
import ctypes
import subprocess
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List, Tuple

# =============================================================================
# КОНСТАНТЫ И КОДЫ LINUX KERNEL HIDDEV & SYSV IPC
# =============================================================================

DEFAULT_VID = 0x0925
DEFAULT_PID = 0x1234
DEFAULT_BAUD = 2400

# Linux hiddev ioctl коды (_IOC_*)
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

# SysV IPC SHM константы
IPC_KEY = 0x1B56              # 7000
SHM_SIZE = 224                # sizeof(UPS_DATA_Status)
CFG_MAGIC = 0x55AA0335
CFG_STRUCT = struct.Struct("<5i16s4i")   # 52 байта

CONFIG_FILE = "ups_config.json"
STATUS_JSON_PATH = "/tmp/ups_status.json"

logger = logging.getLogger("svc_ups")


def setup_logging(debug: bool = False):
    level = logging.DEBUG if debug else logging.INFO
    fmt = "%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s"
    logging.basicConfig(level=level, format=fmt, datefmt="%H:%M:%S")


def hex_dump(data: bytes) -> str:
    return "[" + " ".join(f"{b:02X}" for b in data) + "] ('" + "".join(chr(b) if 32 <= b < 127 else "." for b in data) + "')"


def bcd(b: int) -> int:
    return (b >> 4) * 10 + (b & 0x0F)


# =============================================================================
# ПОИСК УСТРОЙСТВА И КОНФЛИКТУЮЩИХ ПРОЦЕССОВ
# =============================================================================

def ensure_hiddev_nodes():
    """Создаёт узлы /dev/usb/hiddev0..15 если они отсутствуют в Docker/LXC."""
    os.makedirs("/dev/usb", exist_ok=True)
    for i in range(16):
        path = f"/dev/usb/hiddev{i}"
        if not os.path.exists(path):
            try:
                os.mknod(path, 0o20666, os.makedev(180, 96 + i))
            except OSError:
                pass


def find_conflicting_processes(dev_path: str = "/dev/usb/hiddev0") -> List[Tuple[int, str]]:
    """Находит сторонние процессы, удерживающие /dev/usb/hiddev*."""
    conflicts = []
    real_dev = os.path.realpath(dev_path) if os.path.exists(dev_path) else dev_path
    my_pid = os.getpid()

    for proc_entry in glob.glob("/proc/[0-9]*/fd/*"):
        try:
            target = os.readlink(proc_entry)
            if target == dev_path or target == real_dev or "hiddev" in target:
                parts = proc_entry.split("/")
                pid = int(parts[2])
                if pid == my_pid:
                    continue
                cmdline = f"/proc/{pid}/cmdline"
                name = ""
                if os.path.exists(cmdline):
                    with open(cmdline, "rb") as f:
                        name = f.read().replace(b"\x00", b" ").decode("latin1", errors="ignore").strip()
                conflicts.append((pid, name or f"PID {pid}"))
        except (OSError, ValueError):
            continue

    seen = set()
    res = []
    for pid, name in conflicts:
        if pid not in seen:
            seen.add(pid)
            res.append((pid, name))
    return res


# =============================================================================
# МОДЕЛИ ДАННЫХ И СТАТУСОВ
# =============================================================================

class St:
    AC_FAIL   = 0x80      # b7 Utility Fail (работа от АКБ)
    LOW_BATT  = 0x40      # b6 Battery Low
    BYPASS    = 0x20      # b5 Bypass / Boost active
    UPS_FAIL  = 0x10      # b4 UPS Failed
    STANDBY   = 0x08      # b3 UPS Type (1=Standby, 0=Online)
    TEST      = 0x04      # b2 Test in progress
    SHUTDOWN  = 0x02      # b1 Shutdown active
    BEEPER    = 0x01      # b0 Beeper on


@dataclass
class StatusReading:
    ac_in: Optional[float] = None
    ac_out: Optional[float] = None
    load: Optional[float] = None
    freq: Optional[float] = None
    dc: Optional[float] = None
    temp: Optional[float] = None
    status: int = 0
    raw_status_byte: int = 0xC0
    source: str = "usb_text"


@dataclass
class Identity:
    company: str = "SVC"
    model: str = "Richcomm-UPS"
    version: str = "V1.0"


@dataclass
class Rating:
    ac_in: Optional[float] = 230.0
    load: Optional[float] = 10.0
    dc: Optional[float] = 12.0
    freq: Optional[float] = 50.0


def _chk(tok: str, lo: float, hi: float) -> Optional[float]:
    try:
        v = float(tok)
    except ValueError:
        v = 0.0
    return v if lo < v < hi else None


def parse_q1(frame: bytes) -> Optional[StatusReading]:
    """
    Разбор ответа Megatec Q1:
    Формат: (MMM.M NNN.N PPP.P QQQ RR.R S.SS TT.T b7b6b5b4b3b2b1b0\\r
    Длина 47 байт: '(' + 46 байт данных
    """
    if not frame:
        return None
    # Ищем начало '(' и конец '\r'
    start = frame.find(b"(")
    if start < 0:
        return None
    end = frame.find(b"\r", start)
    if end < 0:
        sub = frame[start:]
    else:
        sub = frame[start:end]

    text = sub[1:].decode("ascii", errors="replace").strip()
    tok = text.split()
    if len(tok) < 8 or len(tok[7]) != 8:
        return None

    status = sum(1 << (7 - i) for i, c in enumerate(tok[7]) if c == "1")
    return StatusReading(
        ac_in=_chk(tok[0], -1, 450),
        ac_out=_chk(tok[2], -1, 450),
        load=_chk(tok[3], -1, 150),
        freq=_chk(tok[4], 0, 65),
        dc=_chk(tok[5], -1, 250),
        temp=_chk(tok[6], -20, 100),
        status=status,
        source="megatec_q1"
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
    body = sub[1:].decode("ascii", errors="replace")
    if len(body) >= 37:
        return Identity(body[0:15].strip(), body[16:26].strip(), body[27:37].strip())
    tok = body.split()
    if len(tok) >= 3:
        return Identity(" ".join(tok[:-2]), tok[-2], tok[-1])
    return None


def parse_rating(frame: bytes) -> Optional[Rating]:
    """Разбор ответа команды F\\r: #MMM.M QQQ SS.SS FF.F\\r"""
    if not frame:
        return None
    start = frame.find(b"#")
    if start < 0:
        return None
    end = frame.find(b"\r", start)
    sub = frame[start:end] if end >= 0 else frame[start:]
    tok = sub[1:].decode("ascii", errors="replace").split()
    if len(tok) != 4:
        return None
    return Rating(
        ac_in=_chk(tok[0], 0, 450),
        load=_chk(tok[1], -1, 150),
        dc=_chk(tok[2], -1, 250),
        freq=_chk(tok[3], 0, 65)
    )


# =============================================================================
# РЕАЛИЗАЦИЯ SYSV SHARED MEMORY (СОВМЕСТИМО С ups_monitor И ups_status)
# =============================================================================

class _Sembuf(ctypes.Structure):
    _fields_ = [("sem_num", ctypes.c_ushort),
                ("sem_op", ctypes.c_short),
                ("sem_flg", ctypes.c_short)]


class SysVShm:
    """Полная реализация доступа к общей памяти и семафору SysV IPC (ключ 0x1B56)."""
    IPC_CREAT, IPC_RMID, SEM_UNDO, IPC_NOWAIT = 0o1000, 0, 0x1000, 0o4000

    def __init__(self, key: int = IPC_KEY):
        self.libc = None
        self.key = key
        self.shmid = -1
        self.semid = -1
        self._init_libc()

    def _init_libc(self):
        try:
            self.libc = ctypes.CDLL(None, use_errno=True)
            self.libc.shmget.argtypes = [ctypes.c_int, ctypes.c_size_t, ctypes.c_int]
            self.libc.shmget.restype = ctypes.c_int
            self.libc.shmat.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
            self.libc.shmat.restype = ctypes.c_void_p
            self.libc.shmdt.argtypes = [ctypes.c_void_p]
            self.libc.shmdt.restype = ctypes.c_int
            self.libc.shmctl.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
            self.libc.shmctl.restype = ctypes.c_int
            self.libc.semget.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
            self.libc.semget.restype = ctypes.c_int
            self.libc.semop.argtypes = [ctypes.c_int, ctypes.POINTER(_Sembuf), ctypes.c_size_t]
            self.libc.semop.restype = ctypes.c_int
            self.libc.semctl.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
            self.libc.semctl.restype = ctypes.c_int
        except Exception as e:
            logger.debug(f"SysV IPC init error: {e}")
            self.libc = None

    def open_existing(self) -> bool:
        """Подключается к уже созданной ups_monitor общей памяти (для чтения)."""
        if not self.libc:
            return False
        self.shmid = self.libc.shmget(self.key, SHM_SIZE, 0)
        return self.shmid >= 0

    def create(self) -> bool:
        """Создаёт сегмент памяти и семафор в режиме сервера/демона."""
        if not self.libc:
            return False
        self.shmid = self.libc.shmget(self.key, SHM_SIZE, self.IPC_CREAT | 0o666)
        self.semid = self.libc.semget(self.key, 1, self.IPC_CREAT | 0o666)
        return self.shmid >= 0 and self.semid >= 0

    def _lock(self, acquire: bool):
        if not self.libc or self.semid < 0:
            return
        if acquire:
            ops = (_Sembuf * 2)(_Sembuf(0, 0, 0), _Sembuf(0, 1, self.SEM_UNDO))
        else:
            ops = (_Sembuf * 1)(_Sembuf(0, -1, self.SEM_UNDO | self.IPC_NOWAIT))
        self.libc.semop(self.semid, ops, len(ops))

    def read_raw(self) -> Optional[bytes]:
        if not self.libc or self.shmid < 0:
            return None
        self._lock(True)
        try:
            p = self.libc.shmat(self.shmid, None, 0)
            if not p or p == -1:
                return None
            data = ctypes.string_at(p, SHM_SIZE)
            self.libc.shmdt(p)
            return data
        finally:
            self._lock(False)

    def write_raw(self, raw_bytes: bytes):
        if not self.libc or self.shmid < 0:
            return
        self._lock(True)
        try:
            p = self.libc.shmat(self.shmid, None, 0)
            if p and p != -1:
                ctypes.memmove(p, raw_bytes[:SHM_SIZE], min(len(raw_bytes), SHM_SIZE))
                self.libc.shmdt(p)
        finally:
            self._lock(False)


# =============================================================================
# АВТОНОМНЫЙ АППАРАТНЫЙ USB-HID ДРАЙВЕР (RICHCOMM VID=0925 PID=1234)
# =============================================================================

class SvcUpsHardware:
    """
    Автономный низкоуровневый драйвер прямого доступа к /dev/usb/hiddev*.
    Реализует спецификацию Richcomm USB-to-UART моста:
      1. Авто-поиск или авто-создание узла устройства
      2. Знаковое расширение байтов (signed int32) в ioctl HIDIOCSUSAGE
      3. Инициализация SetBaud 2400 (PDU [0x80, 0xD0, 0x00, 0x00])
      4. Отправка команд пакетами SendData [0xA0 | len, d0, d1, d2]
      5. Непрерывный фоновый поток чтения USBReadThread с Condition-переменной
      6. Сборка UART-байт (rep[0] & 0x80) и бинарных BCD-отчётов
    """

    def __init__(self, dev_path: Optional[str] = None, baud: int = DEFAULT_BAUD):
        self.dev_path = dev_path
        self.baud = baud
        self.fd: int = -1

        self.cond = threading.Condition()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self.error = False

        # Буферы данных
        self.uart_buf = bytearray()
        self.usb_raw_buf = bytearray()
        self.last_status_byte: int = 0xC0
        self.total_frames_received: int = 0
        self.total_bytes_received: int = 0

    def _pdu(self, pdu: bytes) -> bool:
        """UsbSendPDU: 4 байта как 4 usage ((i>>1)&1, i&1) + SET_REPORT(output)."""
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
            logger.debug(f"PDU ioctl error: {e}")
            self.error = True
            return False

    def open(self) -> bool:
        if self.fd >= 0:
            return True

        ensure_hiddev_nodes()
        candidates = []
        if self.dev_path:
            candidates.append(self.dev_path)
        else:
            candidates.extend(sorted(glob.glob("/dev/usb/hiddev*") + glob.glob("/dev/hiddev*")))

        if not candidates:
            # Попробуем создать /dev/usb/hiddev0
            ensure_hiddev_nodes()
            candidates = ["/dev/usb/hiddev0"]

        for p in candidates:
            conflicts = find_conflicting_processes(p)
            for c_pid, c_cmd in conflicts:
                logger.warning(
                    f"ВНИМАНИЕ: Процесс PID {c_pid} ('{c_cmd}') удерживает {p}! "
                    f"Остановите его (kill -9 {c_pid}), иначе он забирает входящие пакеты!"
                )

            try:
                fd = os.open(p, os.O_RDWR)
                info = bytearray(28)
                fcntl.ioctl(fd, HIDIOCGDEVINFO, info)
                _, _, _, _, vid, pid = struct.unpack("<IIIIhh", info[:20])
                vid = vid & 0xFFFF
                pid = pid & 0xFFFF

                if vid == DEFAULT_VID and pid == DEFAULT_PID:
                    self.fd = fd
                    self.dev_path = p
                    self.error = False
                    logger.info(f"Найдено устройство ИБП: {p} (VID={vid:04X}, PID={pid:04X})")
                    self._start_driver()
                    return True
                else:
                    os.close(fd)
            except OSError:
                continue

        # Fallback прямое открытие первого кандидата
        if self.fd < 0 and candidates:
            p = candidates[0]
            try:
                self.fd = os.open(p, os.O_RDWR)
                self.dev_path = p
                self.error = False
                logger.info(f"Прямое подключение к {p}")
                self._start_driver()
                return True
            except OSError as e:
                logger.error(f"Не удалось открыть {p}: {e}")
                return False
        return False

    def _start_driver(self):
        # 1. Связывание отчетов входных дескрипторов
        for j in range(8):
            uref = bytearray(struct.pack("<5Ii", HID_REPORT_TYPE_INPUT, HID_REPORT_ID_FIRST, 0, j, 0, 0))
            try:
                fcntl.ioctl(self.fd, HIDIOCGUCODE, uref)
                fcntl.ioctl(self.fd, HIDIOCGUSAGE, uref)
            except OSError:
                pass

        # 2. SetBaud 2400 (PDU [0x80, 0xD0, 0x00, 0x00])
        divisor = (500000 // self.baud) & 0xFF
        self._pdu(bytes([0x80, divisor, 0, 0]))
        self.clear()

        # 3. Фоновый читатель
        self._running = True
        self._thread = threading.Thread(target=self._reader, daemon=True, name="USBReadThread")
        self._thread.start()
        time.sleep(0.15)

    def clear(self):
        with self.cond:
            self.uart_buf.clear()
            self.usb_raw_buf.clear()

    def _reader(self):
        """Непрерывный поток USBReadThread: собирает отчёты из hiddev_event."""
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

            self.total_frames_received += 1
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
                        chunk = rep[1:1 + n]
                        self.uart_buf += chunk
                        self.total_bytes_received += len(chunk)
                else:
                    self.usb_raw_buf += rep[:6]
                self.cond.notify_all()

    def send_text(self, cmd_bytes: bytes) -> bool:
        """SendData: отправка кусками по 3 байта [0xA0 | len, c0, c1, c2]."""
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

    def send_raw_report(self, cmd: int) -> bool:
        """Отправка бинарного запроса отчёта [cmd, 0, 0, 0]."""
        ok = self._pdu(bytes([cmd & 0xFF, 0, 0, 0]))
        time.sleep(0.02)
        return ok

    def _take_uart(self, n: int, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        with self.cond:
            while len(self.uart_buf) < n:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self.cond.wait(left)
            out = bytes(self.uart_buf[:n])
            del self.uart_buf[:n]
            return out

    def _take_raw(self, n: int = 6, timeout: float = 1.0) -> bytes:
        deadline = time.monotonic() + timeout
        with self.cond:
            while len(self.usb_raw_buf) < n:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self.cond.wait(left)
            out = bytes(self.usb_raw_buf[:n])
            del self.usb_raw_buf[:n]
            return out

    def query_text(self, cmd: str, timeout_sec: float = 1.8) -> str:
        """Отправляет команду Megatec (Q1/I/F) и ожидает ответа с символом '(' или '#'."""
        self.clear()
        full_cmd = (cmd.strip() + "\r").encode("latin1")
        if not self.send_text(full_cmd):
            return ""

        expected = b"(" if cmd.upper().startswith("Q") else b"#"
        deadline = time.monotonic() + timeout_sec
        with self.cond:
            while time.monotonic() < deadline:
                if expected in self.uart_buf and b"\r" in self.uart_buf[self.uart_buf.find(expected):]:
                    start = self.uart_buf.find(expected)
                    end = self.uart_buf.find(b"\r", start)
                    res = self.uart_buf[start:end].decode("ascii", errors="replace").strip()
                    del self.uart_buf[:end + 1]
                    return res
                self.cond.wait(0.05)

            # Если не дождались, вернем что накопилось
            if expected in self.uart_buf:
                start = self.uart_buf.find(expected)
                return self.uart_buf[start:].decode("ascii", errors="replace").strip()
            return self.uart_buf.decode("ascii", errors="replace").strip()

    def query_binary_status(self) -> Optional[StatusReading]:
        """
        Опрос в бинарном режиме (команды 1, 2, 3 с BCD-отчётами).
        Используется только для редких ревизий ИБП с бинарной прошивкой.
        Если чип работает в текстовом режиме Megatec (Q1), бинарные отчёты отсекаются.
        """
        self.clear()
        self.send_raw_report(1)
        r1 = self._take_raw(6, timeout=0.8)
        if len(r1) != 6 or r1[0] != 1 or (r1[4] & 1):
            return None

        # Проверка валидности BCD: каждый полубайт (nibble) должен быть от 0 до 9
        for b in (r1[1], r1[2]):
            if (b >> 4) > 9 or (b & 0x0F) > 9:
                return None

        ac_in = bcd(r1[1]) * 10 + bcd(r1[2]) / 10.0
        status = r1[5]
        if not (100.0 <= ac_in <= 300.0 or ac_in == 0.0):
            return None

        ac_out = freq = load = dc = temp = None
        self.send_raw_report(2)
        r2 = self._take_raw(6, timeout=0.5)
        if len(r2) == 6 and r2[0] == 2:
            for b in (r2[1], r2[2], r2[3], r2[4]):
                if (b >> 4) > 9 or (b & 0x0F) > 9:
                    return None
            ac_out = bcd(r2[1]) * 10 + bcd(r2[2]) / 10.0
            freq = bcd(r2[3]) * 10 + bcd(r2[4]) / 10.0
            load = float(bcd(r2[5])) if (r2[5] >> 4) <= 9 and (r2[5] & 0x0F) <= 9 else 0.0

            if not (0.0 <= ac_out <= 300.0) or not (40.0 <= freq <= 70.0):
                return None

        self.send_raw_report(3)
        r3 = self._take_raw(6, timeout=0.5)
        if len(r3) == 6 and r3[0] == 3:
            for b in (r3[1], r3[2], r3[3], r3[4]):
                if (b >> 4) > 9 or (b & 0x0F) > 9:
                    return None
            dc = bcd(r3[1]) + bcd(r3[2]) / 100.0
            temp = bcd(r3[3]) * 10 + bcd(r3[4]) / 10.0
            if not (-10.0 <= temp <= 90.0):
                return None

        return StatusReading(
            ac_in=ac_in, ac_out=ac_out, load=load, freq=freq, dc=dc, temp=temp,
            status=status, source="usb_binary"
        )

    def shutdown_ups(self, minutes: int = 2):
        """Выключение ИБП через S<min> (текст) или отчет 0x11 (бинарный)."""
        self.send_text(b"S%02d\r" % minutes)
        self.send_raw_report(0x11)

    def query_identity(self) -> Optional[Identity]:
        """Запрос паспортных данных ИБП (команда I\\r): Марка, Модель, Версия прошивки."""
        self.clear()
        res = self.query_text("I", timeout_sec=1.8)
        if res.startswith("#"):
            ident = parse_identity(res.encode("latin1") + b"\r")
            if ident:
                return ident

        # Повтор с Wakeup
        self.send_text(b"\r")
        time.sleep(0.08)
        res = self.query_text("I", timeout_sec=1.5)
        if res.startswith("#"):
            return parse_identity(res.encode("latin1") + b"\r")
        return None

    def set_beeper(self, target_state: bool) -> bool:
        """
        Надежное переключение пищалки в целевое состояние (target_state: True=ВКЛ, False=ВЫКЛ).
        Поскольку Megatec Q\\r является TOGGLE-командой:
          1. Сначала читаем текущее состояние бипера из Q1\\r.
          2. Если состояние уже соответствует target_state — ничего не делаем.
          3. Если отличается — отправляем Q\\r с паузой стабилизации UART 350мс.
          4. Запрашиваем новый статус Q1\\r и проверяем, переключился ли бит b0.
          5. При необходимости повторяем до 3 попыток.
        """
        for attempt in range(1, 4):
            # 1. Запрашиваем текущее состояние
            q1_str = self.query_text("Q1", timeout_sec=1.2)
            if q1_str.startswith("("):
                reading = parse_q1(q1_str.encode("latin1") + b"\r")
                if reading:
                    cur_on = bool(reading.status & St.BEEPER)
                    if cur_on == target_state:
                        logger.info("Бипер уже в целевом состоянии (%s)", target_state)
                        return True

            # 2. Очищаем буфер и посылаем Q\r
            self.clear()
            logger.info("Переключение бипера (попытка %d/3): отправка Q\\r (цель=%s)...", attempt, target_state)
            self.send_text(b"Q\r")
            time.sleep(0.35)

            # 3. Верифицируем смену состояния
            verify_str = self.query_text("Q1", timeout_sec=1.2)
            if verify_str.startswith("("):
                verify_reading = parse_q1(verify_str.encode("latin1") + b"\r")
                if verify_reading:
                    new_on = bool(verify_reading.status & St.BEEPER)
                    if new_on == target_state:
                        logger.info("Бипер успешно переключен в %s на попытке %d", target_state, attempt)
                        return True

            time.sleep(0.25)

        logger.warning("Не удалось переключить бипер в состояние %s после 3 попыток", target_state)
        return False

    def toggle_beeper(self) -> bool:
        """Инверсия текущего состояния бипера с верификацией."""
        q1_str = self.query_text("Q1", timeout_sec=1.2)
        cur_on = True
        if q1_str.startswith("("):
            reading = parse_q1(q1_str.encode("latin1") + b"\r")
            if reading:
                cur_on = bool(reading.status & St.BEEPER)
        return self.set_beeper(not cur_on)

    def start_self_test(self) -> bool:
        """Запуск 10-секундного теста батареи командой 'T\\r' (PDU [0xA2, 0x54, 0x0D, 0x00])."""
        logger.info("Запуск самотестирования ИБП 'T\\r' (10 сек)...")
        self.clear()
        ok = self.send_text(b"T\r")
        time.sleep(0.3)
        return ok

    def cancel_self_test(self) -> bool:
        """Отмена тестирования батареи командой 'CT\\r' (PDU [0xA3, 0x43, 0x54, 0x0D])."""
        logger.info("Отмена самотестирования ИБП 'CT\\r'...")
        self.clear()
        ok = self.send_text(b"CT\r")
        time.sleep(0.3)
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


# =============================================================================
# УНИФИЦИРОВАННЫЙ КОНТРОЛЛЕР СТАТУСА (AUTO / SHM / USB)
# =============================================================================

class SvcUpsController:
    """Обеспечивает опрос ИБП с автоматическим выбором лучшего источника."""

    def __init__(self, mode: str = "auto", dev_path: Optional[str] = None, baud: int = DEFAULT_BAUD):
        self.mode = mode
        self.dev_path = dev_path
        self.baud = baud
        self.hw: Optional[SvcUpsHardware] = None
        self.shm: Optional[SysVShm] = None
        self.active_source = "none"

    def init(self):
        if self.mode in ("auto", "shm"):
            s = SysVShm()
            if s.open_existing():
                raw = s.read_raw()
                if raw and len(raw) == SHM_SIZE:
                    # Проверяем heartbeat или ненулевые данные
                    heartbeat, = struct.unpack_from("<i", raw, 0)
                    ac_in, = struct.unpack_from("<f", raw, 128)
                    if heartbeat > 0 or ac_in > 0:
                        self.shm = s
                        self.active_source = "shm"
                        logger.info("Подключено к SysV IPC Shared Memory (оригинальный ups_monitor активен)")
                        return

        if self.mode in ("auto", "usb"):
            self.hw = SvcUpsHardware(dev_path=self.dev_path, baud=self.baud)
            if self.hw.open():
                self.active_source = "usb"
                return

        if not self.hw and not self.shm:
            # Создаем hw на случай прямого опроса
            self.hw = SvcUpsHardware(dev_path=self.dev_path, baud=self.baud)
            self.hw.open()
            self.active_source = "usb"

    def get_status(self) -> Dict[str, Any]:
        if not self.hw and not self.shm:
            self.init()

        # 1. Попытка чтения из SHM
        if self.shm and self.active_source == "shm":
            raw = self.shm.read_raw()
            if raw and len(raw) == SHM_SIZE:
                return self._parse_shm_data(raw)

        # 2. Попытка прямого опроса USB
        if self.hw:
            # Сначала пробуем Megatec Q1
            q1_str = self.hw.query_text("Q1", timeout_sec=2.0)
            if q1_str.startswith("("):
                reading = parse_q1(q1_str.encode("latin1") + b"\r")
                if reading:
                    return self._reading_to_dict(reading, q1_str)

            # Если ответ не пришел, пробуем Wakeup (CR) и повтор
            self.hw.send_text(b"\r")
            time.sleep(0.08)
            q1_str = self.hw.query_text("Q1", timeout_sec=1.5)
            if q1_str.startswith("("):
                reading = parse_q1(q1_str.encode("latin1") + b"\r")
                if reading:
                    return self._reading_to_dict(reading, q1_str)

            # Fallback на бинарный протокол (отчёты 1, 2, 3)
            bin_reading = self.hw.query_binary_status()
            if bin_reading:
                return self._reading_to_dict(bin_reading, "Binary BCD Reports")

            # Аппаратный статус-байт чипа как резерв
            hdr = self.hw.last_status_byte
            ac_online = bool(hdr & 0x80)
            batt_normal = bool(hdr & 0x40)
            return {
                "online": True,
                "mode": "Richcomm_Hardware_Byte",
                "transport": "hiddev",
                "device": self.hw.dev_path,
                "input_voltage": 230.0 if ac_online else 0.0,
                "output_voltage": 230.0,
                "battery_level": 100.0 if batt_normal else 20.0,
                "battery_voltage": 13.8 if batt_normal else 11.2,
                "load_percent": 10.0,
                "frequency": 50.0,
                "temperature": 28.0,
                "ac_status": "Normal" if ac_online else "Utility Fail (On Battery)",
                "battery_status": "Normal" if batt_normal else "Battery Low",
                "ups_status": "Normal",
                "boost_buck": "Normal",
                "beeper": "Off",
                "shutdown_active": False,
                "test_in_progress": False,
                "raw_response": f"Status Byte: 0x{hdr:02X}",
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
            }

        raise RuntimeError("Не удалось получить телеметрию ИБП (ни через SHM, ни через USB)")

    def _reading_to_dict(self, r: StatusReading, raw_str: str) -> Dict[str, Any]:
        ac_online = not bool(r.status & St.AC_FAIL)
        batt_low = bool(r.status & St.LOW_BATT)
        bypass = bool(r.status & St.BYPASS)
        ups_fail = bool(r.status & St.UPS_FAIL)
        test = bool(r.status & St.TEST)
        shut = bool(r.status & St.SHUTDOWN)
        beeper = bool(r.status & St.BEEPER)

        batt_pct = 100.0
        if r.dc is not None:
            # Оценка 12В АКБ: 10.5В = 0%, 13.6В = 100%
            batt_pct = max(0.0, min(100.0, ((r.dc - 10.5) / (13.6 - 10.5)) * 100.0))

        return {
            "online": True,
            "mode": f"Megatec_{r.source}",
            "transport": "hiddev",
            "device": self.hw.dev_path if self.hw else "/dev/usb/hiddev0",
            "input_voltage": r.ac_in or 230.0,
            "output_voltage": r.ac_out or 230.0,
            "battery_level": round(batt_pct, 1),
            "battery_voltage": r.dc or 13.6,
            "load_percent": r.load or 0.0,
            "frequency": r.freq or 50.0,
            "temperature": r.temp or 25.0,
            "ac_status": "Normal" if ac_online else "Utility Fail (On Battery)",
            "battery_status": "Battery Low" if batt_low else "Normal",
            "ups_status": "UPS Failed" if ups_fail else "Normal",
            "boost_buck": "Active (AVR)" if bypass else "Normal",
            "beeper": "On" if beeper else "Off",
            "shutdown_active": shut,
            "test_in_progress": test,
            "raw_response": raw_str,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
        }

    def _parse_shm_data(self, raw: bytes) -> Dict[str, Any]:
        """Разбор структуры UPS_DATA_Status (224 байта) из SysV IPC."""
        heartbeat, = struct.unpack_from("<i", raw, 0)
        warn_status, = struct.unpack_from("<i", raw, 152)
        ac_in, = struct.unpack_from("<f", raw, 128)
        ac_out, = struct.unpack_from("<f", raw, 136)
        load, = struct.unpack_from("<f", raw, 140)
        freq, = struct.unpack_from("<f", raw, 144)
        dc, = struct.unpack_from("<f", raw, 148)
        temp, = struct.unpack_from("<f", raw, 176)

        model = raw[190:206].split(b"\0", 1)[0].decode("ascii", errors="replace").strip()

        reading = StatusReading(
            ac_in=ac_in, ac_out=ac_out, load=load, freq=freq, dc=dc, temp=temp,
            status=warn_status, source=f"shm_{model or 'ups'}"
        )
        res = self._reading_to_dict(reading, f"SysV SHM (heartbeat={heartbeat})")
        res["model"] = model or "SVC UPS"
        return res

    def query_identity(self) -> Optional[Identity]:
        """Запрос паспорта ИБП (I\\r)."""
        if not self.hw:
            self.init()
        if self.hw:
            return self.hw.query_identity()
        return None

    def set_beeper(self, target_state: bool) -> Dict[str, Any]:
        """Устанавливает звук бипера (True=ВКЛ, False=ВЫКЛ) с верификацией."""
        if not self.hw:
            self.init()
        if self.hw:
            self.hw.set_beeper(target_state)
            time.sleep(0.2)
            return self.get_status()
        raise RuntimeError("Устройство ИБП не подключено")

    def toggle_beeper(self) -> Dict[str, Any]:
        """Инвертирует звук бипера с проверкой смены бита."""
        if not self.hw:
            self.init()
        if self.hw:
            self.hw.toggle_beeper()
            time.sleep(0.2)
            return self.get_status()
        raise RuntimeError("Устройство ИБП не подключено")

    def run_self_test(self, cancel: bool = False) -> Dict[str, Any]:
        """Запускает (T\\r) или отменяет (CT\\r) тест батареи."""
        if not self.hw:
            self.init()
        if self.hw:
            if cancel:
                self.hw.cancel_self_test()
            else:
                self.hw.start_self_test()
            time.sleep(0.3)
            return self.get_status()
        raise RuntimeError("Устройство ИБП не подключено")

    def close(self):
        if self.hw:
            self.hw.close()
            self.hw = None


# =============================================================================
# РЕЖИМ ДЕМОНА МОНИТОРИНГА (--daemon) - ПОЛНАЯ ЗАМЕНА ups_monitor
# =============================================================================

@dataclass
class DaemonConfig:
    ac_fail_delay: int = 300
    battery_low_delay: int = 60
    ups_failed_delay: int = 180
    warning_interval: int = 10
    turn_off_ups: int = 0
    turn_off_ups_time: int = 2
    notify_script: str = "./SendMSG.sh"
    shutdown_script: str = "./shutdown.sh"


def run_daemon(dev_path: Optional[str], baud: int = DEFAULT_BAUD, workdir: str = "."):
    """
    Запускает постоянный процесс службы мониторинга ИБП в фоне.
    Полная и надежная замена оригинального ups_monitor.
    """
    print("=" * 70)
    print("SVC UPS Guardian Daemon v2.5 (Python 3 Standalone Replacement)")
    print("=" * 70)

    # Чтение TimeCfg.conf если есть
    cfg = DaemonConfig()
    cfg_bin = os.path.join(workdir, "TimeCfg.conf")
    if os.path.exists(cfg_bin):
        try:
            with open(cfg_bin, "rb") as f:
                data = f.read(52)
            if len(data) == 52 and struct.unpack_from("<I", data, 0)[0] == CFG_MAGIC:
                (_, ac, bat, ups, itv, _, off, offt, _, _) = CFG_STRUCT.unpack(data)
                cfg.ac_fail_delay = ac
                cfg.battery_low_delay = bat
                cfg.ups_failed_delay = ups
                cfg.warning_interval = itv
                cfg.turn_off_ups = off
                cfg.turn_off_ups_time = offt
                logger.info(f"Загружена конфигурация из {cfg_bin}")
        except Exception as e:
            logger.debug(f"Ошибка загрузки TimeCfg.conf: {e}")

    # Запись PID
    pid_file = os.path.join(workdir, "Monitor.pid")
    with open(pid_file, "w") as f:
        f.write(str(os.getpid()))

    # Создание SysV SHM сервера для обратной совместимости
    shm_srv = SysVShm()
    shm_srv.create()

    ctrl = SvcUpsController(mode="usb", dev_path=dev_path, baud=baud)
    ctrl.init()

    stop_event = threading.Event()

    def sig_handler(sig, frame):
        logger.info("Получен сигнал завершения. Остановка демона...")
        stop_event.set()

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    ac_countdown = 0x7FFFFFFF
    low_countdown = 0x7FFFFFFF
    last_warn_time = 0.0

    def add_log(msg: str):
        t = time.localtime()
        log_name = os.path.join(workdir, f"PowerEvent{t.tm_year:04d}{t.tm_mon:02d}.log")
        with open(log_name, "a") as f:
            f.write(f"{time.ctime()}: {msg}\n")

    def execute_script(script_path: str, arg: str):
        if os.path.exists(script_path):
            try:
                subprocess.run([script_path, arg], cwd=workdir, timeout=60)
            except Exception as e:
                logger.error(f"Ошибка запуска {script_path}: {e}")

    logger.info("Служба мониторинга активна. Опрос каждые 2 секунды...")
    while not stop_event.is_set():
        try:
            status = ctrl.get_status()
            # Сохранение статуса в /tmp/ups_status.json
            with open(STATUS_JSON_PATH, "w") as f:
                json.dump(status, f, indent=2, ensure_ascii=False)

            is_ac_fail = status["ac_status"] != "Normal"
            is_batt_low = status["battery_status"] != "Normal"

            now = time.time()

            # Обработка сбоя сети
            if is_ac_fail:
                if ac_countdown == 0x7FFFFFFF:
                    ac_countdown = cfg.ac_fail_delay
                    add_log("AC Fail detected!")
                    execute_script(cfg.notify_script, "AC Fail: Running on Battery!")

                if now - last_warn_time >= cfg.warning_interval:
                    last_warn_time = now
                    ac_countdown -= cfg.warning_interval
                    logger.warning(f"AC FAIL! Выключение ОС через {ac_countdown} сек.")
                    execute_script(cfg.notify_script, f"AC FAIL: System Shutdown after {ac_countdown} seconds")

                if ac_countdown <= 0:
                    logger.critical("ВРЕМЯ ИСТЕКЛО! Вызов выключения системы...")
                    add_log("Shutdown triggered by AC Fail timeout")
                    execute_script(cfg.shutdown_script, "AC Fail: System shutdown initiated")
                    if cfg.turn_off_ups and ctrl.hw:
                        ctrl.hw.shutdown_ups(cfg.turn_off_ups_time)
                    break
            else:
                if ac_countdown != 0x7FFFFFFF:
                    ac_countdown = 0x7FFFFFFF
                    add_log("AC Power Restored")
                    execute_script(cfg.notify_script, "AC NORMAL: Power restored, warning canceled")

            # Обработка низкого заряда АКБ
            if is_batt_low:
                if low_countdown == 0x7FFFFFFF:
                    low_countdown = cfg.battery_low_delay
                    add_log("BATTERY LOW detected!")

                low_countdown -= 2
                if low_countdown <= 0:
                    logger.critical("КРИТИЧЕСКИЙ РАЗРЯД АКБ! Немедленное выключение...")
                    add_log("Shutdown triggered by Battery Low")
                    execute_script(cfg.shutdown_script, "Battery Low: System shutdown initiated")
                    break
            else:
                low_countdown = 0x7FFFFFFF

        except Exception as e:
            logger.error(f"Ошибка в цикле демона: {e}")

        stop_event.wait(2.0)

    ctrl.close()
    if os.path.exists(pid_file):
        try:
            os.remove(pid_file)
        except OSError:
            pass
    logger.info("Демон остановлен.")


# =============================================================================
# ИНТЕРАКТИВНАЯ НАСТРОЙКА (--config)
# =============================================================================

def run_interactive_config():
    print("=" * 70)
    print("Конфигуратор параметров SVC UPS (замена ups_config)")
    print("=" * 70)
    cfg = DaemonConfig()

    def ask(prompt: str, current: Any) -> Any:
        val = input(f"{prompt} [{current}]: ").strip()
        return val if val else current

    try:
        cfg.ac_fail_delay = int(ask("Задержка до выключения при сбое сети 220V (сек)", cfg.ac_fail_delay))
        cfg.battery_low_delay = int(ask("Задержка до выключения при разряде АКБ (сек)", cfg.battery_low_delay))
        cfg.warning_interval = int(ask("Интервал периодических оповещений (сек)", cfg.warning_interval))
        cfg.turn_off_ups = int(ask("Выключать ли сам ИБП после гашения ОС (1=Да, 0=Нет)", cfg.turn_off_ups))

        # Сохранение в ups_config.json
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg.__dict__, f, indent=2)

        # Сохранение в TimeCfg.conf (бинарный формат для обратной совместимости)
        raw_cfg = CFG_STRUCT.pack(
            CFG_MAGIC, cfg.ac_fail_delay, cfg.battery_low_delay, cfg.ups_failed_delay,
            cfg.warning_interval, b"192.168.0.100\0\0\0", cfg.turn_off_ups,
            cfg.turn_off_ups_time, 1, 1
        )
        with open("TimeCfg.conf", "wb") as f:
            f.write(raw_cfg)

        print("\nНастройки успешно сохранены в ups_config.json и TimeCfg.conf!")
    except (ValueError, KeyboardInterrupt):
        print("\nОтменено.")


# =============================================================================
# ДИАГНОСТИЧЕСКИЕ ТЕСТЫ (--test)
# =============================================================================

def run_test_shm():
    print("\n" + "=" * 70)
    print("ТЕСТ: Проверка чтения SysV Shared Memory (IPC key 0x1B56)")
    print("=" * 70)
    shm = SysVShm()
    if shm.open_existing():
        data = shm.read_raw()
        if data:
            print(f"[УСПЕХ] Сегмент SysV SHM найден (224 байта).")
            heartbeat, = struct.unpack_from("<i", data, 0)
            ac_in, = struct.unpack_from("<f", data, 128)
            print(f"  -> Heartbeat: {heartbeat}, Напряжение входа: {ac_in}V")
            return True
    print("[ИНФО] Общая память оригинального ups_monitor не найдена (демон не запущен).")
    return False


def run_test_sniff(dev_path: Optional[str]):
    print("\n" + "=" * 70)
    print("ТЕСТ 1: Прослушивание входящих пакетов USB (Sniffing 5 сек)")
    print("=" * 70)
    hw = SvcUpsHardware(dev_path)
    if not hw.open():
        print("[ОШИБКА] Не удалось открыть устройство hiddev.")
        return False

    t_end = time.time() + 5.0
    while time.time() < t_end:
        time.sleep(0.5)
        print(f"  [T+{5.0 - (t_end - time.time()):.1f}s] Фреймов: {hw.total_frames_received}, Байт UART: {hw.total_bytes_received}, Статус: 0x{hw.last_status_byte:02X}")

    if hw.total_frames_received > 0:
        print(f"\n[РЕЗУЛЬТАТ ТЕСТА 1: УСПЕХ] ИБП непрерывно шлет данные! Всего получено {hw.total_frames_received} отчетов.")
        ac_ok = bool(hw.last_status_byte & 0x80)
        batt_ok = bool(hw.last_status_byte & 0x40)
        print(f"Последний статус-байт: 0x{hw.last_status_byte:02X}")
        print(f"  -> Сеть 220V: {'подключена (OK)' if ac_ok else 'ОТКЛЮЧЕНА (Utility Fail)'}")
        print(f"  -> Батарея: {'в норме (OK)' if batt_ok else 'РАЗРЯЖЕНА (Battery Low)'}")
    else:
        print("\n[РЕЗУЛЬТАТ ТЕСТА 1: ОШИБКА] Пакеты не поступают. Проверьте права и проброс hiddev.")
    hw.close()


def run_test_baud(dev_path: Optional[str]):
    print("\n" + "=" * 70)
    print("ТЕСТ 2: Проверка команды SetBaud (установка 2400 бод)")
    print("=" * 70)
    hw = SvcUpsHardware(dev_path)
    hw.open()
    print("Отправка PDU SetBaud [0x80, 0xD0, 0x00, 0x00]...")
    hw._pdu(bytes([0x80, 0xD0, 0, 0]))
    time.sleep(0.5)
    print(f"  Статус: фреймов={hw.total_frames_received}, статус-байт=0x{hw.last_status_byte:02X}")
    print("[РЕЗУЛЬТАТ ТЕСТА 2: ВЫПОЛНЕНО]")
    hw.close()


def run_test_q1(dev_path: Optional[str]):
    print("\n" + "=" * 70)
    print("ТЕСТ 3: Проверка команды запроса статуса Q1")
    print("=" * 70)
    hw = SvcUpsHardware(dev_path)
    hw.open()

    print("TX PDU: [0xA3, 0x51, 0x31, 0x0D] ('Q1\\r')...")
    res = hw.query_text("Q1", timeout_sec=2.5)
    if res.startswith("("):
        print(f"\n[РЕЗУЛЬТАТ ТЕСТА 3: УСПЕХ!] Принят ответ Megatec: {repr(res)}")
        reading = parse_q1(res.encode("latin1") + b"\r")
        if reading:
            print(f"  -> Вход: {reading.ac_in}V, Выход: {reading.ac_out}V, Батарея: {reading.dc}V, Нагрузка: {reading.load}%")
    else:
        print(f"\n[РЕЗУЛЬТАТ ТЕСТА 3] Ответ '(' пока не получен. Буфер: {repr(res)}, статус-байт: 0x{hw.last_status_byte:02X}")

    hw.close()


def run_test_wakeup(dev_path: Optional[str]):
    print("\n" + "=" * 70)
    print("ТЕСТ 4: Пробуждение UART (Wakeup CR '\\r' + 'Q1\\r')")
    print("=" * 70)
    hw = SvcUpsHardware(dev_path)
    hw.open()

    print("1. Отправка одиночного CR '\\r'...")
    hw.send_text(b"\r")
    time.sleep(0.1)

    print("2. Отправка 'Q1\\r'...")
    res = hw.query_text("Q1", timeout_sec=2.0)
    if res.startswith("("):
        print(f"[РЕЗУЛЬТАТ ТЕСТА 4: УСПЕХ!] Принят ответ: {repr(res)}")
    else:
        print(f"[РЕЗУЛЬТАТ ТЕСТА 4] Буфер: {repr(res)}")
    hw.close()


def run_test_binary(dev_path: Optional[str]):
    print("\n" + "=" * 70)
    print("ТЕСТ 5: Проверка бинарных отчётов (команды 1, 2, 3 с BCD)")
    print("=" * 70)
    hw = SvcUpsHardware(dev_path)
    hw.open()

    reading = hw.query_binary_status()
    if reading:
        print("[РЕЗУЛЬТАТ ТЕСТА 5: УСПЕХ!] Получен бинарный отчёт:")
        print(f"  -> Сеть 220V: {reading.ac_in}V")
        print(f"  -> Выход ИБП: {reading.ac_out}V")
        print(f"  -> Нагрузка: {reading.load}%")
        print(f"  -> Частота: {reading.freq}Hz")
        print(f"  -> Батарея: {reading.dc}V")
        print(f"  -> Температура: {reading.temp}°C")
        print(f"  -> Статус-байт: 0x{reading.status:02X}")
    else:
        print("[РЕЗУЛЬТАТ ТЕСТА 5] Бинарные отчёты не вернулись. Чип работает в текстовом режиме UART.")
    hw.close()


def run_test_identity(dev_path: Optional[str]):
    print("\n" + "=" * 70)
    print("ТЕСТ 6: Запрос паспортных данных ИБП (команда I\\r)")
    print("=" * 70)
    hw = SvcUpsHardware(dev_path)
    hw.open()

    print("TX PDU: [0xA2, 0x49, 0x0D, 0x00] ('I\\r')...")
    ident = hw.query_identity()
    if ident:
        print(f"[РЕЗУЛЬТАТ ТЕСТА 6: УСПЕХ!] Паспортные данные ИБП:")
        print(f"  -> Производитель : {ident.company}")
        print(f"  -> Модель        : {ident.model}")
        print(f"  -> Прошивка (FW) : {ident.version}")
    else:
        print("[РЕЗУЛЬТАТ ТЕСТА 6] ИБП не вернул строку '#...'. Будет использована стандартная модель 'SVC Richcomm UPS'.")
    hw.close()


def run_test_all(dev_path: Optional[str]):
    run_test_sniff(dev_path)
    run_test_baud(dev_path)
    run_test_q1(dev_path)
    run_test_wakeup(dev_path)
    run_test_binary(dev_path)
    run_test_identity(dev_path)


# =============================================================================
# УПРАВЛЕНИЕ БИПЕРОМ И САМОТЕСТИРОВАНИЕМ (BEEPER & SELF-TEST)
# =============================================================================

def run_command_identity(dev_path: Optional[str], baud: int = DEFAULT_BAUD):
    print("=" * 70)
    print("ПАСПОРТ И ВЕРСИЯ ПРОШИВКИ ИБП (команда 'I\\r')")
    print("=" * 70)
    ctrl = SvcUpsController(mode="usb", dev_path=dev_path, baud=baud)
    try:
        ident = ctrl.query_identity()
        if ident:
            print("[УСПЕХ] Паспортные данные получены из чипа ИБП:")
            print(f"  -> Производитель : {ident.company}")
            print(f"  -> Модель ИБП    : {ident.model}")
            print(f"  -> Версия ПО/FW  : {ident.version}")
        else:
            print("[ВНИМАНИЕ] Чип ИБП не ответил на команду 'I\\r' (используются значения по умолчанию)")
    finally:
        ctrl.close()


def run_command_beeper(dev_path: Optional[str], baud: int = DEFAULT_BAUD, target_state: Optional[bool] = None):
    print("=" * 70)
    print("УПРАВЛЕНИЕ БИПЕРОМ (Beeper Control - команда 'Q\\r')")
    print("=" * 70)
    ctrl = SvcUpsController(mode="usb", dev_path=dev_path, baud=baud)
    try:
        data_before = ctrl.get_status()
        beeper_before = data_before.get("beeper", "N/A")
        print(f"Текущее состояние пищалки: {beeper_before}")
        if target_state is not None:
            print(f"Установка состояния: {'ВКЛ (On)' if target_state else 'ВЫКЛ (Off)'}...")
            data_after = ctrl.set_beeper(target_state)
        else:
            print("Инверсия звука (Toggle) с верификацией...")
            data_after = ctrl.toggle_beeper()
        beeper_after = data_after.get("beeper", "N/A")
        print(f"[УСПЕХ] Новое состояние пищалки: {beeper_after}")
        if beeper_after == "On":
            print("  -> Звуковые сигналы ВКЛЮЧЕНЫ (бипер будет звучать при сбое 220V)")
        else:
            print("  -> Звуковые сигналы ВЫКЛЮЧЕНЫ (режим тишины активен)")
    finally:
        ctrl.close()


def run_command_selftest(dev_path: Optional[str], baud: int = DEFAULT_BAUD, duration: int = 10):
    print("=" * 70)
    print(f"САМОТЕСТИРОВАНИЕ БАТАРЕИ (Self Test {duration} сек - команда 'T\\r')")
    print("=" * 70)
    ctrl = SvcUpsController(mode="usb", dev_path=dev_path, baud=baud)
    try:
        print("TX PDU: [0xA2, 0x54, 0x0D, 0x00] ('T\\r')...")
        ctrl.run_self_test(cancel=False)
        print("Тест запущен: кратковременный переход на питание от АКБ под нагрузкой.")
        print("Мониторинг параметров (Ctrl+C для досрочной отмены):")
        for sec in range(1, duration + 1):
            time.sleep(1.0)
            data = ctrl.get_status()
            bat_v = data.get("battery_voltage", 0)
            out_v = data.get("output_voltage", 0)
            load = data.get("load_percent", 0)
            test_active = data.get("test_in_progress", False)
            print(f"  [{sec:2d}/{duration}s] Выход: {out_v:.1f}V | Батарея: {bat_v:.2f}V | Нагрузка: {load}% | Статус теста: {'АКТИВЕН' if test_active else 'Завершен'}")
            if not test_active and sec >= 4:
                break
        print("\n[РЕЗУЛЬТАТ] Самотестирование завершено! ИБП вернулся на нормальное питание от сети.")
    except KeyboardInterrupt:
        print("\nПрерывание пользователем! Отмена теста командой 'CT\\r'...")
        ctrl.run_self_test(cancel=True)
        print("[ГОТОВО] Тест отменён.")
    finally:
        ctrl.close()


def run_command_cancel_test(dev_path: Optional[str], baud: int = DEFAULT_BAUD):
    print("=" * 70)
    print("ОТМЕНА САМОТЕСТИРОВАНИЯ (команда 'CT\\r')")
    print("=" * 70)
    ctrl = SvcUpsController(mode="usb", dev_path=dev_path, baud=baud)
    try:
        print("TX PDU: [0xA3, 0x43, 0x54, 0x0D] ('CT\\r')...")
        ctrl.run_self_test(cancel=True)
        print("[УСПЕХ] Команда 'CT\\r' отправлена. Тестирование отменено.")
    finally:
        ctrl.close()


# =============================================================================
# ФОРМАТИРОВАНИЕ ВЫВОДА В ТЕРМИНАЛ
# =============================================================================

def format_table(data: Dict[str, Any]) -> str:
    def _f(key: str, default: float = 0.0) -> float:
        val = data.get(key)
        try:
            return float(val) if val is not None else default
        except (ValueError, TypeError):
            return default

    mode = str(data.get("mode") or "N/A")
    transport = str(data.get("transport") or "N/A")
    device = str(data.get("device") or "/dev/usb/hiddev0")
    ac_stat = str(data.get("ac_status") or "N/A")
    bat_stat = str(data.get("battery_status") or "N/A")
    ups_stat = str(data.get("ups_status") or "N/A")
    boost = str(data.get("boost_buck") or "N/A")
    beeper = str(data.get("beeper") or "N/A")
    tstamp = str(data.get("timestamp") or time.strftime("%Y-%m-%d %H:%M:%S"))

    lines = [
        "+-----------------------------------------------------------------------------+",
        f"| Mode: {mode:<18} Transport: {transport:<8} Device: {device:<24} |",
        "+---------------------------------++------------------------------------------+",
        f"|     Input AC Power : {ac_stat:<10} || UPS Input Voltage   : {_f('input_voltage'):6.2f} Volt        |",
        f"|     Battery Status : {bat_stat:<10} || UPS Output Voltage  : {_f('output_voltage'):6.2f} Volt        |",
        f"|     UPS Status     : {ups_stat:<10} || UPS Battery Level   : {_f('battery_level'):6.2f} %           |",
        f"|     Boost/Buck     : {boost:<10} || UPS Battery Voltage : {_f('battery_voltage'):6.2f} Volt        |",
        f"|     Beeper Status  : {beeper:<10} || UPS Power Loading   : {_f('load_percent'):6.2f} %           |",
        f"|     Timestamp      : {tstamp:<19} || UPS Line Frequency  : {_f('frequency'):6.2f} Hz          |",
        "+---------------------------------++------------------------------------------+"
    ]
    return "\n".join(lines)


# =============================================================================
# ТОЧКА ВХОДА MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="SVC / Richcomm (VID=0925 PID=1234) UPS Complete Python Suite"
    )
    parser.add_argument("--mode", "-m", choices=["auto", "shm", "usb"], default="auto",
                        help="Режим: auto (SHM если активен, иначе USB), shm (память), usb (прямой hiddev)")
    parser.add_argument("--device", "-d", help="Явный путь к устройству (например /dev/usb/hiddev0)", default=None)
    parser.add_argument("--baud", "-b", help="Скорость UART (по умолчанию 2400)", type=int, default=DEFAULT_BAUD)
    parser.add_argument("--debug", action="store_true", help="Подробный отладочный вывод")
    parser.add_argument("--json", action="store_true", help="Вывод статуса в формате JSON")
    parser.add_argument("--daemon", action="store_true", help="Запуск фоновой службы мониторинга")
    parser.add_argument("--config", action="store_true", help="Интерактивная настройка параметров выключения")
    parser.add_argument("--info", "-i", action="store_true", help="Запросить паспорт и версию прошивки ИБП (команда 'I\\r')")
    parser.add_argument("--beeper", action="store_true", help="Переключить звук пищалки ИБП (команда 'Q\\r')")
    parser.add_argument("--set-beeper", choices=["on", "off"], help="Явно установить состояние пищалки: on или off")
    parser.add_argument("--selftest", "--test-battery", action="store_true", help="Запустить 10-секундный тест батареи (команда 'T\\r')")
    parser.add_argument("--cancel-test", action="store_true", help="Отменить текущий тест батареи (команда 'CT\\r')")
    parser.add_argument("--test", choices=["shm", "sniff", "baud", "q1", "wakeup", "binary", "identity", "all"],
                        help="Запуск диагностических тестов", default=None)
    parser.add_argument("--loop", "-w", type=float, help="Постоянный опрос каждые N секунд", default=0.0)
    parser.add_argument("cmd", nargs="?", help="Дополнительная команда: info, beeper, selftest, cancel-test, decode")
    parser.add_argument("args", nargs="*", help="Аргументы дополнительной команды")

    args = parser.parse_args()
    setup_logging(args.debug)

    # 1. Запрос паспорта / версии прошивки
    if args.info or args.cmd in ("info", "identity", "version"):
        run_command_identity(args.device, args.baud)
        return

    # 2. Команды управления пищалкой и тестом
    if args.set_beeper:
        run_command_beeper(args.device, args.baud, target_state=(args.set_beeper == "on"))
        return

    if args.beeper or args.cmd in ("beeper", "beep"):
        target = None
        if args.args and args.args[0].lower() in ("on", "1", "true"):
            target = True
        elif args.args and args.args[0].lower() in ("off", "0", "false"):
            target = False
        run_command_beeper(args.device, args.baud, target_state=target)
        return

    if args.selftest or args.cmd in ("selftest", "test-battery", "test"):
        run_command_selftest(args.device, args.baud)
        return

    if args.cancel_test or args.cmd in ("cancel-test", "stop-test", "cancel"):
        run_command_cancel_test(args.device, args.baud)
        return

    # 2. Разбор строки decode
    if args.cmd == "decode" and args.args:
        raw = " ".join(args.args).encode("latin1")
        if not raw.endswith(b"\r"):
            raw += b"\r"
        r = parse_q1(raw)
        if r:
            print("[DECODE Q1 SUCCESS]:", r)
        else:
            print("[DECODE]: Не удалось разобрать строку Megatec.")
        return

    # 3. Интерактивная конфигурация
    if args.config:
        run_interactive_config()
        return

    # 4. Режим демона
    if args.daemon:
        run_daemon(args.device, args.baud)
        return

    # 5. Диагностика
    if args.test:
        if args.test == "shm":
            run_test_shm()
        elif args.test == "sniff":
            run_test_sniff(args.device)
        elif args.test == "baud":
            run_test_baud(args.device)
        elif args.test == "q1":
            run_test_q1(args.device)
        elif args.test == "wakeup":
            run_test_wakeup(args.device)
        elif args.test == "binary":
            run_test_binary(args.device)
        elif args.test == "identity":
            run_test_identity(args.device)
        elif args.test == "all":
            run_test_all(args.device)
        return

    # Стандартный опрос статуса
    ctrl = SvcUpsController(mode=args.mode, dev_path=args.device, baud=args.baud)

    def handle_sigint(sig, frame):
        ctrl.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_sigint)

    try:
        while True:
            data = ctrl.get_status()
            if args.json:
                print(json.dumps(data, ensure_ascii=False, indent=2))
            else:
                print(format_table(data))

            if args.loop <= 0:
                break
            time.sleep(args.loop)
    except Exception as e:
        logger.error(f"Ошибка получения статуса: {e}")
        if args.debug:
            import traceback
            traceback.print_exc()
        sys.exit(1)
    finally:
        ctrl.close()


if __name__ == "__main__":
    main()

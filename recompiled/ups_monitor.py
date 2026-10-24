#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ups_monitor.py - Python-реализация демона ups_monitor, восстановленная
реверс-инжинирингом ELF32/i386 бинарника (GCC 3.2.2, Red Hat 8/9, glibc 2.3.2).

Что делает оригинал (и эта реализация):
  * опрашивает ИБП по протоколу Megatec/Q1 (COM 2400 8N1) либо через USB-HID
    (VID 0x0925, PID 0x1234; два режима: "текст поверх HID" и бинарные BCD-отчёты);
  * следит за битами статуса (отказ сети, разряд батареи, отказ ИБП, bypass, тест);
  * при пропадании питания запускает обратный отсчёт, шлёт уведомления
    (./SendMSG.sh), пишет журнал ./PowerEventГГГГММ.log и по истечении времени
    вызывает ./shutdown.sh (и, опционально, командой S<n> выключает сам ИБП);
  * публикует состояние в SysV shared memory (ключ 0x1B56, 224 байта) для GUI;
  * конфигурация: бинарный файл ./TimeCfg.conf (52 байта, magic 0x55AA0335),
    имя процесса для сообщений: ./pm.conf ("ProgramName:ups_manager").

Отличия от оригинала (осознанные исправления багов) описаны в отчёте
и помечены в коде комментариями "ОТЛИЧИЕ".

Запуск:   ups_monitor.py run --device /dev/ttyS0 [--debug] [--dry-run] [--shm]
          ups_monitor.py simulate      # демо без железа (виртуальный ИБП на pty)
          ups_monitor.py selftest      # автотесты
          ups_monitor.py decode "(230.0 140.0 230.0 020 50.0 2.27 25.0 00001001"
Только stdlib, Linux.
"""
from __future__ import annotations

import argparse
import ctypes
import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import tty
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

INT_MAX = 0x7FFFFFFF
IPC_KEY = 0x1B56            # ключ shm и семафора (оригинал: 0x1b56)
SHM_SIZE = 224              # sizeof(UPS_DATA_Status)
CFG_MAGIC = 0x55AA0335
RETRY_COUNT = 5             # m_RetryCount: сбоев подряд до "UPS Adapter No Response"
NO_RESPONSE_SLEEP = 15      # пауза после сообщения о потере связи, сек
PROC_NAME_DEFAULT = "ups_manager"


# --------------------------------------------------------------------------- #
# Биты статуса (последние 8 символов ответа Q1, MSB first)
# --------------------------------------------------------------------------- #
class St:
    AC_FAIL = 0x80      # b7 Utility Fail (работа от батареи)
    LOW_BATT = 0x40     # b6 Battery Low
    BYPASS = 0x20       # b5 Bypass/Boost active
    UPS_FAIL = 0x10     # b4 UPS Failed
    STANDBY = 0x08      # b3 UPS Type: 1=standby, 0=online (оригинал не использует)
    TEST = 0x04         # b2 Test in progress
    SHUTDOWN = 0x02     # b1 Shutdown active (не использует)
    BEEPER = 0x01       # b0 Beeper on (не использует)


# --------------------------------------------------------------------------- #
# Конфигурация TimeCfg.conf  (struct TheCfgData, 52 байта, little-endian)
# --------------------------------------------------------------------------- #
CFG_STRUCT = struct.Struct("<5i16s4i")   # head, 3 delays, interval, ip[16], 4 ints
assert CFG_STRUCT.size == 52


@dataclass
class Config:
    head: int = CFG_MAGIC
    ac_fail_delay: int = 300        # сек до выключения при отказе сети (-1 = выкл.)
    battery_low_delay: int = 60     # сек до выключения при низком заряде (-1 = выкл.)
    ups_failed_delay: int = 180     # сек до выключения при отказе ИБП (-1 = выкл.)
    warning_interval: int = 10      # период уведомлений о обратном отсчёте
    ups_ip: str = "192.168.0.100"   # IP SNMP-адаптера (оригинал не использует)
    turn_off_ups: int = 0           # 1 = после shutdown послать ИБП команду S<n>
    turn_off_ups_time: int = 2      # n для S<n> (минут)
    com_port: int = 1               # номер COM (1 -> /dev/ttyS0)
    comm_mode: int = 0              # 0=COM, 1=USB-HID текст, 2=USB-HID бинарный

    def pack(self) -> bytes:
        return CFG_STRUCT.pack(
            self.head, self.ac_fail_delay, self.battery_low_delay,
            self.ups_failed_delay, self.warning_interval,
            self.ups_ip.encode("ascii", "replace")[:15],
            self.turn_off_ups, self.turn_off_ups_time, self.com_port, self.comm_mode)

    @classmethod
    def unpack(cls, raw: bytes) -> "Config":
        (head, ac, bat, ups, itv, ip, off, offt, port, mode) = CFG_STRUCT.unpack(raw)
        return cls(head, ac, bat, ups, itv,
                   ip.split(b"\0", 1)[0].decode("ascii", "replace"),
                   off, offt, port, mode)

    @classmethod
    def load(cls, path: str) -> "Config":
        """ReadConfiguration(): нет файла / неверный magic -> значения по умолчанию."""
        try:
            with open(path, "rb") as f:
                raw = f.read(CFG_STRUCT.size)
        except OSError:
            print("Read: Not open the file: TimeCfg.conf")
            return cls()
        if len(raw) < CFG_STRUCT.size:                    # ОТЛИЧИЕ: оригинал читал мусор
            print("Read: Data head not agree the file: TimeCfg.conf")
            return cls()
        cfg = cls.unpack(raw)
        if cfg.head != CFG_MAGIC:
            print("Read: Data head not agree the file: TimeCfg.conf")
            return cls()
        return cfg

    def save(self, path: str) -> None:
        with open(path, "wb") as f:
            f.write(self.pack())


def read_program_name(path: str = "./pm.conf") -> str:
    """InitPMCfg(): ищет 'ProgramName:' в pm.conf, '#' начинает комментарий."""
    name = PROC_NAME_DEFAULT
    try:
        with open(path, "r", errors="replace") as f:
            for line in f:
                for i, ch in enumerate(line):
                    if ch in "\r\n#":
                        line = line[:i]
                        break
                pos = line.find("ProgramName")
                if pos >= 0 and line[pos + 11:pos + 12] == ":":
                    name = line[pos + 12:][:255]
    except OSError:
        pass
    return name


# --------------------------------------------------------------------------- #
# Данные ИБП (struct UPS_DATA_Status, 224 байта) и SysV shared memory
# --------------------------------------------------------------------------- #
UPS_STRUCT = struct.Struct("<i120si6fi4f18s16s18s")
assert UPS_STRUCT.size == SHM_SIZE


@dataclass
class UpsData:
    heartbeat: int = 0              # nMesgLen: оригинал хранит тут счётчик циклов
    message: bytes = b""            # cMesgTxt[120]: почтовый ящик команд ("shutdown", "start")
    nset: int = 0                   # nSet: 1 = GUI просит перечитать TimeCfg.conf
    ac_in: float = 0.0              # fAcInValue
    ac_out: float = 0.0             # fAcOutValue
    ac_freq: float = 0.0            # fAcFreValue
    dc: float = 0.0                 # fDcValue (батарея)
    temp: float = 0.0               # fUpsTemp
    load: float = 0.0               # fLoad, %
    warn_status: int = 0            # nWarnStatus = байт статуса
    rate_ac_in: float = 0.0         # fRate_AcIn  (команда F)
    rate_dc: float = 0.0            # fRate_DcValue
    rate_freq: float = 0.0          # fRate_AcFre
    rate_load: float = 0.0          # fRate_Load (номинальный ток)
    company: str = ""               # cCompanyName (команда I)
    model: str = ""                 # cUpsModel
    version: str = ""               # cVersion

    def clear(self) -> None:
        """ClrAllData(): обнулить измерения (heartbeat/message/nset не трогает)."""
        self.ac_in = self.ac_out = self.ac_freq = self.dc = self.temp = self.load = 0.0
        self.warn_status = 0
        self.rate_ac_in = self.rate_dc = self.rate_freq = self.rate_load = 0.0
        self.company = self.model = self.version = ""

    def pack(self) -> bytes:
        enc = lambda s, n: s.encode("ascii", "replace")[:n - 1]
        return UPS_STRUCT.pack(
            self.heartbeat, self.message[:119], self.nset,
            self.ac_in, self.ac_out, self.ac_freq, self.dc, self.temp, self.load,
            self.warn_status, self.rate_ac_in, self.rate_dc, self.rate_freq,
            self.rate_load, enc(self.company, 18), enc(self.model, 16),
            enc(self.version, 18))

    @classmethod
    def unpack(cls, raw: bytes) -> "UpsData":
        (hb, msg, nset, ai, ao, af, dc, tmp, ld, ws, rai, rdc, rf, rl,
         co, mo, ve) = UPS_STRUCT.unpack(raw)
        z = lambda b: b.split(b"\0", 1)[0].decode("ascii", "replace")
        return cls(hb, msg.split(b"\0", 1)[0], nset, ai, ao, af, dc, tmp, ld,
                   ws, rai, rdc, rf, rl, z(co), z(mo), z(ve))


class _Sembuf(ctypes.Structure):
    _fields_ = [("sem_num", ctypes.c_ushort), ("sem_op", ctypes.c_short),
                ("sem_flg", ctypes.c_short)]


class SysVShm:
    """Совместимый с оригиналом обмен: shm key 0x1B56 (224 Б) + семафор key 0x1B56.
    Блокировка как в LockSem(): [wait-for-zero, +1 SEM_UNDO]; разблокировка: -1."""
    IPC_CREAT, IPC_RMID, SEM_UNDO, IPC_NOWAIT = 0o1000, 0, 0x1000, 0o4000

    def __init__(self, key: int = IPC_KEY):
        libc = ctypes.CDLL(None, use_errno=True)
        libc.shmget.argtypes = [ctypes.c_int, ctypes.c_size_t, ctypes.c_int]
        libc.shmat.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        libc.shmat.restype = ctypes.c_void_p
        libc.shmdt.argtypes = [ctypes.c_void_p]
        libc.shmctl.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
        libc.semget.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
        libc.semop.argtypes = [ctypes.c_int, ctypes.POINTER(_Sembuf), ctypes.c_size_t]
        self.libc, self.key = libc, key
        self.shmid = libc.shmget(key, SHM_SIZE, self.IPC_CREAT | 0o600)
        self.semid = libc.semget(key, 1, self.IPC_CREAT | 0o600)
        if self.shmid < 0 or self.semid < 0:
            raise OSError(ctypes.get_errno(), "shmget/semget failed")

    def _lock(self, lock: bool) -> None:
        if lock:
            ops = (_Sembuf * 2)(_Sembuf(0, 0, 0), _Sembuf(0, 1, self.SEM_UNDO))
        else:
            ops = (_Sembuf * 1)(_Sembuf(0, -1, self.SEM_UNDO | self.IPC_NOWAIT))
        self.libc.semop(self.semid, ops, len(ops))

    def read(self) -> UpsData:
        self._lock(True)
        try:
            p = self.libc.shmat(self.shmid, None, 0)
            raw = ctypes.string_at(p, SHM_SIZE)
            self.libc.shmdt(p)
        finally:
            self._lock(False)
        return UpsData.unpack(raw)

    def write(self, d: UpsData) -> None:
        self._lock(True)
        try:
            p = self.libc.shmat(self.shmid, None, 0)
            ctypes.memmove(p, d.pack(), SHM_SIZE)
            self.libc.shmdt(p)
        finally:
            self._lock(False)

    def delete(self) -> None:
        self.libc.shmctl(self.shmid, self.IPC_RMID, None)
        self.libc.semctl(self.semid, 0, self.IPC_RMID, 0)


# --------------------------------------------------------------------------- #
# Разбор ответов протокола Megatec
#   Q1\r  -> (MMM.M NNN.N PPP.P QQQ RR.R S.SS TT.T b7b6b5b4b3b2b1b0\r   (46 Б после '(')
#   I\r   -> #Company_Name_ UPS_Model_ Version___\r                      (38 Б после '#')
#   F\r   -> #MMM.M QQQ SS.SS FF.F\r                                     (21 Б после '#')
# В оригинале имена функций перепутаны: Quary_F шлёт "I\r", Quary_I шлёт "F\r".
# --------------------------------------------------------------------------- #
@dataclass
class StatusReading:
    ac_in: Optional[float]
    ac_out: Optional[float]
    load: Optional[float]
    freq: Optional[float]
    dc: Optional[float]
    temp: Optional[float]
    status: int


@dataclass
class Identity:
    company: str
    model: str
    version: str


@dataclass
class Rating:
    ac_in: Optional[float]
    load: Optional[float]       # номинальный ток (поле QQQ)
    dc: Optional[float]
    freq: Optional[float]


def _chk(tok: str, lo: float, hi: float) -> Optional[float]:
    """strtod + проверка диапазона lo < v < hi (границы жёстко зашиты в .rodata)."""
    try:
        v = float(tok)
    except ValueError:
        v = 0.0                                  # strtod("garbage") == 0.0
    return v if lo < v < hi else None


def parse_q1(frame: bytes) -> Optional[StatusReading]:
    if not frame or frame[0] != 0x28 or frame[-1] != 0x0D:
        return None
    tok = frame[1:-1].decode("ascii", "replace").split()
    if len(tok) != 8 or len(tok[7]) != 8:        # tok[1] (fault voltage) игнорируется
        return None
    status = sum(1 << (7 - i) for i, c in enumerate(tok[7]) if c == "1")
    return StatusReading(
        ac_in=_chk(tok[0], -1, 450), ac_out=_chk(tok[2], -1, 450),
        load=_chk(tok[3], -1, 150), freq=_chk(tok[4], 0, 65),
        dc=_chk(tok[5], -1, 250), temp=_chk(tok[6], -20, 100), status=status)


def parse_identity(frame: bytes) -> Optional[Identity]:
    if not frame or frame[0] != 0x23 or frame[-1] != 0x0D:
        return None
    body = frame[1:-1].decode("ascii", "replace")
    if len(body) == 37:                          # фиксированные смещения 0/16/27
        return Identity(body[0:15].strip(), body[16:26].strip(), body[27:37].strip())
    tok = body.split()
    if len(tok) >= 3:
        return Identity(" ".join(tok[:-2]), tok[-2], tok[-1])
    return None


def parse_rating(frame: bytes) -> Optional[Rating]:
    if not frame or frame[0] != 0x23 or frame[-1] != 0x0D:
        return None
    tok = frame[1:-1].decode("ascii", "replace").split()
    if len(tok) != 4:
        return None
    return Rating(ac_in=_chk(tok[0], 0, 450), load=_chk(tok[1], -1, 150),
                  dc=_chk(tok[2], -1, 250), freq=_chk(tok[3], 0, 65))


def bcd(b: int) -> int:
    return (b >> 4) * 10 + (b & 0x0F)


# --------------------------------------------------------------------------- #
# Транспорты
# --------------------------------------------------------------------------- #
class LinkError(Exception):
    pass


class UpsLink:
    """Интерфейс: то, что Start() требует от слоя связи."""
    failed = False                                 # аналог dev_err

    def open(self) -> bool: raise NotImplementedError
    def close(self) -> None: raise NotImplementedError
    def query_status(self) -> Optional[StatusReading]: raise NotImplementedError
    def query_identity(self) -> Optional[Identity]: raise NotImplementedError
    def query_rating(self) -> Optional[Rating]: raise NotImplementedError
    def shutdown_ups(self, minutes: int) -> None: raise NotImplementedError


_BAUDS = {110: termios.B110, 300: termios.B300, 600: termios.B600,
          1200: termios.B1200, 2400: termios.B2400, 4800: termios.B4800,
          9600: termios.B9600, 19200: termios.B19200, 38400: termios.B38400,
          57600: termios.B57600, 115200: termios.B115200}


class SerialLink(UpsLink):
    """InitializeCom()+SendStr()+GetStr(): сырой termios, 8N1, без управления потоком,
    c_iflag=c_oflag=c_lflag=0, VMIN=VTIME=0, open(O_RDWR|O_NOCTTY|O_NONBLOCK)."""

    def __init__(self, device: str, baud: int = 2400, timeout: float = 1.5):
        self.device, self.baud, self.timeout = device, baud, timeout
        self.fd = -1
        self._saved = None

    def open(self) -> bool:
        try:
            fd = os.open(self.device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK, 0o666)
        except OSError as e:
            print(f"{self.device}: {e.strerror}")
            self.failed = True
            return False
        speed = _BAUDS.get(self.baud, termios.B2400)       # неизвестная скорость -> 2400
        try:
            self._saved = termios.tcgetattr(fd)
            cc = [0] * len(self._saved[6])
            cflag = speed | termios.CS8 | termios.CLOCAL | termios.CREAD
            termios.tcflush(fd, termios.TCIFLUSH)
            termios.tcsetattr(fd, termios.TCSANOW, [0, 0, cflag, 0, speed, speed, cc])
        except termios.error as e:
            os.close(fd)
            print(f"{self.device}: termios: {e}")
            self.failed = True
            return False
        self.fd, self.failed = fd, False
        return True

    def close(self) -> None:
        if self.fd >= 0:
            try:
                if self._saved:
                    termios.tcsetattr(self.fd, termios.TCSANOW, self._saved)
            except termios.error:
                pass
            os.close(self.fd)
        self.fd = -1

    def _xfer(self, cmd: bytes, header: int) -> Optional[bytes]:
        if self.fd < 0:
            return None
        try:
            termios.tcflush(self.fd, termios.TCIFLUSH)    # ОТЛИЧИЕ: сброс старых байт
            os.write(self.fd, cmd)
        except OSError:
            self.failed = True
            return None
        deadline = time.monotonic() + self.timeout        # оригинал: sleep(1) + read
        buf, started = bytearray(), False
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            r, _, _ = select.select([self.fd], [], [], left)
            if not r:
                return None
            try:
                chunk = os.read(self.fd, 256)
            except BlockingIOError:
                continue
            except OSError:
                self.failed = True
                return None
            if not chunk:
                time.sleep(0.01)
                continue
            for b in chunk:
                if not started:
                    if b == header:
                        started = True
                        buf.append(b)
                else:
                    buf.append(b)
                    if b == 0x0D:
                        return bytes(buf)

    def query_status(self):   return parse_q1(self._xfer(b"Q1\r", 0x28) or b"")
    def query_identity(self): return parse_identity(self._xfer(b"I\r", 0x23) or b"")
    def query_rating(self):   return parse_rating(self._xfer(b"F\r", 0x23) or b"")

    def shutdown_ups(self, minutes: int) -> None:
        """ShutdownUPS() режим 0: 'S%02d\\r'."""
        if self.fd >= 0:
            os.write(self.fd, b"S%02d\r" % minutes)


# --- USB HID (hiddev) -------------------------------------------------------
HIDIOCGDEVINFO, HIDIOCSUSAGE, HIDIOCSREPORT = 0x801C4803, 0x4018480C, 0x400C4808
HIDIOCGUSAGE, HIDIOCGUCODE = 0xC018480B, 0xC018480D
_USAGE_REF = struct.Struct("<IIIIIi")     # type, id, field_idx, usage_idx, code, value
_REPORT_INFO = struct.Struct("<III")      # type, id, num_fields
_EVENT = struct.Struct("<Ii")             # hid, value
HID_VID, HID_PID = 0x0925, 0x1234


class HiddevDevice:
    """НЕ ПРОВЕРЕНО НА ЖЕЛЕЗЕ (устройства не было при разборе): логика
    восстановлена из UsbOpen/UsbSendPDU/SendData/USBReadThread/SetBaud."""

    def __init__(self, baud: int = 2400):
        self.baud, self.fd = baud, -1
        self.uart, self.usb = bytearray(), bytearray()      # кольцевые буферы -> bytearray
        self.cond = threading.Condition()
        self.thread: Optional[threading.Thread] = None
        self.error = False
        self._stop = False

    # -- низкий уровень --
    def _pdu(self, pdu: bytes) -> bool:
        """UsbSendPDU: 4 байта как 4 usage (field=(i>>1)&1, usage=i&1) + SET_REPORT(feature)."""
        try:
            for i in range(4):
                v = pdu[i] - 256 if pdu[i] > 127 else pdu[i]
                fcntl.ioctl(self.fd, HIDIOCSUSAGE,
                            _USAGE_REF.pack(2, 0, (i >> 1) & 1, i & 1, 0, v))
            fcntl.ioctl(self.fd, HIDIOCSREPORT, _REPORT_INFO.pack(2, 0, 0))
            return True
        except OSError:
            self.error = True
            return False

    def open(self) -> bool:
        self.close()
        for i in range(16):
            path = f"/dev/usb/hiddev{i}"
            for attempt in (0, 1):
                try:
                    fd = os.open(path, os.O_RDWR)
                    break
                except FileNotFoundError:
                    try:
                        os.mknod(path, 0o20666, os.makedev(180, 96 + i))
                    except OSError:
                        fd = -1
                        break
                    fd = -1
                except OSError:
                    fd = -1
                    break
            else:
                fd = -1
            if fd < 0:
                continue
            info = bytearray(28)
            try:
                fcntl.ioctl(fd, HIDIOCGDEVINFO, info, True)
            except OSError:
                os.close(fd)
                continue
            vid, pid = struct.unpack_from("<HH", info, 16)
            if (vid, pid) != (HID_VID, HID_PID):
                os.close(fd)
                continue
            self.fd, self.error = fd, False
            self._pdu(bytes([0x80, (500000 // self.baud) & 0xFF, 0, 0]))   # SetBaud
            self.clear()
            if self.thread is None or not self.thread.is_alive():
                self._stop = False
                self.thread = threading.Thread(target=self._reader, daemon=True)
                self.thread.start()
            return True
        return False

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
        self.fd = -1

    def shutdown_thread(self) -> None:
        self._stop = True
        self.close()

    def clear(self) -> None:
        with self.cond:
            self.uart.clear()
            self.usb.clear()

    def _reader(self) -> None:
        """USBReadThread: select(250 мс) -> read(hiddev_event[]) -> собрать 6-байтовый отчёт."""
        while not self._stop:
            if self.fd < 0:
                time.sleep(1)
                continue
            try:
                r, _, _ = select.select([self.fd], [], [], 0.25)
                if not r:
                    continue
                data = os.read(self.fd, 512)
            except OSError:
                self.error = True
                time.sleep(1)
                continue
            if not data:
                self.error = True
                continue
            rep = bytearray(8)
            for off in range(0, len(data) - 7, 8):
                hid, val = _EVENT.unpack_from(data, off)
                idx = (hid & 0xFF) - 3
                if 0 <= idx < 6:
                    rep[idx] = val & 0xFF
            with self.cond:
                if rep[0] & 0x80:                     # UART-кадр: [0x80|len, d1..d5]
                    n = min(rep[0] & 7, 5)
                    self.uart += rep[1:1 + n]
                else:                                 # бинарный отчёт 6 байт
                    self.usb += rep[:6]
                self.cond.notify_all()

    # -- высокий уровень --
    def send_text(self, data: bytes) -> bool:
        """SendData: куски по ≤3 байта, PDU = [0xA0|n, d0, d1, d2], пауза n*11000/baud мс."""
        pos = 0
        while pos < len(data):
            chunk = data[pos:pos + 3]
            pdu = bytes([0xA0 | len(chunk)]) + chunk.ljust(3, b"\0")
            if not self._pdu(pdu):
                return False
            time.sleep(int(len(chunk) * (11000.0 / self.baud) + 0.5) / 1000.0)
            pos += len(chunk)
        return True

    def write_raw(self, pdu4: bytes) -> bool:
        ok = self._pdu(pdu4[:4].ljust(4, b"\0"))
        time.sleep(0.001)
        return ok

    def _take(self, buf: bytearray, n: int, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        with self.cond:
            while len(buf) < n:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self.cond.wait(left)
            out = bytes(buf[:n])
            del buf[:n]
            return out

    def read_text(self, n: int, timeout: float = 1.0) -> bytes: return self._take(self.uart, n, timeout)
    def read_raw(self, n: int = 6, timeout: float = 1.0) -> bytes: return self._take(self.usb, n, timeout)


class HidTextLink(UpsLink):
    """comm_mode=1: тот же Megatec-текст, но через UART-кадры HID."""

    def __init__(self, dev: Optional[HiddevDevice] = None, baud: int = 2400):
        self.dev = dev or HiddevDevice(baud)

    @property
    def failed(self): return self.dev.error
    @failed.setter
    def failed(self, v): pass

    def open(self): return self.dev.open()
    def close(self): self.dev.shutdown_thread()

    def _xfer(self, cmd: bytes, nbytes: int) -> bytes:
        self.dev.clear()
        if not self.dev.send_text(cmd):
            return b""
        return self.dev.read_text(nbytes, 1.0)

    def query_status(self):   return parse_q1(self._xfer(b"Q1\r", 47))      # '(' + 46
    def query_identity(self): return parse_identity(self._xfer(b"I\r", 39)) # '#' + 38
    def query_rating(self):   return parse_rating(self._xfer(b"F\r", 22))   # '#' + 21

    def shutdown_ups(self, minutes: int) -> None:
        self.dev.send_text(b"S%02d\r" % minutes)


class HidBinaryLink(UpsLink):
    """comm_mode=2: команды-отчёты [cmd,0,0,0] -> ответ 6 байт [cmd,b1..b5], числа в BCD."""

    def __init__(self, dev: Optional[HiddevDevice] = None, baud: int = 2400):
        self.dev = dev or HiddevDevice(baud)

    @property
    def failed(self): return self.dev.error
    @failed.setter
    def failed(self, v): pass

    def open(self): return self.dev.open()
    def close(self): self.dev.shutdown_thread()

    def _report(self, cmd: int) -> Optional[bytes]:
        self.dev.clear()
        self.dev.write_raw(bytes([cmd, 0, 0, 0]))
        rep = self.dev.read_raw(6, 1.0)
        return rep if len(rep) == 6 and rep[0] == cmd else None

    def query_status(self) -> Optional[StatusReading]:
        r1 = self._report(1)
        if r1 is None or (r1[4] & 1):               # байт4 бит0 = "данные недостоверны"
            return None
        ac_in = bcd(r1[1]) * 10 + bcd(r1[2]) / 10.0
        status = r1[5]
        ac_out = freq = load = dc = temp = None
        r2 = self._report(2)
        if r2:
            ac_out = bcd(r2[1]) * 10 + bcd(r2[2]) / 10.0
            freq = bcd(r2[3]) * 10 + bcd(r2[4]) / 10.0
            load = float(bcd(r2[5]))
        r3 = self._report(3)
        if r3:
            dc = bcd(r3[1]) + bcd(r3[2]) / 100.0
            temp = bcd(r3[3]) * 10 + bcd(r3[4]) / 10.0
        return StatusReading(ac_in, ac_out, load, freq, dc, temp, status)

    def query_identity(self) -> Optional[Identity]:
        def text(cmds):
            out = b""
            for c in cmds:
                r = self._report(c)
                if r is None:
                    return None
                out += r[1:6]
            return out.split(b"\0", 1)[0].decode("ascii", "replace").strip()
        co, mo, ve = text((6, 7, 8)), text((9, 10)), text((11, 12))
        return None if None in (co, mo, ve) else Identity(co, mo, ve)

    def query_rating(self) -> Optional[Rating]:
        r4 = self._report(4)
        rate_ac = rate_load = None
        if r4:
            rate_ac = bcd(r4[1]) * 10 + bcd(r4[2]) / 10.0
            rate_load = bcd(r4[3]) * 100 + float(bcd(r4[4]))
        r5 = self._report(5)
        if r5 is None:
            return None
        return Rating(rate_ac, rate_load, bcd(r5[1]) + bcd(r5[2]) / 100.0,
                      bcd(r5[3]) * 10 + bcd(r5[4]) / 10.0)

    def shutdown_ups(self, minutes: int) -> None:
        self.dev.clear()
        self.dev.write_raw(bytes([0x11, minutes & 0xFF, 0, 0]))   # ShutdownUPS режим 2


# --------------------------------------------------------------------------- #
# Виртуальный ИБП (Megatec) на pty - для демо и тестов
# --------------------------------------------------------------------------- #
class MegatecSimulator:
    def __init__(self, company="SIMCORP", model="SIM-1000", version="V1.0"):
        self.master, self.slave = pty.openpty()
        tty.setraw(self.slave)
        self.slave_name = os.ttyname(self.slave)
        self.company, self.model, self.version = company, model, version
        self.v_in, self.v_out, self.v_fault = 230.0, 230.0, 140.0
        self.load, self.freq, self.batt, self.temp = 20, 50.0, 2.27, 25.0
        self.status = 0b00001000
        self.muted = False
        self.commands: List[bytes] = []
        self._run = True
        self._t = threading.Thread(target=self._loop, daemon=True)

    def start(self): self._t.start(); return self

    def stop(self):
        self._run = False
        self._t.join(1)
        for fd in (self.master, self.slave):
            try: os.close(fd)
            except OSError: pass

    def set_flag(self, bit: int, on: bool):
        self.status = (self.status | bit) if on else (self.status & ~bit)

    def ac_fail(self, on: bool):
        self.set_flag(St.AC_FAIL, on)
        self.v_in = 0.0 if on else 230.0
        self.v_out = 230.0
        self.batt = 2.10 if on else 2.27

    def _loop(self):
        buf = b""
        while self._run:
            try:
                r, _, _ = select.select([self.master], [], [], 0.1)
                if not r:
                    continue
                buf += os.read(self.master, 256)
            except OSError:
                return
            while b"\r" in buf:
                cmd, buf = buf.split(b"\r", 1)
                self.commands.append(cmd)
                if not self.muted:
                    resp = self._answer(cmd)
                    if resp:
                        os.write(self.master, resp)

    def _answer(self, cmd: bytes) -> bytes:
        if cmd == b"Q1":
            bits = format(self.status, "08b")
            return ("(%05.1f %05.1f %05.1f %03d %04.1f %04.2f %04.1f %s\r" % (
                self.v_in, self.v_fault, self.v_out, self.load, self.freq,
                self.batt, self.temp, bits)).encode()
        if cmd == b"I":
            return ("#%-15s %-10s %-10s\r" % (self.company, self.model, self.version)).encode()
        if cmd == b"F":
            return b"#230.0 004 12.00 50.0\r"
        return b""


# --------------------------------------------------------------------------- #
# Монитор: конечный автомат Start()
# --------------------------------------------------------------------------- #
class Monitor:
    def __init__(self, link: UpsLink, cfg: Config, *, workdir: str = ".",
                 debug: bool = False, dry_run: bool = False,
                 shm: Optional[SysVShm] = None, proc_name: Optional[str] = None,
                 cfg_path: Optional[str] = None,
                 clock: Callable[[], float] = time.time,
                 sleep: Optional[Callable[[float], None]] = None):
        self.link, self.cfg, self.workdir = link, cfg, workdir
        self.debug, self.dry_run, self.shm = debug, dry_run, shm
        self.cfg_path = cfg_path or os.path.join(workdir, "TimeCfg.conf")
        self.proc_name = proc_name or read_program_name(os.path.join(workdir, "pm.conf"))
        self.clock = clock
        self._stop = threading.Event()
        self.sleep = sleep or (lambda s: self._stop.wait(s))
        self.data = UpsData()
        self.status = 0                  # UPS_status
        self.status_old = 0              # UPS_status_old
        self.countdown = [INT_MAX] * 3   # iShutDownCountDown[AC, LOW CELL, UPS FAIL]
        self.shutdown_flag = False       # bShutdownUps
        self.query_ok = True             # bQuarySuccess
        self.ups_ready = True            # m_UpsReady
        self.fail_count = 0
        self.counter = -1
        self.old_warn_time = int(clock())   # oldwarntime
        self.executed: List[Tuple[str, Tuple[str, ...]]] = []   # для тестов
        self._opened = False

    # ---- служебное ----
    def stop(self): self._stop.set()

    def _dbg(self, text: str) -> None:
        if self.debug:
            print(text, flush=True)

    def add_log(self, text: str) -> None:
        """AddLog(): ./PowerEventГГГГММ.log, строка 'ctime: текст'."""
        t = time.localtime(self.clock())
        path = os.path.join(self.workdir, "PowerEvent%04d%02d.log" % (t.tm_year, t.tm_mon))
        self._dbg(f"Save Log:{path}")
        try:
            with open(path, "a") as f:
                f.write(f"{time.ctime(self.clock())}: {text}\n")
        except OSError:
            print("doesn't Open the log file")

    def _script(self, script: str, *args: str) -> None:
        """system('./script "arg"'). В dry-run только фиксируем вызов."""
        path = os.path.join(self.workdir, script)
        self.executed.append((script, args))
        cmdline = f'./{script}  ' + " ".join(f'"{a}"' for a in args)
        if self.dry_run or not os.path.isfile(path):
            self._dbg(f"[no-exec] {cmdline}")
            return
        try:
            subprocess.run([path, *args], cwd=self.workdir, timeout=300)
        except (OSError, subprocess.SubprocessError) as e:
            print(f"{cmdline}: {e}")

    # ---- SendMessage(): уведомления обратного отсчёта ----
    def send_message(self, kind: int) -> None:
        msg = {0: "AC FAIL:System Shutdown after %d Second" % self.countdown[0],
               1: "LOW CELL:System Shutdown after %d Second" % self.countdown[1],
               2: "UPS FAIL:System Shutdown after %d Second" % self.countdown[2],
               3: "UPS Adapter No Response, please stop the process!!"}.get(kind)
        if msg:
            self._dbg(f'./SendMSG.sh  "{msg}"')
            self._script("SendMSG.sh", msg)

    def _notify(self, text: str) -> None:
        full = f"Received By {self.proc_name}  -->  {text}"
        self.add_log(f'./SendMSG.sh  "{full}"')
        self._script("SendMSG.sh", full)

    # ---- Process_UPS_warging / Process_UPS_Cancel_warging ----
    def on_warning(self, bit: int) -> None:
        if bit == 7:
            self.countdown[0] = self.cfg.ac_fail_delay
            self._notify("AC Fail")
        elif bit == 6:
            self.countdown[1] = self.cfg.battery_low_delay
            self._notify("LOW CELL")
        elif bit == 5:
            self._notify("UPS BYPASS")
        elif bit == 4:
            self.countdown[2] = self.cfg.ups_failed_delay
            self._notify("UPS Failed")
        elif bit == 2:
            self._notify("UPS TESTTING")           # (sic) опечатка оригинала сохранена

    def on_cancel(self, bit: int) -> None:
        # ОТЛИЧИЕ: оригинал заодно сбрасывает несвязанные младшие биты UPS_status
        # (маски 0xFE/0xFD/0xFB) - явный баг, здесь не воспроизводится.
        if bit == 7:
            self.countdown[0] = INT_MAX
            self._notify("AC NORMAL,Warnning Canceled")
        elif bit == 6:
            self.countdown[1] = INT_MAX
            self._notify("Battery OK,Warnning Canceled ")
        elif bit == 4:
            self.countdown[2] = INT_MAX
            self._notify("UPS OK,Warnning Canceled  ")
        elif bit == 5:
            self._notify("BYPASS OK,Warnning Canceled  ")
        elif bit == 2:
            self._notify("TEST OK,Warnning Canceled  ")

    # ---- Shutdown() / ProcessCountDown() ----
    def do_shutdown(self, reason: int) -> None:
        if self.shutdown_flag:
            return        # ОТЛИЧИЕ: оригинал здесь вызывал system() с неинициализированным буфером
        text = {1: "AC Fail:System may be shutdown.", 2: "LOW CELL:System may be shutdown.",
                3: "UPS BAD:System may be shutdown."}.get(reason, "System may be shutdown.")
        self.add_log(f'./shutdown.sh "{text}"')
        self.shutdown_flag = True
        self._script("shutdown.sh", text)

    def process_countdown(self) -> None:
        cfg, cd, now = self.cfg, self.countdown, int(self.clock())
        active, idx = False, 0
        if self.status & St.AC_FAIL and cfg.ac_fail_delay != -1:
            active = True
            if cd[0] <= 0:
                self.do_shutdown(1)
        if self.status & St.LOW_BATT and cfg.battery_low_delay != -1:
            active = True
            if cd[1] <= 0:
                self.do_shutdown(2)
            if cd[1] < cd[idx]:
                idx = 1
        if self.status & St.UPS_FAIL and cfg.ups_failed_delay != -1:
            active = True
            if cd[2] <= 0:
                self.do_shutdown(3)
            if cd[2] < cd[idx]:
                idx = 2
        if active and now - self.old_warn_time >= cfg.warning_interval:
            self.old_warn_time = now
            cd[idx] -= cfg.warning_interval
            self._dbg("%s,%d,iShutDownCountDown[index] = %d,pTheCfgData->WarningInterval = %d"
                      % ("ups_monitor.c", 0x49E, cd[idx], cfg.warning_interval))
            self.send_message(idx)

    # ---- разбор показаний ----
    def apply_reading(self, r: StatusReading) -> None:
        d = self.data
        for attr, val in (("ac_in", r.ac_in), ("ac_out", r.ac_out), ("load", r.load),
                          ("ac_freq", r.freq), ("dc", r.dc), ("temp", r.temp)):
            if val is not None:
                setattr(d, attr, val)
        self.status = r.status
        d.warn_status = r.status

    def handle_status_change(self) -> None:
        if self.status_old != self.status:
            diff = self.status_old ^ self.status
            for bit in range(8):
                if diff >> bit & 1:
                    if self.status >> bit & 1:
                        self.on_warning(bit)
                    else:
                        self.on_cancel(bit)
                    self._dbg("UPS warning : %d %02x->%d " % (bit, self.status, self.status >> bit & 1))
            self.status_old = self.status
            self._dbg("Now CountDown:%d,%d,%d--%d" % (*self.countdown, int(self.shutdown_flag)))

    # ---- IPC ----
    def publish(self) -> None:
        if self.shm:
            try:
                cur = self.shm.read()
                self.data.nset, self.data.message = cur.nset, cur.message
                self.shm.write(self.data)
            except OSError:
                pass

    def poll_ipc(self) -> bool:
        """Начало цикла: 'shutdown' в почтовом ящике -> выход; nSet==1 -> перечитать конфиг."""
        if not self.shm:
            return True
        try:
            cur = self.shm.read()
        except OSError:
            return True
        if b"shutdown" in cur.message:
            return False
        if cur.nset == 1:
            old = self.cfg
            self.cfg = Config.load(self.cfg_path)
            self.data.nset, cur.nset = 0, 0
            self.shm.write(cur)
            if self.cfg.comm_mode != old.comm_mode or (
                    self.cfg.comm_mode == 0 and self.cfg.com_port != old.com_port):
                print("The communicate mode changed!The process restart..." if
                      self.cfg.comm_mode != old.comm_mode else
                      "The com port changed!The process restart...")
                self.reopen_requested = True
            self.query_ok = True
        return True

    reopen_requested = False

    # ---- один проход цикла Start() ----
    def step(self) -> bool:
        if not self._opened:
            self._opened = self.link.open()
            print("Initialize %s %s!" % ("Com" if isinstance(self.link, SerialLink) else "Usb",
                                         "Succeeded" if self._opened else "Failed"))
            if not self._opened:
                self.sleep(5)
                return not self._stop.is_set()
        if not self.poll_ipc() or self._stop.is_set():
            return False
        if self.reopen_requested or self.link.failed:
            if self.link.failed:
                self.sleep(5)
            self.link.close()
            self.reopen_requested = False
            self._opened = self.link.open()
            if not self._opened:
                return not self._stop.is_set()
            self.data.clear()
            self.query_ok = True

        self.counter += 1
        self.data.heartbeat = self.counter
        if not self.query_ok:
            self.fail_count += 1
            if self.fail_count >= RETRY_COUNT:
                if self.ups_ready:
                    self.ups_ready = False
                    self.data.clear()
                self.fail_count = 0
                self.send_message(3)
                self.sleep(NO_RESPONSE_SLEEP)
        else:
            self.ups_ready, self.fail_count = True, 0
        self.query_ok = True

        if self.counter % 10 == 1:                       # F и I - раз в 10 циклов
            ident = self.link.query_identity()
            if ident:
                self.data.company, self.data.model, self.data.version = (
                    ident.company, ident.model, ident.version)
            rate = self.link.query_rating()
            if rate:
                for attr, val in (("rate_ac_in", rate.ac_in), ("rate_load", rate.load),
                                  ("rate_dc", rate.dc), ("rate_freq", rate.freq)):
                    if val is not None:
                        setattr(self.data, attr, val)

        reading = self.link.query_status()
        if reading is None:
            self.query_ok = False
            return not self._stop.is_set()

        self.apply_reading(reading)
        self.publish()
        self.handle_status_change()
        if self.status != 0:
            self.process_countdown()
            if self.shutdown_flag:
                if self.cfg.turn_off_ups:
                    self.link.shutdown_ups(self.cfg.turn_off_ups_time)
                print("exec shutdown.sh /n")
                self._script("shutdown.sh")              # второй вызов, без аргументов
                self.shutdown_flag = False
                self.status = 0
        self.sleep(1)
        return not self._stop.is_set()

    # ---- основной цикл + PID-файл ----
    def run(self) -> int:
        pid_path = os.path.join(self.workdir, "Monitor.pid")
        if self.find_running(pid_path):
            print("%s is Running in System Now!" % self.proc_name)
            return 1
        print("=========================%s start...============================" % self.proc_name)
        try:
            while self.step():
                pass
        finally:
            self.link.close()
            if self.shm:
                self.shm.delete()
            with open(pid_path, "w") as f:
                f.write("0")
        return 0

    @staticmethod
    def find_running(pid_path: str) -> bool:
        """FindProIsRun(): живой PID из Monitor.pid (и не наш) -> уже запущен."""
        try:
            with open(pid_path) as f:
                pid = int(f.read().strip() or 0)
            if pid > 0 and pid != os.getpid():
                os.kill(pid, 0)
                return True
        except (OSError, ValueError):
            pass
        with open(pid_path, "w") as f:
            f.write(str(os.getpid()))
        return False


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def make_link(cfg: Config, device: Optional[str], baud: int) -> UpsLink:
    if cfg.comm_mode == 1:
        return HidTextLink(baud=baud)
    if cfg.comm_mode == 2:
        return HidBinaryLink(baud=baud)
    return SerialLink(device or f"/dev/ttyS{cfg.com_port - 1}", baud)


class FakeClock:
    def __init__(self, t0=1_700_000_000.0): self.t = t0
    def time(self): return self.t
    def sleep(self, s): self.t += s


def cmd_simulate(a) -> int:
    """Демо: виртуальный ИБП, сбой сети на ~25 «секунд» (время ускорено)."""
    sim = MegatecSimulator().start()
    tmp = tempfile.mkdtemp(prefix="upsdemo_")
    cfg = Config(ac_fail_delay=60, warning_interval=10)
    clk = FakeClock()
    mon = Monitor(SerialLink(sim.slave_name, timeout=0.5), cfg, workdir=tmp, debug=True,
                  dry_run=True, clock=clk.time, sleep=clk.sleep)
    print(f"[sim] виртуальный ИБП: {sim.slave_name}, каталог журнала: {tmp}")
    for n in range(75):
        if n == 5:
            print("[sim] --- пропало питание ---"); sim.ac_fail(True)
        if n == 40:
            print("[sim] --- питание вернулось ---"); sim.ac_fail(False)
        mon.step()
    sim.stop()
    mon.link.close()
    for fn in os.listdir(tmp):
        if fn.startswith("PowerEvent"):
            print(f"\n--- {fn} ---\n" + open(os.path.join(tmp, fn)).read())
    return 0


def cmd_decode(a) -> int:
    for text in a.frames:
        raw = text.encode().replace(b"\\r", b"\r")
        if not raw.endswith(b"\r"):
            raw += b"\r"
        for fn in (parse_q1, parse_identity, parse_rating):
            r = fn(raw)
            if r:
                print(fn.__name__, "->", r)
                if isinstance(r, StatusReading):
                    names = ["AC_FAIL", "LOW_BATT", "BYPASS", "UPS_FAIL", "STANDBY", "TEST", "SHUTDOWN", "BEEPER"]
                    print("   биты:", [n for i, n in enumerate(names) if r.status >> (7 - i) & 1])
                break
        else:
            print("не распознано:", raw)
    return 0


def cmd_config(a) -> int:
    path = os.path.join(a.workdir, "TimeCfg.conf")
    cfg = Config.load(path) if os.path.exists(path) else Config()
    for opt in ("ac_fail_delay", "battery_low_delay", "ups_failed_delay", "warning_interval",
                "turn_off_ups", "turn_off_ups_time", "com_port", "comm_mode"):
        v = getattr(a, opt, None)
        if v is not None:
            setattr(cfg, opt, v)
    if a.ups_ip is not None:
        cfg.ups_ip = a.ups_ip
    if a.write:
        cfg.save(path)
        print("записано", path)
    print(cfg)
    return 0


def cmd_stop(a) -> int:
    pid_path = os.path.join(a.workdir, "Monitor.pid")
    try:
        pid = int(open(pid_path).read().strip() or 0)
        if pid > 0:
            os.kill(pid, signal.SIGTERM)
            print("SIGTERM ->", pid)
            return 0
    except (OSError, ValueError):
        pass
    print("процесс не найден")
    return 1


def cmd_run(a) -> int:
    os.makedirs(a.workdir, exist_ok=True)
    cfg = Config.load(os.path.join(a.workdir, "TimeCfg.conf"))
    link = make_link(cfg, a.device, a.baud)
    shm = None
    if a.shm:
        try:
            shm = SysVShm()
        except OSError as e:
            print("shm недоступна:", e)
    mon = Monitor(link, cfg, workdir=a.workdir, debug=a.debug, dry_run=a.dry_run, shm=shm)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: mon.stop())
    return mon.run()


def cmd_selftest(a) -> int:
    import unittest
    return 0 if unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(SelfTest)).wasSuccessful() else 1


class SelfTest(__import__("unittest").TestCase):
    def test_parse_q1(self):
        r = parse_q1(b"(230.0 140.0 230.0 020 50.0 2.27 25.0 10001001\r")
        self.assertEqual((r.ac_in, r.ac_out, r.load, r.freq, r.dc, r.temp),
                         (230.0, 230.0, 20.0, 50.0, 2.27, 25.0))
        self.assertEqual(r.status, 0b10001001)
        self.assertIsNone(parse_q1(b"(230.0 140.0\r"))
        # значения вне диапазона отбрасываются (как проверки с константами 450/150/65/250/-20/100)
        r = parse_q1(b"(999.0 140.0 999.0 999 99.9 9.99 99.9 00000000\r")
        self.assertIsNone(r.ac_in); self.assertIsNone(r.load); self.assertIsNone(r.freq)

    def test_parse_identity_rating(self):
        i = parse_identity(b"#SIMCORP         SIM-1000   V1.0      \r")
        self.assertEqual((i.company, i.model, i.version), ("SIMCORP", "SIM-1000", "V1.0"))
        r = parse_rating(b"#230.0 004 12.00 50.0\r")
        self.assertEqual((r.ac_in, r.load, r.dc, r.freq), (230.0, 4.0, 12.0, 50.0))

    def test_config_roundtrip(self):
        c = Config(ac_fail_delay=-1, ups_ip="10.0.0.5", comm_mode=2)
        raw = c.pack()
        self.assertEqual(len(raw), 52)
        self.assertEqual(struct.unpack("<I", raw[:4])[0], 0x55AA0335)
        self.assertEqual(Config.unpack(raw), c)

    def test_shm_struct(self):
        d = UpsData(heartbeat=7, ac_in=230.5, model="X", warn_status=0x80, message=b"shutdown")
        raw = d.pack()
        self.assertEqual(len(raw), 224)
        self.assertEqual(struct.unpack_from("<f", raw, 128)[0], 230.5)     # fAcInValue @0x80
        self.assertEqual(struct.unpack_from("<i", raw, 152)[0], 0x80)      # nWarnStatus @0x98
        self.assertEqual(raw[190:191], b"X")                               # cUpsModel @0xbe
        self.assertEqual(UpsData.unpack(raw).model, "X")

    def test_bcd(self):
        self.assertEqual(bcd(0x23), 23)
        self.assertAlmostEqual(bcd(0x23) * 10 + bcd(0x05) / 10.0, 230.5)

    def test_sysv_shm(self):
        try:
            shm = SysVShm(key=0x1B57)
        except OSError:
            self.skipTest("SysV IPC недоступен")
        try:
            shm.write(UpsData(heartbeat=42, ac_in=231.0))
            self.assertEqual(shm.read().heartbeat, 42)
        finally:
            shm.delete()

    def _mon(self, **kw):
        sim = MegatecSimulator().start()
        self.addCleanup(sim.stop)
        tmp = tempfile.mkdtemp()
        clk = FakeClock()
        cfg = Config(**kw)
        mon = Monitor(SerialLink(sim.slave_name, timeout=0.3), cfg, workdir=tmp,
                      dry_run=True, clock=clk.time, sleep=clk.sleep)
        self.addCleanup(mon.link.close)
        return sim, mon, tmp

    def _msgs(self, mon): return [a[0] for s, a in mon.executed if s == "SendMSG.sh"]

    def test_full_ac_fail_shutdown(self):
        sim, mon, tmp = self._mon(ac_fail_delay=30, warning_interval=10,
                                  turn_off_ups=1, turn_off_ups_time=2)
        for _ in range(3):
            mon.step()
        self.assertEqual(mon.data.model, "SIM-1000")
        self.assertEqual(mon.data.rate_ac_in, 230.0)
        self.assertEqual(mon.data.ac_in, 230.0)
        sim.ac_fail(True)
        for _ in range(60):
            mon.step()
        msgs = self._msgs(mon)
        self.assertIn("Received By ups_manager  -->  AC Fail", msgs)
        self.assertIn("AC FAIL:System Shutdown after 20 Second", msgs)
        self.assertTrue(any(s == "shutdown.sh" for s, _ in mon.executed))
        self.assertIn(b"S02", sim.commands)
        logs = [f for f in os.listdir(tmp) if f.startswith("PowerEvent")]
        self.assertEqual(len(logs), 1)
        self.assertIn("AC Fail", open(os.path.join(tmp, logs[0])).read())

    def test_cancel(self):
        sim, mon, _ = self._mon(ac_fail_delay=300, warning_interval=10)
        mon.step(); sim.ac_fail(True)
        for _ in range(15):
            mon.step()
        self.assertLess(mon.countdown[0], 300)
        sim.ac_fail(False)
        for _ in range(3):
            mon.step()
        self.assertEqual(mon.countdown[0], INT_MAX)
        self.assertIn("Received By ups_manager  -->  AC NORMAL,Warnning Canceled", self._msgs(mon))
        self.assertFalse(any(s == "shutdown.sh" for s, _ in mon.executed))

    def test_no_response(self):
        sim, mon, _ = self._mon()
        mon.step()
        sim.muted = True
        for _ in range(8):
            mon.step()
        self.assertIn("UPS Adapter No Response, please stop the process!!", self._msgs(mon))
        self.assertEqual(mon.data.ac_in, 0.0)               # ClrAllData()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("-w", "--workdir", default=".", help="каталог TimeCfg.conf/журналов/pid")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="запустить мониторинг (foreground)")
    r.add_argument("--device", help="COM-устройство (по умолчанию /dev/ttyS<порт-1>)")
    r.add_argument("--baud", type=int, default=2400)
    r.add_argument("--debug", action="store_true")
    r.add_argument("--dry-run", action="store_true", help="не запускать ./SendMSG.sh и ./shutdown.sh")
    r.add_argument("--shm", action="store_true", help="публиковать в SysV shm 0x1B56")
    r.set_defaults(fn=cmd_run)
    sub.add_parser("stop", help="послать SIGTERM процессу из Monitor.pid").set_defaults(fn=cmd_stop)
    sub.add_parser("simulate", help="демо на виртуальном ИБП").set_defaults(fn=cmd_simulate)
    sub.add_parser("selftest", help="автотесты").set_defaults(fn=cmd_selftest)
    d = sub.add_parser("decode", help="разобрать кадр(ы) Megatec")
    d.add_argument("frames", nargs="+")
    d.set_defaults(fn=cmd_decode)
    c = sub.add_parser("config", help="показать/изменить TimeCfg.conf")
    for name in ("ac_fail_delay", "battery_low_delay", "ups_failed_delay", "warning_interval",
                 "turn_off_ups", "turn_off_ups_time", "com_port", "comm_mode"):
        c.add_argument("--" + name.replace("_", "-"), dest=name, type=int)
    c.add_argument("--ups-ip")
    c.add_argument("--write", action="store_true")
    c.set_defaults(fn=cmd_config)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

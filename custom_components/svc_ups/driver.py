"""Драйвер USB-HID моста SVC / Richcomm Technologies (VID 0925, PID 1234).

Мост эмулирует UART ИБП поверх HID (протокол восстановлен из бинарника ups_monitor):

* исходящее: текст команды режется на куски по <=3 байта, каждый уходит как PDU
  ``[0xA0 | n, d0, d1, d2]`` (четыре ``HIDIOCSUSAGE`` + ``HIDIOCSREPORT``, report_type=OUTPUT);
* скорость мостового UART: PDU ``[0x80, 500000 // baud, 0, 0]``;
* входящее: ``read()`` отдаёт события ``hiddev_event {hid, value}``; один HID-отчёт =
  6 событий с usage 3..8 -> байты отчёта 0..5; если ``report[0] & 0x80`` - это UART-кадр:
  ``report[0] & 7`` (не более 5) байт данных в ``report[1:]``.

Поверх идёт обычный Megatec: ``Q1`` / ``I`` / ``F`` / ``Q`` (toggle бипера) / ``T`` / ``CT``.

Модуль не зависит от Home Assistant (тестируется отдельно).
"""

from __future__ import annotations

import errno
import fcntl
import glob
import logging
import os
import re
import select
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

_LOGGER = logging.getLogger(__name__)

VID = 0x0925
PID = 0x1234
DEFAULT_BAUD = 2400
REPORT_LEN = 6  # usage 3..8 -> байты отчёта 0..5

# Linux hiddev ioctl (значения подтверждены дизассемблером ups_monitor)
HIDIOCGDEVINFO = 0x801C4803
HIDIOCGNAME64 = 0x80404806
HIDIOCGPHYS64 = 0x80404812
HIDIOCSUSAGE = 0x4018480C
HIDIOCSREPORT = 0x400C4808
HID_REPORT_TYPE_OUTPUT = 2  # (1=input, 2=output, 3=feature)

_USAGE_REF = struct.Struct("<IIIIIi")  # type, id, field_idx, usage_idx, code, value
_REPORT_INFO = struct.Struct("<III")  # type, id, num_fields
_EVENT = struct.Struct("<Ii")  # hid, value
_NODE_RE = re.compile(r"hiddev(\d+)$")


class St:
    """Биты байта статуса из ответа Q1 (MSB первым)."""

    AC_FAIL = 0x80  # b7 Utility Fail (работа от батареи)
    LOW_BATT = 0x40  # b6 Battery Low
    BYPASS = 0x20  # b5 Bypass / Boost / Buck активен
    UPS_FAIL = 0x10  # b4 UPS Failed
    STANDBY = 0x08  # b3 тип: 1 = standby/line-interactive, 0 = online
    TEST = 0x04  # b2 идёт тест
    SHUTDOWN = 0x02  # b1 shutdown активен
    BEEPER = 0x01  # b0 бипер включён


class UpsError(Exception):
    """Базовая ошибка драйвера."""


class UpsCommandError(UpsError):
    """Команда не выполнена или не подтверждена ИБП."""


class DeviceMismatch(UpsError):
    """Узел hiddev принадлежит не мосту Richcomm (VID/PID не совпали)."""


@dataclass(frozen=True)
class Identity:
    company: str = "SVC"
    model: str = "Richcomm UPS"
    version: str = ""


@dataclass(frozen=True)
class Rating:
    """Ответ F: номинальные параметры (``#MMM.M QQQ SS.SS FF.F``)."""

    voltage: Optional[float] = None
    current: Optional[float] = None
    battery_voltage: Optional[float] = None
    frequency: Optional[float] = None


@dataclass(frozen=True)
class StatusReading:
    ac_in: Optional[float] = None
    ac_out: Optional[float] = None
    load: Optional[float] = None
    freq: Optional[float] = None
    dc: Optional[float] = None
    temp: Optional[float] = None
    status: int = 0
    raw_response: str = ""


@dataclass(frozen=True)
class FrameSpec:
    header: int
    min_len: int  # полная длина кадра: заголовок + тело + CR
    max_len: int


# Эталонные длины из ups_monitor: Q1 = 1+45+CR, I = 1+37+CR, F = 1+20+CR.
Q1_SPEC = FrameSpec(ord("("), 40, 60)
Q1_STD_LEN = 47  # '(' + 45 символов + CR; оригинал требовал ровно столько
I_SPEC = FrameSpec(ord("#"), 30, 48)
F_SPEC = FrameSpec(ord("#"), 17, 28)


# --------------------------------------------------------------------------- #
# Разбор ответов (границы значений - из .rodata оригинала: lo < v < hi)
# --------------------------------------------------------------------------- #
def _chk(tok: str, lo: float, hi: float) -> Optional[float]:
    try:
        v = float(tok)
    except ValueError:
        return None
    return v if lo < v < hi else None


def _body(frame: bytes, header: bytes) -> Optional[str]:
    start = frame.find(header)
    if start < 0:
        return None
    end = frame.find(b"\r", start)
    chunk = frame[start + 1 : end] if end >= 0 else frame[start + 1 :]
    return chunk.decode("ascii", errors="replace")


def parse_q1(frame: bytes) -> Optional[StatusReading]:
    """``(MMM.M NNN.N PPP.P QQQ RR.R S.SS TT.T b7..b0`` + CR."""
    body = _body(frame, b"(")
    if body is None:
        return None
    tok = body.split()
    if len(tok) != 8 or len(tok[7]) != 8 or set(tok[7]) - {"0", "1"}:
        return None
    return StatusReading(
        ac_in=_chk(tok[0], -1, 450),
        ac_out=_chk(tok[2], -1, 450),  # tok[1] (fault voltage) игнорируется, как в оригинале
        load=_chk(tok[3], -1, 150),
        freq=_chk(tok[4], 0, 65),
        dc=_chk(tok[5], -1, 250),
        temp=_chk(tok[6], -20, 100),
        status=int(tok[7], 2),
        raw_response=body.strip(),
    )


def parse_identity(frame: bytes) -> Optional[Identity]:
    """``#Company_Name_ UPS_Model_ Version___`` + CR (поля 15/10/10 символов)."""
    body = _body(frame, b"#")
    if body is None:
        return None
    # Фиксированные смещения проверяются ДО strip(): хвост поля версии - пробелы.
    if len(body) == 37 and body[15] == " " and body[26] == " ":
        parts = (body[0:15], body[16:26], body[27:37])
    else:
        tok = body.split()
        if len(tok) >= 3:
            parts = (" ".join(tok[:-2]), tok[-2], tok[-1])
        elif len(tok) == 2:
            parts = (tok[0], tok[1], "")
        else:
            return None
    company, model, version = (p.strip() for p in parts)
    default = Identity()
    return Identity(company or default.company, model or default.model, version)


def parse_rating(frame: bytes) -> Optional[Rating]:
    """``#MMM.M QQQ SS.SS FF.F`` + CR."""
    body = _body(frame, b"#")
    if body is None:
        return None
    tok = body.split()
    if len(tok) != 4:
        return None
    return Rating(
        voltage=_chk(tok[0], 0, 450),
        current=_chk(tok[1], -1, 150),
        battery_voltage=_chk(tok[2], -1, 250),
        frequency=_chk(tok[3], 0, 65),
    )


# --------------------------------------------------------------------------- #
# Транспорт HID
# --------------------------------------------------------------------------- #
class HidTransport:
    """Минимальный интерфейс; реальная реализация - hiddev, в тестах - эмулятор."""

    path: str = ""
    phys: str = ""

    def write_pdu(self, pdu: bytes) -> None:  # pragma: no cover - интерфейс
        raise NotImplementedError

    def read_events(self, timeout: float) -> bytes:  # pragma: no cover - интерфейс
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - интерфейс
        raise NotImplementedError


def _ioctl_str(fd: int, request: int) -> str:
    buf = bytearray(64)
    try:
        fcntl.ioctl(fd, request, buf, True)
    except OSError:
        return ""
    return bytes(buf).split(b"\0", 1)[0].decode("utf-8", "replace")


class HiddevTransport(HidTransport):
    def __init__(self, path: str) -> None:
        self.path = path
        self._fd = os.open(path, os.O_RDWR)
        try:
            info = bytearray(28)
            fcntl.ioctl(self._fd, HIDIOCGDEVINFO, info, True)
            vid, pid = struct.unpack_from("<HH", info, 16)
            if (vid, pid) != (VID, PID):
                raise DeviceMismatch(f"{path}: VID/PID {vid:04x}:{pid:04x}, ожидалось {VID:04x}:{PID:04x}")
            self.phys = _ioctl_str(self._fd, HIDIOCGPHYS64)
        except BaseException:
            os.close(self._fd)
            raise

    def write_pdu(self, pdu: bytes) -> None:
        padded = (list(pdu) + [0, 0, 0, 0])[:4]
        for i in range(4):
            val = padded[i] - 256 if padded[i] > 127 else padded[i]
            fcntl.ioctl(
                self._fd,
                HIDIOCSUSAGE,
                _USAGE_REF.pack(HID_REPORT_TYPE_OUTPUT, 0, (i >> 1) & 1, i & 1, 0, val),
            )
        fcntl.ioctl(self._fd, HIDIOCSREPORT, _REPORT_INFO.pack(HID_REPORT_TYPE_OUTPUT, 0, 0))

    def read_events(self, timeout: float) -> bytes:
        ready, _, _ = select.select([self._fd], [], [], timeout)
        if not ready:
            return b""
        data = os.read(self._fd, 512)
        if not data:
            raise OSError(errno.ENODEV, "hiddev: EOF")
        return data

    def close(self) -> None:
        os.close(self._fd)


def list_nodes() -> list[str]:
    return sorted(set(glob.glob("/dev/usb/hiddev*") + glob.glob("/dev/hiddev*")))


def _ensure_node(path: Optional[str]) -> None:
    """Best effort: создать узел как это делал оригинал (major 180, minor 96+N)."""
    if not path or os.path.exists(path):
        return
    match = _NODE_RE.search(path)
    if not match:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.mknod(path, 0o20666, os.makedev(180, 96 + int(match.group(1))))
    except OSError:
        pass


def probe_node(path: str) -> Optional[str]:
    """phys-строка, если узел - наш мост (VID/PID совпали); иначе None."""
    try:
        transport = HiddevTransport(path)
    except (OSError, UpsError):
        return None
    try:
        return transport.phys
    finally:
        transport.close()


@dataclass(frozen=True)
class Candidate:
    path: str
    phys: str


def discover() -> list[Candidate]:
    found = []
    for path in list_nodes():
        phys = probe_node(path)
        if phys is not None:
            found.append(Candidate(path, phys))
    return found


def resolve_device(preferred: Optional[str], phys: Optional[str] = None) -> Optional[str]:
    """Актуальный путь к мосту. hiddevN переименовываются при перезагрузке/смене порта,
    поэтому сначала пробуем сохранённый путь, затем ищем по phys, затем единственный мост."""
    _ensure_node(preferred)
    if preferred:
        got = probe_node(preferred)
        if got is not None and (not phys or not got or got == phys):
            return preferred
    candidates = discover()
    if phys:
        for cand in candidates:
            if cand.phys == phys:
                return cand.path
    if len(candidates) == 1:
        return candidates[0].path
    return None


# --------------------------------------------------------------------------- #
# Драйвер
# --------------------------------------------------------------------------- #
class SvcUpsHardware:
    """Потокобезопасный драйвер. Все публичные методы блокирующие (executor)."""

    MAX_CONSECUTIVE_FAILS = 3  # после стольких пустых опросов подряд мост переоткрывается

    def __init__(
        self,
        dev_path: Optional[str] = None,
        baud: int = DEFAULT_BAUD,
        phys: Optional[str] = None,
        transport_factory: Optional[Callable[[], HidTransport]] = None,
        response_timeout: float = 1.5,
        beeper_settle: float = 5.0,
        beeper_poll: float = 0.5,
    ) -> None:
        self.dev_path = dev_path or "/dev/usb/hiddev0"
        self.baud = baud
        self.phys = phys or ""
        self.response_timeout = response_timeout
        self.beeper_settle = beeper_settle
        self.beeper_poll = beeper_poll
        self._factory = transport_factory or self._open_hiddev

        self._cmd_lock = threading.RLock()  # один диалог с ИБП за раз
        self._cond = threading.Condition()  # защищает _rx
        self._rx = bytearray()
        self._transport: Optional[HidTransport] = None
        self._reader: Optional[threading.Thread] = None
        self._stop: Optional[threading.Event] = None
        self._broken = False
        self._needs_settle = False
        self._consec_fail = 0
        # После первого кадра эталонной длины требуем её всегда: потеря/вставка одного символа
        # («230.0» -> «23.0») давала валидный на вид, но неверный кадр.
        self._q1_locked = False
        self.stats: dict = {
            "opens": 0,
            "status_ok": 0,
            "status_failed": 0,
            "bad_frames": 0,
            "timeouts": 0,
            "q_sent": 0,
            "last_error": "",
            "last_beeper": None,
        }

    # ----- открытие / закрытие -----
    def _open_hiddev(self) -> HidTransport:
        path = resolve_device(self.dev_path, self.phys)
        if path is None:
            raise UpsError(f"мост Richcomm ({VID:04x}:{PID:04x}) не найден; ожидался {self.dev_path}")
        if path != self.dev_path:
            _LOGGER.warning("Узел ИБП изменился: %s -> %s", self.dev_path, path)
            self.dev_path = path
        return HiddevTransport(path)

    def open(self) -> bool:
        with self._cmd_lock:
            if self._transport is not None and not self._broken:
                return True
            self._close_locked()
            try:
                transport = self._factory()
            except (OSError, UpsError) as err:
                self.stats["last_error"] = str(err)
                _LOGGER.error("Не удалось открыть ИБП (%s): %s", self.dev_path, err)
                return False
            self.stats["opens"] += 1
            self._transport = transport
            if transport.phys:
                self.phys = transport.phys
            self._stop = threading.Event()
            self._reader = threading.Thread(
                target=self._reader_loop,
                args=(transport, self._stop),
                daemon=True,
                name="svc_ups_reader",
            )
            self._reader.start()
            try:  # SetBaud: [0x80, 500000 // baud, 0, 0]
                transport.write_pdu(bytes([0x80, (500000 // self.baud) & 0xFF, 0, 0]))
            except OSError as err:
                self._mark_broken(err, transport)
                return False
            time.sleep(0.1)
            return True

    def close(self) -> None:
        with self._cmd_lock:
            self._close_locked()

    def _close_locked(self) -> None:
        stop, reader, transport = self._stop, self._reader, self._transport
        self._stop = self._reader = self._transport = None
        self._broken = False
        self._needs_settle = False
        if stop is not None:
            stop.set()
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=1.0)
        if transport is not None:
            try:
                transport.close()
            except OSError:
                pass
        with self._cond:
            self._rx.clear()

    def _mark_broken(self, err: BaseException, transport: Optional[HidTransport] = None) -> None:
        if transport is not None and transport is not self._transport:
            return  # ошибка устаревшего читателя
        self._broken = True
        self.stats["last_error"] = str(err)
        _LOGGER.warning("Ошибка USB-HID, мост будет переоткрыт: %s", err)
        with self._cond:
            self._cond.notify_all()

    # ----- приём -----
    def _reader_loop(self, transport: HidTransport, stop: threading.Event) -> None:
        """Собирает HID-отчёты из потока событий. В отличие от старой версии:
        несколько отчётов в одном read() не склеиваются, а неполный отчёт
        переживает границу read()."""
        rep = bytearray(REPORT_LEN)
        last = -1
        pending = False
        while not stop.is_set():
            try:
                data = transport.read_events(0.1)
            except OSError as err:
                if not stop.is_set():
                    self._mark_broken(err, transport)
                return
            if not data:
                if pending:  # отчёт короче 6 usage - выпускаем по тишине
                    self._on_report(bytes(rep))
                    rep, last, pending = bytearray(REPORT_LEN), -1, False
                continue
            for off in range(0, len(data) - _EVENT.size + 1, _EVENT.size):
                hid, val = _EVENT.unpack_from(data, off)
                idx = (hid & 0xFF) - 3
                if not 0 <= idx < REPORT_LEN:
                    continue
                if pending and idx <= last:  # начался следующий отчёт
                    self._on_report(bytes(rep))
                    rep, pending = bytearray(REPORT_LEN), False
                rep[idx] = val & 0xFF
                last, pending = idx, True
                if idx == REPORT_LEN - 1:
                    self._on_report(bytes(rep))
                    rep, last, pending = bytearray(REPORT_LEN), -1, False

    def _on_report(self, rep: bytes) -> None:
        header = rep[0]
        if header & 0x80:
            count = min(header & 7, 5)
            if count:
                with self._cond:
                    self._rx += rep[1 : 1 + count]
                    self._cond.notify_all()

    # ----- передача -----
    def _send(self, data: bytes) -> bool:
        transport = self._transport
        if transport is None:
            return False
        try:
            for pos in range(0, len(data), 3):
                chunk = data[pos : pos + 3]
                transport.write_pdu(bytes([0xA0 | len(chunk)]) + chunk.ljust(3, b"\0"))
                time.sleep(max(0.014, len(chunk) * 11.0 / self.baud))
        except OSError as err:
            self._mark_broken(err, transport)
            return False
        return True

    def _extract_frame(self, spec: FrameSpec) -> Optional[bytes]:
        """Достаёт первый корректный кадр из буфера. Вызывается под self._cond."""
        buf = self._rx
        while True:
            start = buf.find(bytes([spec.header]))
            if start < 0:
                buf.clear()
                return None
            if start:
                del buf[:start]
            end = buf.find(b"\r")
            if end < 0:
                if len(buf) > spec.max_len:  # заголовок без CR: мусор
                    del buf[:1]
                    continue
                return None
            frame = bytes(buf[: end + 1])
            del buf[: end + 1]
            if spec.min_len <= len(frame) <= spec.max_len:
                return frame
            self.stats["bad_frames"] += 1
            _LOGGER.debug("Отброшен кадр неверной длины %d: %r", len(frame), frame)

    def _exchange(self, cmd: bytes, spec: FrameSpec) -> Optional[bytes]:
        """Команда -> кадр. Вызывать под _cmd_lock."""
        if self._transport is None or self._broken:
            return None
        if self._needs_settle:
            # прошлый ответ не пришёл: даём запоздавшим байтам дойти, иначе они
            # будут приняты за ответ на ЭТОТ запрос (сдвиг ответов на один запрос).
            time.sleep(0.4)
            self._needs_settle = False
        with self._cond:
            self._rx.clear()
        if not self._send(cmd):
            return None
        deadline = time.monotonic() + self.response_timeout
        with self._cond:
            while not self._broken:
                frame = self._extract_frame(spec)
                if frame is not None:
                    return frame
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(min(remaining, 0.05))
        if not self._broken:
            self.stats["timeouts"] += 1
            self._needs_settle = True
        return None

    # ----- запросы -----
    def _read_status(self, attempts: int = 2) -> Optional[StatusReading]:
        if not self.open():
            return None
        for _ in range(attempts):
            frame = self._exchange(b"Q1\r", Q1_SPEC)
            if frame is not None and self._q1_locked and len(frame) != Q1_STD_LEN:
                self.stats["bad_frames"] += 1
                _LOGGER.debug("Q1: длина %d вместо %d, кадр отброшен: %r", len(frame), Q1_STD_LEN, frame)
                frame = None
            if frame is not None:
                reading = parse_q1(frame)
                if reading is not None:
                    if len(frame) == Q1_STD_LEN:
                        self._q1_locked = True
                    self._consec_fail = 0
                    self.stats["status_ok"] += 1
                    return reading
                self.stats["bad_frames"] += 1
                _LOGGER.debug("Не разобран кадр Q1: %r", frame)
            if self._broken:
                break
        self.stats["status_failed"] += 1
        self._consec_fail += 1
        if self._consec_fail >= self.MAX_CONSECUTIVE_FAILS:
            _LOGGER.warning("ИБП молчит %d опроса подряд - переоткрываю мост", self._consec_fail)
            self._broken = True
            self._consec_fail = 0
        return None

    def query_status(self, attempts: int = 2) -> Optional[StatusReading]:
        """Q1. Возвращает None, если ИБП не ответил (никаких выдуманных значений)."""
        with self._cmd_lock:
            return self._read_status(attempts)

    def _query(self, cmd: bytes, spec: FrameSpec, parser, attempts: int = 2):
        with self._cmd_lock:
            if not self.open():
                return None
            for _ in range(attempts):
                frame = self._exchange(cmd, spec)
                if frame is not None:
                    parsed = parser(frame)
                    if parsed is not None:
                        return parsed
                    self.stats["bad_frames"] += 1
                if self._broken:
                    break
            return None

    def query_identity(self) -> Optional[Identity]:
        """I: производитель / модель / версия прошивки."""
        return self._query(b"I\r", I_SPEC, parse_identity)

    def query_rating(self) -> Optional[Rating]:
        """F: номинальные напряжение / ток / напряжение батареи / частота."""
        return self._query(b"F\r", F_SPEC, parse_rating)

    # ----- управление -----
    def _poll_until(
        self, predicate: Callable[[StatusReading], bool], timeout: float
    ) -> tuple[Optional[StatusReading], bool]:
        deadline = time.monotonic() + timeout
        last: Optional[StatusReading] = None
        while True:
            time.sleep(self.beeper_poll)
            reading = self._read_status(1)
            if reading is not None:
                last = reading
                if predicate(reading):
                    return reading, True
            if time.monotonic() >= deadline:
                return last, False

    def set_beeper(self, target: bool) -> StatusReading:
        """Привести бипер к состоянию ``target``.

        Команда Q - это TOGGLE, а не set, поэтому она НИКОГДА не шлётся вслепую
        и никогда не повторяется: повтор Q при медленной реакции ИБП возвращал
        бипер обратно (отсюда «работает через раз»). Схема:
        1) прочитать статус (обязательно получить ответ, иначе отказ);
        2) если b0 уже равен цели - ничего не отправлять;
        3) отправить Q ровно один раз;
        4) опрашивать Q1 до ``beeper_settle`` секунд, пока b0 не станет целевым.
        """
        with self._cmd_lock:
            current = self._read_status(3)
            if current is None:
                raise UpsCommandError("ИБП не ответил на запрос статуса, команда не отправлена")
            if bool(current.status & St.BEEPER) == target:
                return current
            if not self._send(b"Q\r"):
                raise UpsCommandError("не удалось отправить команду Q")
            self.stats["q_sent"] += 1
            started = time.monotonic()
            reading, ok = self._poll_until(lambda r: bool(r.status & St.BEEPER) == target, self.beeper_settle)
            self.stats["last_beeper"] = {
                "target": target,
                "confirmed": ok,
                "seconds": round(time.monotonic() - started, 2),
                "raw": reading.raw_response if reading else None,
            }
            if ok and reading is not None:
                return reading
            raise UpsCommandError(
                f"ИБП не подтвердил смену бипера за {self.beeper_settle:.0f} с (команда Q отправлена один раз, "
                "повтор не выполнялся, чтобы не вернуть прежнее состояние)"
            )

    def toggle_beeper(self) -> StatusReading:
        """Инверсия бипера; чтение и запись под одной блокировкой."""
        with self._cmd_lock:
            current = self._read_status(3)
            if current is None:
                raise UpsCommandError("ИБП не ответил на запрос статуса, команда не отправлена")
            return self.set_beeper(not bool(current.status & St.BEEPER))

    def start_self_test(self) -> Optional[StatusReading]:
        """T: тест батареи ~10 с. Возвращает статус, в котором виден бит теста (если успел)."""
        with self._cmd_lock:
            if self._read_status(2) is None:
                raise UpsCommandError("ИБП не отвечает, тест не запущен")
            if not self._send(b"T\r"):
                raise UpsCommandError("не удалось отправить команду T")
            reading, _ = self._poll_until(lambda r: bool(r.status & St.TEST), 3.0)
            return reading

    def cancel_self_test(self) -> Optional[StatusReading]:
        """CT: отмена теста."""
        with self._cmd_lock:
            if self._read_status(2) is None:
                raise UpsCommandError("ИБП не отвечает, отмена не отправлена")
            if not self._send(b"CT\r"):
                raise UpsCommandError("не удалось отправить команду CT")
            reading, _ = self._poll_until(lambda r: not r.status & St.TEST, 3.0)
            return reading

    def diagnostics(self) -> dict:
        return {**self.stats, "path": self.dev_path, "phys": self.phys, "baud": self.baud, "broken": self._broken}

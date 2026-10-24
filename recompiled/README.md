# Реверс-инжиниринг бинарника `ups_monitor`

**Объект:** `ups_monitor` — ELF32 i386, демон мониторинга ИБП (протокол Megatec/Q1, COM и USB-HID).
**Результат:** разбор в этом файле + рабочая реализация на Python — `ups_monitor.py`.

**Что и как проверено.** Разбор статический: `readelf`, `objdump -d -M intel`, `strings`, DWARF
(`readelf --debug-dump=info`). Бинарник не stripped: имена функций, глобалов и структур сохранились.
Сам бинарник **не запускался** (нет ИБП, 32-битное окружение Red Hat 8/9). Python-версия проверена
автотестами (9 штук) на виртуальном ИБП через pty. USB-HID часть написана по дизассемблеру и
**на железе не проверялась**.

---

## 1. Паспорт бинарника

| Параметр | Значение |
|---|---|
| Формат | ELF 32-bit LSB, Intel 80386, EXEC (не PIE), dynamic, not stripped, есть `.debug_info` |
| Размер / entry | 122 072 байт, точка входа `0x8048dd0` |
| Компилятор / libc | GCC 3.2.2 20030222 (Red Hat Linux 3.2.2-5), glibc 2.3.2-20030313 |
| Зависимости | `libpthread.so.0`, `libc.so.6`, интерпретатор `/lib/ld-linux.so.2` |
| Секции | `.text` 0x8048dd0 (0x3a80), `.rodata` 0x804c880, `.data` 0x804e694, `.bss` 0x804e8e0 (0x53d0) |
| Модули (DWARF) | `ramcom.c`, `ups_monitor.c`, `shm_sem.c`, `ups_comm.c`, `configfile.c`, `pm_pub.c`, `init.c` |
| Ключевые API | termios (COM), `ioctl` на `/dev/usb/hiddevN`, SysV `shmget/shmat/semop`, `pthread_create`, `system()` |

## 2. Карта функций

Модули сопоставлены по порядку символов и DWARF.

| Модуль | Адреса | Функции |
|---|---|---|
| `ramcom.c` | 0x8048e80–0x8049254 | `ComClose` 8048e80, `InitializeCom` 8048eae, `SendStr` 804903a, `GetStr` 804907d, **`main`** 80490a0 |
| `shm_sem.c` | 0x8049254–0x80495a0 | `LockSem` 8049254, `UnlkSem` 80492b7, `create_shm` 804931a, `delete_shm` 804937d, `write_shm` 8049433, `read_shm` 80494e8 |
| `ups_monitor.c` | 0x80495a0–0x804b86c | `SetStatus` 80495a0, `ClrAllData` 80495ff, `AddLog` 80496cd, `ShutdownUPS` 80497c4, `usberr_occur` 804984a, `open_dev` 8049877, `close_dev` 80498dc, `printf_initmsg` 804991f, **`Start`** 8049966 (главный цикл), `Quary_Q1` 8049e71, `Quary_F` 804a809, `Quary_I` 804ab56, `InitCom` 804b0c8, `InitUsb` 804b161, `SendMessage` 804b191, `Shutdown` 804b234, `ProcessCountDown` 804b2ea, `Process_UPS_warging` 804b41b, `Process_UPS_Cancel_warging` 804b5e8, `FindProIsRun` 804b7bf |
| `configfile.c` | 0x804b86c–0x804b94c | `SetDefaultValue` 804b86c, `ReadConfiguration` 804b8c8 |
| `pm_pub.c` | 0x804b94c–0x804bad0 | `InitPMCfg` 804b94c, `GetPMProcessName` 804bac4 |
| `ups_comm.c` (USB-HID) | 0x804bad0–0x804c790 | `LockSem/UnlockSem/DeleteSem` (замок кольцевых буферов, код не разбирался детально), `onUartDataReady` 804bcd7, `onUsbDataReady` 804bd5c, `UsbSendPDU` 804be0f, `USBReadThread` 804bef4, `SetBaud` 804c239, `UsbOpen` 804c287, `ReadData` 804c40f, `GetData` 804c4c9, `WriteData` 804c583, `SendData` 804c61a, `UsbExit` 804c723 |

Обработчики запросов существуют в трёх вариантах; диспетчеры `Quary_Q1/F/I` делают `switch(comm_mode)`:

| Суффикс | Режим | Транспорт |
|---|---|---|
| без суффикса | `comm_mode = 0` | COM-порт, текстовый Megatec |
| `…1` | `comm_mode = 1` | USB-HID, тот же текст, упакованный в HID-кадры |
| `…2` | `comm_mode = 2` | USB-HID, бинарные отчёты с BCD-числами |

**Странность именования:** `Quary_F` отправляет команду `I` (идентификация), а `Quary_I` — команду `F`
(номинальные параметры). Имена перепутаны.

### Глобальные переменные (`nm`)

| Адрес | Имя | Начальное значение / смысл |
|---|---|---|
| 0x804e6a0 | `m_nStartType` | 1; аргументы `start`/`debug` ставят 0, что включает отладочный вывод |
| 0x804e6b8 | `m_nComFd` | fd COM-порта (-1) |
| 0x804e6bc / bd / be | `UPS_status` / `_old` / `_tmp` | байт статуса, предыдущий, служебный |
| 0x804e6c0 | `bQuarySuccess` | 1 = последний опрос успешен |
| 0x804e6c4 | `iShutDownCountDown[3]` | {INT_MAX ×3}: AC / LOW CELL / UPS FAIL |
| 0x804e6d0 | `debug` | `(m_nStartType == 0)` |
| 0x804e6d4 | `bShutdownUps` | флаг «идёт выключение» |
| 0x804e6d8 / dc | `dev_opened` / `dev_err` | состояние устройства |
| 0x804e6e4 / e8 | `gs_nBaud` / `gs_nUsbFd` | 2400 / fd hiddev |
| 0x804e940 | `m_UpsReady` | ИБП отвечает |
| 0x804e944 | `m_UPS_DATA` | указатель на рабочую `UPS_DATA_Status` |
| 0x804e960 | `UPS_Data_tmp` | копия shm, читаемая в начале цикла |
| 0x804ea40 | `m_RetryCount` | 5 |
| 0x804ea44 / 48 | `m_ComBaud` / `m_ComPort` | 2400 / `/dev/ttyS0` |
| 0x804ea54 | `m_RemoteCtrlReady` | сбрасывается в `SetStatus`, больше нигде не взводится (мёртвая ветка) |
| 0x804ea64 | `oldwarntime` | время последнего уведомления |
| 0x804ea80 | `gs_PMCfg` | 256 Б, имя процесса (`ups_manager`) |
| 0x804eb80 / 0x804fba0 | кольцевые буферы | UART-RX 4096 байт; USB-RX 16384 слов (int на байт) |
| 0x8053bc0 | `UPS_Data_Temp` | буфер команды в `main()` |
| 0x8053ca4 / a8 / ac | `semid` / `pMesg` / `shmid` | IPC-дескрипторы |

---

## 3. Структуры данных

### `UPS_DATA_Status` — 224 байта (shared memory)

| Смещение | Тип | Поле | Назначение |
|---|---|---|---|
| 0x00 | int | `nMesgLen` | **фактически счётчик циклов (heartbeat)**: `Start` пишет сюда номер итерации |
| 0x04 | char[120] | `cMesgTxt` | почтовый ящик команд: `shutdown` / `start` |
| 0x7c | int | `nSet` | 1 = GUI просит перечитать `TimeCfg.conf` |
| 0x80 | float | `fAcInValue` | входное напряжение |
| 0x84 | float | `fAcOutValue` | выходное напряжение |
| 0x88 | float | `fAcFreValue` | частота |
| 0x8c | float | `fDcValue` | напряжение батареи |
| 0x90 | float | `fUpsTemp` | температура |
| 0x94 | float | `fLoad` | нагрузка, % |
| 0x98 | int | `nWarnStatus` | байт статуса |
| 0x9c | float | `fRate_AcIn` | номинальное входное напряжение (команда F) |
| 0xa0 | float | `fRate_DcValue` | номинальное напряжение батареи |
| 0xa4 | float | `fRate_AcFre` | номинальная частота |
| 0xa8 | float | `fRate_Load` | номинальный ток |
| 0xac | char[18] | `cCompanyName` | команда I |
| 0xbe | char[16] | `cUpsModel` | |
| 0xce | char[18] | `cVersion` | |

Python-формат: `struct.Struct('<i120si6fi4f18s16s18s')` — ровно 224 байта.

### `TheCfgData` — файл `./TimeCfg.conf`, 52 байта, little-endian

| Смещение | Поле | По умолчанию | Смысл |
|---|---|---|---|
| 0x00 | `nIdentifyHead` | `0x55AA0335` | сигнатура |
| 0x04 | `nTimeAcFailDelay` | 300 | сек до выключения при отказе сети (-1 = отключено) |
| 0x08 | `nBatteryLowDelay` | 60 | при низком заряде (-1 = отключено) |
| 0x0c | `nUPSFailedDelay` | 180 | при отказе ИБП (-1 = отключено) |
| 0x10 | `nWarningInterval` | 10 | период уведомлений обратного отсчёта, сек |
| 0x14 | `cUpsIPAddress[16]` | `192.168.0.100` | IP SNMP-адаптера; **в коде не используется** |
| 0x24 | `nTurnOffUPS` | 0 | 1 = после shutdown выключить сам ИБП |
| 0x28 | `nTurnOffUPSTime` | 2 | параметр *n* команды `S<n>` |
| 0x2c | `nComPort` | 1 | номер COM (1 → `/dev/ttyS0`) |
| 0x30 | `nCommMode` | 0 | 0 = COM, 1 = USB-текст, 2 = USB-бинарный |

`ReadConfiguration()`: файла нет или сигнатура не совпала → `SetDefaultValue()`.
Структура `_com_comm_procotol` (96 Б: буферы отправки/приёма, длины) определена в `ramcom.c`, но не
используется. `_program_cfg` (256 Б) хранит `szProgramName`.

## 4. Внешние интерфейсы

| Что | Как используется |
|---|---|
| `./TimeCfg.conf` | бинарная конфигурация, перечитывается при `nSet == 1` |
| `./pm.conf` | текст; строка `ProgramName:ups_manager`, `#` — комментарий; имя подставляется в `Received By %s` |
| `./Monitor.pid` | PID демона; `FindProIsRun()` проверяет `kill(pid, 0)`; при выходе пишется `0` |
| `./PowerEventГГГГММ.log` | журнал: `ctime(без перевода строки): текст`, режим `a+t` |
| `./SendMSG.sh "текст"` | внешний скрипт оповещений (через `system()`) |
| `./shutdown.sh ["текст"]` | внешний скрипт выключения ОС |
| SysV shm + sem, ключ `0x1b56` | shm 224 Б (`IPC_CREAT\|0600`), семафор из 1 элемента; обмен с GUI `ups_manager` |
| `/dev/ttyS<порт-1>` | COM-порт ИБП |
| `/dev/usb/hiddev0..15` | USB-HID ИБП, VID `0x0925`, PID `0x1234` |

## 5. Запуск: `main()`

`InitPMCfg()` читает `pm.conf`. Затем по `argv[1]` (проверка подстроки через `strstr`):

- **нет аргументов** — `delete_shm(); create_shm();` и тихий запуск;
- **`stop`** — пишет в почтовый ящик shm строку `shutdown` (константы `0x74756873 0x6e776f64` = «shut» «down»)
  и выходит; работающий демон видит её в начале следующего цикла и завершается;
- **`debug`** — пересоздаёт shm, `m_nStartType = 0` (подробный вывод);
- **`start`** — ведёт себя как `debug` (тоже `m_nStartType = 0`; похоже на остаток старой логики);
- **любой другой** — если в ящике уже `start`, печатает `%s is runing!` и выходит; иначе пишет туда `start` и работает.

Дальше `SetStatus(pMesg, m_nStartType)` инициализирует глобалы (порт `/dev/ttyS0`, 2400 бод, retry = 5,
`UpsReady = 1`, `debug = (type == 0)`), затем `Start(0x960)` — главный цикл. После возврата печатается
`%s Program Quit!`.

---

## 6. Главный цикл `Start()`

Инициализация: `ReadConfiguration` → `system("clear")` → `oldwarntime = time()` → `read_shm` →
`FindProIsRun()` (если жив другой экземпляр: `%s is Running in System Now!` и выход) → `open_dev(mode, port, baud)`.

Итерация цикла (`0x8049a1c`):

1. `read_shm(&UPS_Data_tmp)`. Если в `cMesgTxt` есть `shutdown` — выход из цикла (удаление shm, запись `0` в `Monitor.pid`).
2. Если `nSet == 1`: перечитать конфиг, сбросить `nSet`. Сменился `nCommMode` → закрыть устройство, напечатать
   `The communicate mode changed!The process restart...` и открыть заново. Сменился `nComPort` при `mode == 0` →
   то же с сообщением `The com port changed!…`.
3. Если `dev_err` (ошибка USB): `sleep(5)`, `close_dev`, `open_dev`.
4. `counter++`, `m_UPS_DATA->nMesgLen = counter` (heartbeat).
5. Если прошлый опрос не удался (`bQuarySuccess == 0`): `local_fail++`; при `local_fail >= 5` — если `UpsReady`, то
   `UpsReady = 0` и `ClrAllData()` (все измерения в 0); `local_fail = 0`; `SendMessage(3)`
   («UPS Adapter No Response…»); `sleep(15)`. Иначе: `UpsReady = 1`, `local_fail = 0`.
6. `bQuarySuccess = 1`.
7. При `counter % 10 == 1`: `Quary_F()` (команда `I`) и `Quary_I()` (команда `F`).
8. `Quary_Q1()`. Результат 0 → `bQuarySuccess = 0`, следующий цикл; результат 1 → шаги ниже
   (результат 2 в коде недостижим — мёртвая ветка выхода).
9. Побитовое сравнение `UPS_status` и `UPS_status_old`: для каждого изменившегося бита 0…7 — бит стал 1 →
   `Process_UPS_warging(bit)`, стал 0 → `Process_UPS_Cancel_warging(bit)`. Затем `old = status`.
10. Если `UPS_status != 0`: `ProcessCountDown()`. Если `bShutdownUps` — блок выключения (раздел 9).
11. `sleep(1)`.

Темп опроса при исправном ИБП: внутри `Q1` — `sleep(1)` между отправкой и чтением, плюс `sleep(1)` в конце цикла,
то есть около 2 секунд на итерацию.

## 7. Протокол Megatec

### COM-порт (`InitializeCom`)

- `open(path, O_RDWR|O_NOCTTY|O_NONBLOCK, 0666)` (флаги `0x902`).
- `c_cflag = скорость | размер | чётность | CLOCAL | CREAD (0x880)`; остальные поля termios (`iflag`, `oflag`, `lflag`,
  `c_cc`) — нули: сырой режим, `VMIN = VTIME = 0`. Перед `tcsetattr` выполняется `tcflush(TCIFLUSH)`.
- Таблица скоростей: 110, 300, 600, 1200, 4800, 9600, 19200, 38400 — стандартные `B*`; 57600 → `0x1001`,
  115200 → `0x1002`; **любая другая → `B2400` (0xb)**.
- Размер: аргумент 1 → CS5 (0), 2 → CS6 (0x10), 3 → CS7 (0x20), иначе CS8 (0x30). Чётность: 1 → `0x200`, 2 → `0x100`.
- `InitCom` вызывает `InitializeCom(port, 2400, 0, 0)`, то есть **2400 8N1 без управления потоком**.
- `SendStr` — `write(fd, s, strlen(s))`; `GetStr` — `read(fd, buf, n)`. Ожидание ответа — фиксированный `sleep(1)`.

### Команды

| Команда | Запрос | Ответ | Читаемый размер |
|---|---|---|---|
| `Q1` | `Q1` + CR (`0x804cb9c`) | `(MMM.M NNN.N PPP.P QQQ RR.R S.SS TT.T b7…b0` + CR | `(` + 46 байт |
| `I` | `I` + CR (`0x804cbab`) | `#company(15) model(10) version(10)` + CR | `#` + 38 байт |
| `F` | `F` + CR (`0x804cbae`) | `#MMM.M QQQ SS.SS FF.F` + CR | `#` + 21 байт |
| `S<nn>` | `S%02d` + CR | — | выключение ИБП через *nn* минут |

Алгоритм чтения (COM): `read(1)` до заголовочного символа (`(` или `#`); затем `read(N)` ровно *N* байт; последний
байт должен быть CR. Ошибка `read` (-1) или отсутствие данных → «нет ответа» (возврат 0). **Но** если
заголовок получен, а длина или CR неверны, функция возвращает 1 (успех) без обновления данных.

### Разбор `Q1` (смещения внутри 46 байт после `(`)

| Смещение | Поле | Куда | Проверка диапазона (`lo < v < hi`) |
|---|---|---|---|
| 0–4 | входное напряжение | `fAcInValue` | −1 … 450 |
| 6–10 | напряжение отказа | игнорируется | — |
| 12–16 | выходное напряжение | `fAcOutValue` | −1 … 450 |
| 18–20 | нагрузка, % | `fLoad` | −1 … 150 |
| 22–25 | частота | `fAcFreValue` | 0 … 65 |
| 27–30 | батарея | `fDcValue` | −1 … 250 |
| 32–35 | температура | `fUpsTemp` | −20 … 100 |
| 37–44 | 8 бит статуса | `UPS_status`, `nWarnStatus` | символ `'1'` → бит взведён, MSB первым |

Числа разбираются через `strtod`; значение вне диапазона просто не записывается (остаётся прежнее). Границы
взяты из `.rodata` (`-1.0`, `450.0`, `150.0`, `65.0`, `250.0`, `-20.0`, `100.0`).

### Разбор `I` и `F`

- `I`: `company` — байты 0–14, `model` — 16–25, `version` — 27–36 (строки копируются `strcpy` в 18/16/18-байтовые поля).
- `F` (в оригинале `Quary_I`): напряжение (0–4) → `fRate_AcIn` (0…450); ток (6–8) → `fRate_Load` (−1…150);
  напряжение батареи (10–14) → `fRate_DcValue` (−1…250); частота (16–19) → `fRate_AcFre` (0…65).

### USB-текст (режим 1)

Те же команды через `SendData`. `Q1`: `GetData(buf, 0x2f, 1000 мс)`, затем `sscanf("(%5s %5s %5s %3s %4s %4s %4s %8s")`,
требуется ровно 8 полей; значения пишутся **без проверки диапазона**. `I`: 0x27 байт, `F`: аналогично.

### USB-бинарный (режим 2): отчёт 6 байт `[cmd, b1…b5]`, числа в BCD

Запрос — 4 байта `[cmd, 0, 0, 0]` через `WriteData`; `ReadData(6, 1000 мс)`; ответ годен, если `report[0] == cmd`.
`BCD(x) = (x >> 4) * 10 + (x & 15)`.

| cmd | Содержимое |
|---|---|
| 1 | вход = `BCD(b1)*10 + BCD(b2)/10`; если `b4 & 1` — запрос считается неудачным; `b5` — байт статуса |
| 2 | выход = `BCD(b1)*10 + BCD(b2)/10`; частота = `BCD(b3)*10 + BCD(b4)/10`; нагрузка = `BCD(b5)` |
| 3 | батарея = `BCD(b1) + BCD(b2)/100`; температура = `BCD(b3)*10 + BCD(b4)/10` |
| 4 | ном. вход = `BCD(b1)*10 + BCD(b2)/10`; ном. ток = `BCD(b3)*100 + BCD(b4)` |
| 5 | ном. батарея = `BCD(b1) + BCD(b2)/100`; ном. частота = `BCD(b3)*10 + BCD(b4)/10` |
| 6, 7, 8 | company: по 5 символов (`b1…b5`) × 3 = 15 |
| 9, 10 | model: 5 + 5 = 10 символов |
| 11, 12 | version: 5 + 5 = 10 символов |
| 0x11 | `ShutdownUPS`: `[0x11, delay, 0, 0]` |

## 8. Биты статуса, предупреждения, обратный отсчёт

### Биты (стандарт Megatec)

| Бит | Маска | Значение | Реакция оригинала |
|---|---|---|---|
| 7 | 0x80 | Utility Fail (работа от батареи) | отсчёт [0], сообщение `AC Fail` |
| 6 | 0x40 | Battery Low | отсчёт [1], `LOW CELL` |
| 5 | 0x20 | Bypass/Boost | сообщение `UPS BYPASS` |
| 4 | 0x10 | UPS Failed | отсчёт [2], `UPS Failed` |
| 3 | 0x08 | тип: 1 = standby, 0 = online | не используется |
| 2 | 0x04 | идёт тест | сообщение `UPS TESTTING` |
| 1 | 0x02 | выключение активно | не используется |
| 0 | 0x01 | звуковой сигнал | не используется |

Таблицы переходов `Process_UPS_warging` (`0x804d484`) и `Process_UPS_Cancel_warging` (`0x804d4a4`) — по 8 записей,
индекс — номер бита; для битов 0, 1, 3 запись ведёт на «ничего не делать».

### Сообщения (все через `./SendMSG.sh "…"`, кроме отмеченных)

| Событие | Текст |
|---|---|
| бит 7 взведён / сброшен | `Received By %s  -->  AC Fail` / `…AC NORMAL,Warnning Canceled` |
| бит 6 | `…LOW CELL` / `…Battery OK,Warnning Canceled ` |
| бит 5 | `…UPS BYPASS` / `…BYPASS OK,Warnning Canceled  ` |
| бит 4 | `…UPS Failed` / `…UPS OK,Warnning Canceled  ` |
| бит 2 | `…UPS TESTTING` / `…TEST OK,Warnning Canceled  ` |
| отсчёт | `AC FAIL:System Shutdown after %d Second`, `LOW CELL:…`, `UPS FAIL:…` |
| нет связи | `UPS Adapter No Response, please stop the process!!` |

Опечатки `TESTTING` и `Warnning` — в оригинале. Все сообщения «Received By…» дополнительно пишутся в журнал
(`AddLog`); сообщения отсчёта в журнал не пишутся.

### `ProcessCountDown()`

Вызывается каждую итерацию, пока `UPS_status != 0`:

```
active = false; idx = 0
если AC_FAIL  и cfg.ac_fail_delay      != -1: active = true; если cd[0] <= 0: Shutdown(1)
если LOW_BATT и cfg.battery_low_delay  != -1: active = true; если cd[1] <= 0: Shutdown(2); если cd[1] < cd[idx]: idx = 1
если UPS_FAIL и cfg.ups_failed_delay   != -1: active = true; если cd[2] <= 0: Shutdown(3); если cd[2] < cd[idx]: idx = 2
если active и (now - oldwarntime) >= cfg.warning_interval:
    oldwarntime = now; cd[idx] -= warning_interval; SendMessage(idx)
```

`cd[]` при взведении бита инициализируется значением из конфига, при сбросе — `INT_MAX`. Сообщение отправляется
**после** вычитания, поэтому при задержке 300 с и интервале 10 с первое сообщение говорит «290 Second». Для
отсчёта выбирается меньшее из активных значений.

## 9. Последовательность выключения

`Shutdown(code)` (код 1/2/3 = AC / LOW CELL / UPS BAD), если `bShutdownUps == 0`:

1. в журнал и в `system()` уходит `./shutdown.sh "AC Fail:System may be shutdown."` (или `LOW CELL…`, `UPS BAD…`);
2. `bShutdownUps = 1`.

Затем в `Start()` при `bShutdownUps`:

1. если `nTurnOffUPS` — `ShutdownUPS(comm_mode, nTurnOffUPSTime)`: режим 0 → `S%02d` + CR в COM, режим 1 → то же через
   `SendData`, режим 2 → отчёт `[0x11, delay]`;
2. печать `exec shutdown.sh /n`;
3. **повторный** `system("./shutdown.sh")` (без аргументов);
4. `bShutdownUps = 0`, `UPS_status = 0`.

Итого `shutdown.sh` вызывается дважды подряд. Пока питание не вернулось и ОС жива, на следующей итерации
всё повторится (см. раздел 12).

## 10. USB-HID слой (`ups_comm.c`)

**Открытие (`UsbOpen`).** Перебор `/dev/usb/hiddev0…15`: `open(O_RDWR)`; при `ENOENT` — `mknod(path, S_IFCHR|0666,
makedev(180, 96+i))` и повтор того же индекса. `ioctl HIDIOCGDEVINFO (0x801c4803)`: VID по смещению 16 и PID по смещению 18
должны быть `0x0925` / `0x1234`. Далее `SetBaud`, очистка буферов, хук `USB_CONNECT_SUC (2)`, однократный запуск потока
`USBReadThread`.

**Коды хука (`_usb_ctrl_error`):** `USB_READ_FAIL 0`, `USB_WRITE_FAIL 1`, `USB_CONNECT_SUC 2`, `USB_DISCONNECT 3`,
`USB_CREATE_THREAD_FAIL 4`. Обработчик `usberr_occur` на коды 0 и 1 печатает `Usb device error occured!` и
взводит `dev_err`; `Start` через 5 секунд переоткрывает устройство.

**Отправка PDU (`UsbSendPDU`).** 4 байта PDU кладутся как 4 usage: `HIDIOCSUSAGE (0x4018480c)` со структурой
`{report_type=2 (feature), report_id=0, field_index=(i>>1)&1, usage_index=i&1, value=byte}`, затем
`HIDIOCSREPORT (0x400c4808)` с `{2, 0, 0}`.

- `SetBaud`: PDU `[0x80, (500000 / baud) & 0xFF, 0, 0]`.
- `SendData` (текст): куски по ≤ 3 байта, PDU = `[0xA0 | n, d0, d1, d2]`, пауза `int(n * 11000 / baud + 0.5)` мс.
- `WriteData` (бинарный): сырые 4 байта, пауза 1 мс.

**Приём (`USBReadThread`).** Начальное заполнение через `HIDIOCGUCODE (0xc018480d)` / `HIDIOCGUSAGE (0xc018480b)`.
Цикл: `select` 250 мс → `read` до 512 байт структур `hiddev_event {hid, value}` → `report[(hid & 0xff) - 3] = value`.
Если `report[0] & 0x80` — это UART-кадр: длина `report[0] & 7` (не более 5), байты `[1…n]` кладутся в UART-кольцо;
иначе 6 байт идут в USB-кольцо. При ошибке чтения (`EIO`, `ENOENT`, `ENODEV`) устройство закрывается,
буферы очищаются, вызывается `USB_DISCONNECT`, и раз в секунду выполняется переоткрытие.

`GetData` / `ReadData` забирают байты из колец с опросом каждые 10 мс до таймаута.

## 11. Shared memory и семафоры

- Ключ `0x1b56`. `shmget(key, 224, IPC_CREAT|0600)` (при создании в `create_shm` добавляется `IPC_EXCL`), `semget(key, 1, …)`.
- `LockSem`: `semop` с двумя операциями — «ждать нуля», затем `+1` с `SEM_UNDO`. `UnlkSem`: `-1` с `SEM_UNDO|IPC_NOWAIT`.
- `read_shm` / `write_shm`: attach → lock → `memcpy` 224 байта (`rep movsd ×0x38`) → unlock → detach.
- После каждого успешного `Q1` оригинал делает `read_shm(tmp)`, **`delete_shm()`** (удаляет и сегмент, и семафор),
  копирует `tmp.nSet` в рабочую структуру и `write_shm()` (сегмент создаётся заново через `IPC_CREAT`).

---

## 12. Баги и странности оригинала

| № | Где | Что |
|---|---|---|
| 1 | `QuaryUpsStatusProcess` и аналоги | Битый кадр (неверная длина/CR) возвращает «успех»: детектор потери связи слеп к мусору в линии |
| 2 | `Process_UPS_Cancel_warging` | При отмене маскируются несвязанные младшие биты `UPS_status` (`& 0xFE`, `& 0xFD`, `& 0xFB`); затем `old = status` копирует искажённое значение → возможны ложные «изменения» |
| 3 | `Shutdown()` | При повторном вызове (`bShutdownUps == 1`) в `system()` уходит неинициализированный стековый буфер |
| 4 | цикл + `Shutdown` | `shutdown.sh` вызывается дважды, а пока отказ сети сохраняется и ОС жива — повторяется на каждой итерации |
| 5 | `Quary_F` / `Quary_I` | Имена перепутаны с реально отправляемыми командами |
| 6 | `ReadData` | Проверяет флаг переполнения UART-кольца (`0x804fb8c`) вместо USB-кольца (`0x8053ba8`) — опечатка копирования |
| 7 | `UsbOpen` | Если `mknod` не удался, цикл повторяет тот же индекс бесконечно |
| 8 | shm | Delete + recreate при каждом опросе: окно гонки для GUI-клиентов |
| 9 | `nMesgLen` | Поле «длина сообщения» используется как heartbeat |
| 10 | `Start` | Результат 2 из `Quary_Q1` недостижим; `m_RemoteCtrlReady` никогда не взводится |
| 11 | `start` / `debug` | Оба включают отладочный вывод |
| 12 | `ReadConfiguration` | Короткий файл оставляет остаток структуры неинициализированным (проверяется только сигнатура) |

### Замечания по безопасности (для аудита развёртывания)

- Все внешние вызовы — `system()` с **относительными путями** `./SendMSG.sh`, `./shutdown.sh`. Результат зависит от
  рабочего каталога; если демон запущен от root из каталога, где пишет кто-то ещё, возможно выполнение чужого кода.
- Имя процесса из `pm.conf` подставляется в командную строку без экранирования. Кавычка в `ProgramName` даёт
  инъекцию команд оболочки (нужен доступ на запись к `pm.conf`).
- shm создаётся с правами `0600`: GUI должен работать под тем же пользователем. Любой процесс с этим правом может
  записать `shutdown` в почтовый ящик и остановить демон.

---

## 13. Python-реализация `ups_monitor.py`

Только стандартная библиотека, Linux. Состав: разбор протокола, конфиг (`TimeCfg.conf` бит-в-бит), SysV shm через
`ctypes`, три транспорта (COM, USB-текст, USB-бинарный), виртуальный ИБП на pty, конечный автомат `Monitor`.

### Запуск

```bash
python3 ups_monitor.py selftest                       # 9 автотестов
python3 ups_monitor.py simulate                       # демо: сбой сети и восстановление
python3 ups_monitor.py decode "(230.0 140.0 230.0 020 50.0 2.27 25.0 10001001"
python3 ups_monitor.py config --ac-fail-delay 120 --write       # создать/изменить TimeCfg.conf
python3 ups_monitor.py run --device /dev/ttyS0 --dry-run --debug  # боевой цикл без выполнения скриптов
python3 ups_monitor.py run --shm                      # + публикация в shm 0x1B56
python3 ups_monitor.py stop                           # SIGTERM процессу из Monitor.pid
```

Для реального запуска положите `SendMSG.sh` и `shutdown.sh` в рабочий каталог (`-w DIR`); без `--dry-run`
они выполняются так же, как в оригинале. Сначала проверьте `--dry-run`.

### Сохранено без изменений

Пороговые диапазоны, формат кадров, смещения, период `F`/`I` (каждый 10-й цикл), порог 5 сбоев и пауза 15 с,
логика выбора минимального отсчёта, тексты сообщений (с опечатками), двойной вызов `shutdown.sh`, формат
журнала `PowerEventГГГГММ.log`, формат `TimeCfg.conf` и shm.

### Осознанные отличия

| Отличие | Причина |
|---|---|
| Чтение ответа с таймаутом (1,5 с) вместо `sleep(1)` + `read` | быстрее и надёжнее; цикл около 1 с вместо 2 с |
| `tcflush` перед каждой командой | устаревшие байты не склеиваются с ответом |
| Битый кадр = неудача | устраняет баг 1 |
| Отмена предупреждения не трогает посторонние биты | баг 2 |
| `Shutdown` при уже взведённом флаге ничего не запускает | баг 3 |
| Скрипты запускаются списком аргументов, без оболочки | нет инъекции через `ProgramName` |
| Короткий `TimeCfg.conf` → значения по умолчанию | баг 12 |
| shm: read-modify-write без delete/recreate; почтовый ящик сохраняется | баг 8 |
| Добавлены `--dry-run`, подкоманды `simulate`, `decode`, `config`, `selftest` | удобство |

### Что проверено, а что нет

- **Проверено** (автотесты на pty-симуляторе Megatec): разбор `Q1`/`I`/`F` и проверка диапазонов; упаковка
  `TimeCfg.conf` (52 Б, сигнатура) и `UPS_DATA_Status` (224 Б, смещения 0x80/0x98/0xbe); BCD; SysV shm
  (запись/чтение/удаление); полный сценарий отказа сети до выключения с командой `S02`; отмена предупреждения;
  потеря связи после 5 неудачных опросов.
- **Не проверено:** реальный COM-порт с настоящим ИБП, USB-HID режимы 1 и 2 (код написан по дизассемблеру
  `UsbOpen`/`UsbSendPDU`/`USBReadThread`), совместимость shm с оригинальным GUI `ups_manager`.

## 14. Как воспроизвести анализ

```bash
file ups_monitor; readelf -h -S -d ups_monitor
strings -a -n 3 ups_monitor                  # сообщения, команды, пути
nm -n ups_monitor                            # функции и глобалы (бинарник не stripped)
readelf --debug-dump=info ups_monitor        # структуры UPS_DATA_Status, TheCfgData
objdump -d -M intel --no-show-raw-insn ups_monitor > dis.txt   # дизассемблер
```

Числовые константы: `.rodata` читались напрямую (`float` по адресам `0x804d464…0x804d480`, таблицы переходов
`0x804d484` и `0x804d4a4`, значения по умолчанию конфига — `SetDefaultValue`).

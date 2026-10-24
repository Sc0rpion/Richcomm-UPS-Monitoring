# Reconstructed `ups_monitor` Daemon (Reverse-Engineered Python Reimplementation)

**Languages**: [English](README.md) | [Русский](README.ru.md)

This directory contains **`ups_monitor.py`** — an accurate, structure- and memory-compatible Python reimplementation of the legacy `ups_monitor` daemon, reverse-engineered from the original 32-bit ELF executable.

---

## 1. Background & Legacy Context

The original `ups_monitor` binary was provided by Richcomm Technologies as part of their Linux management suite:
* **Binary Format**: ELF 32-bit LSB executable, Intel 80386.
* **Compiler Toolchain**: GCC 3.2.2 (Red Hat Linux release 8.0/9.0, circa 2002–2003).
* **C Library**: GNU C Library (glibc) version 2.3.2.
* **Why it fails on modern systems**:
  1. Contemporary 64-bit distributions lack 32-bit runtime loaders (`/lib/ld-linux.so.2`) by default.
  2. Deprecated `/dev/usb/hiddev0` device handling (`mknod` with fixed permissions fails in Docker / read-only `/dev`).
  3. Memory layout and semaphore discrepancies when communicating with modern 64-bit user processes.
  4. Memory corruption bugs in the original C code causing segmentation faults (`SIGSEGV`).

`ups_monitor.py` reconstructs 100% of the legacy behavior in pure Python 3.8+ without compilation or third-party packages.

---

## 2. Core Architecture & Algorithms

### 2.1. The Main Polling Loop: `Start()`
The daemon operates as a finite state machine (FSM):
1. **Link Initialization (`InitializeCom` / `UsbOpen`)**:
   - Serial mode: `open(O_RDWR | O_NOCTTY | O_NONBLOCK)` with raw `termios` configuration (2400 baud, 8N1, no flow control).
   - USB mode: Scans `/dev/usb/hiddev0` through `/dev/usb/hiddev15` checking VID `0x0925` and PID `0x1234` via `HIDIOCGDEVINFO`. Upon match, calls `SetBaud(2400)` and spawns the background worker `USBReadThread`.
2. **IPC Polling (SysV Shared Memory)**:
   - Checks inbox commands from GUI utilities: if `cMesgTxt` contains `"shutdown"`, the daemon exits cleanly.
   - If `nSet == 1`, re-reads `TimeCfg.conf` and reopens transport links if communication parameters changed.
3. **Telemetry Polling**:
   - Queries `Q1\r` every cycle (1 second).
   - Queries `I\r` (Identity) and `F\r` (Ratings) every 10 cycles.
   - After 5 consecutive query failures (`m_RetryCount >= 5`), resets ready flag, clears telemetry, and broadcasts `"UPS Adapter No Response, please stop the process!!"`.
4. **Status Change Handling (`handle_status_change`)**:
   - Computes bitwise XOR: `status_old ^ status_new`.
   - Dispatches `on_warning` on rising edges ($0 \rightarrow 1$) and `on_cancel` on falling edges ($1 \rightarrow 0$) for each bit.
5. **Countdown & Orderly Shutdown (`ProcessCountDown`)**:
   - Independent countdown timers for mains utility failure, battery low, and inverter fault.
   - Broadcasts warnings every `WarningInterval` seconds via `./SendMSG.sh`.
   - Upon timer expiry, triggers `./shutdown.sh` and sends inverter shutdown command `S<n>\r`.

---

## 3. Data Structures & Protocols

### 3.1. Binary Configuration: `TimeCfg.conf` (52 Bytes)
Little-endian struct layout (`struct TheCfgData`):
```c
struct TheCfgData {
    int   head;                 // 0x00: Magic number 0x55AA0335
    int   ac_fail_delay;        // 0x04: Seconds before shutdown on AC fail (-1 = disabled)
    int   battery_low_delay;    // 0x08: Seconds before shutdown on Low Battery (-1 = disabled)
    int   ups_failed_delay;     // 0x0C: Seconds before shutdown on UPS Fail (-1 = disabled)
    int   warning_interval;     // 0x10: Warning repeat interval (seconds)
    char  ups_ip[16];           // 0x14: SNMP IP string (unused in C logic)
    int   turn_off_ups;         // 0x24: 1 = send S<n> to UPS after system shutdown
    int   turn_off_ups_time;    // 0x28: Minutes delay for S<n> command
    int   com_port;             // 0x2C: COM port index (1 -> /dev/ttyS0)
    int   comm_mode;            // 0x30: 0=COM, 1=USB-HID Text, 2=USB-HID Binary
}; // Exactly 52 bytes
```

### 3.2. SysV Shared Memory: `UPS_DATA_Status` (224 Bytes, Key `0x1B56`)
Synchronized via binary semaphore `0x1B56`:
```c
struct UPS_DATA_Status {
    int   nMesgLen;             // 0x00: Heartbeat loop counter
    char  cMesgTxt[120];        // 0x04: Command mailbox ("shutdown", "start")
    int   nSet;                 // 0x7C: Flag to reload TimeCfg.conf
    float fAcInValue;           // 0x80: Input Voltage (V)
    float fAcOutValue;          // 0x84: Output Voltage (V)
    float fAcFreValue;          // 0x88: Line Frequency (Hz)
    float fDcValue;             // 0x8C: Battery Voltage (V)
    float fUpsTemp;             // 0x90: Internal Temperature (°C)
    float fLoad;                // 0x94: Power Load (%)
    int   nWarnStatus;          // 0x98: Status byte (Megatec Q1 bits b7..b0)
    float fRate_AcIn;           // 0x9C: Nominal Input Voltage (from F command)
    float fRate_DcValue;        // 0xA0: Nominal Battery Voltage
    float fRate_AcFre;          // 0xA4: Nominal Line Frequency
    float fRate_Load;           // 0xA8: Nominal Current Rating
    char  cCompanyName[18];     // 0xAC: Manufacturer string (from I command)
    char  cUpsModel[16];        // 0xBE: UPS Model string
    char  cVersion[18];         // 0xCE: Firmware version string
}; // Exactly 224 bytes
```

---

## 4. Dissected Bugs in the Original Binary

Reverse-engineering the disassembled ELF32 code revealed several critical bugs in the original implementation:

| Original C Bug | Real-World Failure | Fix in `ups_monitor.py` |
|---|---|---|
| **Mask corruption in `Process_UPS_Cancel_warging`** | Applied bitwise AND masks `& 0xFE`, `& 0xFD`, `& 0xFB`, inadvertently clearing unrelated status bits (e.g. beeper and test flags). | Fixed: strictly modifies only the target warning bit. |
| **Wild pointer in `Shutdown()`** | Re-entry called `system(buffer)` where `buffer` contained uninitialized stack garbage. | Added `shutdown_flag` re-entrancy guard. |
| **Swapped function names** | Function `Quary_F` actually sent `I\r`, while `Quary_I` sent `F\r`. | Logic kept compatible, but properly named in Python. |
| **Unchecked short reads of `TimeCfg.conf`** | Reading files < 52 bytes parsed garbage stack data. | Enforces exact 52-byte length and `0x55AA0335` magic check. |
| **Serial buffer buildup** | Lacked input buffer flushing before sending commands. | Adds `tcflush(TCIFLUSH)` before every serial query. |

---

## 5. Usage & Tools

### Built-in Virtual PTY Simulator
Test the complete daemon without physical hardware using Linux pseudo-terminals (`pty`):
```bash
python3 recompiled/ups_monitor.py simulate
```
Simulates a power outage, 25-second battery countdown, alert generation, and AC recovery.

### Automated Unit Test Suite
Executes 9 automated unit tests verifying struct packing, BCD conversions, Megatec parsers, SHM memory writes, and shutdown workflows:
```bash
python3 recompiled/ups_monitor.py selftest
```

### Run Daemon in Production
```bash
python3 recompiled/ups_monitor.py run --device /dev/ttyS0 --shm --debug
```

### Inspect and Edit `TimeCfg.conf`
```bash
# Display configuration:
python3 recompiled/ups_monitor.py config

# Update AC fail shutdown delay to 180 seconds:
python3 recompiled/ups_monitor.py config --ac-fail-delay 180 --write
```

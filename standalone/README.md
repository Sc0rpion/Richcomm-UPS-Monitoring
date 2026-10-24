# Standalone CLI & Daemon: `upstest.py`

**Languages**: [English](README.md) | [Русский](README.ru.md)

A self-contained, zero-dependency Python 3 driver and CLI monitoring daemon for **SVC / Richcomm Technologies** UPS units (USB VID `0x0925`, PID `0x1234`).

Works entirely using the Python standard library (Linux only, Python 3.8+) and provides a 100% modern replacement for the obsolete 32-bit `ups_monitor` / `ups_status` utility suite.

---

## Key Features

1. **Direct USB-HID Driver (`/dev/usb/hiddev*`)**:
   - Automatic hardware discovery across `/dev/usb/hiddev0..15`.
   - Node auto-creation (`mknod major 180, minor 96+i`) for Docker / LXC containers.
   - Baud rate initialization to 2400 bps via PDU `[0x80, 0xD0, 0x00, 0x00]`.
   - Native Richcomm FRAMED `0xA0` packet encapsulation (`[0xA0 | len, d0, d1, d2]`).
   - Background reader thread parsing incoming UART `0x80` frames.
2. **Dynamic Hardware Identity (`I\r`)**:
   - Queries manufacturer, model name, and firmware version directly from the UPS microcontroller.
3. **Stabilized Beeper Control (`Q\r`)**:
   - Reads bit `b0` before transmitting to prevent accidental toggling.
   - Enforces a 350 ms settling delay and retries up to 3 times with state verification.
4. **Battery Self-Test Under Load**:
   - Triggers a 10-second battery test (`T\r`) with real-time terminal telemetry.
   - Immediate cancellation with `CT\r`.
5. **SysV Shared Memory Compatibility (Key `0x1B56`)**:
   - Read mode: reads telemetry from an active legacy daemon without USB port contention.
   - Server mode: publishes telemetry to SHM for legacy GUI tools.
6. **Background Monitoring Daemon (`--daemon`)**:
   - Monitors mains AC failure, battery low, and inverter fault conditions.
   - Countdown to system shutdown with alerts via `./SendMSG.sh`.
   - Orderly system shutdown (`./shutdown.sh`) and optional inverter power-off (`S<n>\r`).
   - Live JSON telemetry export to `/tmp/ups_status.json` (for Zabbix, Telegraf, Prometheus).

---

## Quick Start

### One-Shot Status Query
```bash
python3 upstest.py
```
Displays an ASCII telemetry table:
```text
+-----------------------------------------------------------------------------+
| Mode: Megatec_megatec_q1 Transport: hiddev   Device: /dev/usb/hiddev0        |
+---------------------------------++------------------------------------------+
|     Input AC Power : Normal     || UPS Input Voltage   : 231.00 Volt        |
|     Battery Status : Normal     || UPS Output Voltage  : 232.00 Volt        |
|     UPS Status     : Normal     || UPS Battery Level   : 100.00 %           |
|     Boost/Buck     : Normal     || UPS Battery Voltage :  13.70 Volt        |
|     Beeper Status  : On         || UPS Power Loading   :  10.00 %           |
|     Timestamp      : 2026-10-24 || UPS Line Frequency  :  49.00 Hz          |
+---------------------------------++------------------------------------------+
```

### Query Hardware Model & Firmware Version
```bash
python3 upstest.py --info
```
Example output:
```text
======================================================================
UPS IDENTITY & FIRMWARE (Command 'I\r')
======================================================================
[SUCCESS] Hardware identity retrieved from UPS:
  -> Manufacturer     : SVC
  -> Model Name       : V-650-F-LCD
  -> Firmware Version : V1.2
```

### Beeper (Sound) Control
```bash
# Toggle current state:
python3 upstest.py --beeper

# Explicitly turn ON:
python3 upstest.py --set-beeper on

# Explicitly turn OFF (Silent mode):
python3 upstest.py --set-beeper off
```

### Battery Self-Test
```bash
python3 upstest.py --selftest
```
Switches to battery power for 10 seconds and reports voltages every second. Press `Ctrl+C` to cancel early.

---

## Diagnostic Test Suite (`--test`)

```bash
python3 upstest.py --test all       # Run all 6 hardware & protocol tests
python3 upstest.py --test sniff      # Test 1: Sniff incoming USB reports for 5s
python3 upstest.py --test baud       # Test 2: Verify SetBaud 2400 bps
python3 upstest.py --test q1         # Test 3: Query Megatec Q1 status
python3 upstest.py --test wakeup     # Test 4: Test UART wakeup (CR + Q1)
python3 upstest.py --test binary     # Test 5: Test binary BCD fallback reports
python3 upstest.py --test identity   # Test 6: Query hardware identity (I\r)
python3 upstest.py --test shm        # Test SysV Shared Memory
```

---

## Background Daemon Mode (`--daemon`)

Run as a system monitoring service:
```bash
python3 upstest.py --daemon
```
Configure shutdown delays interactively:
```bash
python3 upstest.py --config
```

---

## JSON Output

For integration with third-party dashboards and monitoring agents:
```bash
python3 upstest.py --json
```
```json
{
  "online": true,
  "mode": "Megatec_megatec_q1",
  "transport": "hiddev",
  "device": "/dev/usb/hiddev0",
  "input_voltage": 231.0,
  "output_voltage": 232.0,
  "battery_level": 100.0,
  "battery_voltage": 13.7,
  "load_percent": 10.0,
  "frequency": 49.0,
  "temperature": 30.8,
  "ac_status": "Normal",
  "battery_status": "Normal",
  "ups_status": "Normal",
  "boost_buck": "Normal",
  "beeper": "On",
  "shutdown_active": false,
  "test_in_progress": false,
  "raw_response": "(231.0 000.0 232.0 010 49.0 13.7 30.8 00001001",
  "timestamp": "2026-10-24 07:02:52"
}
```

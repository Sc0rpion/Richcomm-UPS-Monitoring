# SVC / Richcomm UPS Home Assistant Integration

**Languages**: [English](README.md) | [Русский](README.ru.md)

Custom integration for **SVC / Richcomm Technologies UPS** units (VID `0x0925`, PID `0x1234`) for **Home Assistant** with full **HACS** custom repository support.

---

## Features

* **Direct Hardware Communication**: Native USB-HID driver (`/dev/usb/hiddev*`) using Python standard library (no extra pip requirements).
* **Dynamic Hardware Identity**: Queries manufacturer, model name, and firmware version directly from the UPS chip via command `I\r`.
* **Mute / Beeper Control Switch**: State-verified sound switch with 350 ms settling delay and retry loop.
* **Battery Self-Test Buttons**: 10-second battery self-test under load (`T\r`) and emergency cancel (`CT\r`).
* **9 Numerical Sensors**: Input voltage, output voltage, battery voltage, battery estimated charge %, power load %, AC frequency, temperature, AC status, UPS status.
* **5 Binary Sensors**: AC utility connected (`plug`), battery low (`battery`), AVR boost/buck, UPS problem, test in progress.

---

## Installation via HACS (Recommended)

1. Open **HACS** in your Home Assistant sidebar.
2. Go to **Integrations**.
3. In the top-right menu (three dots **⋮**), select **Custom repositories**.
4. Enter the repository URL:
   ```text
   https://github.com/Sc0rpion/Richcomm-UPS-Monitoring
   ```
5. Select category: **Integration**.
6. Click **Add**, find **SVC Richcomm UPS**, and click **Download**.
7. Restart Home Assistant.
8. Go to **Settings &rarr; Devices & Services &rarr; Add Integration &rarr; Search "SVC Richcomm UPS"**.

---

## Standalone MQTT Auto-Discovery Bridge (Synology / Proxmox)

For setups where the UPS USB cable is physically plugged into a NAS or virtual machine separate from Home Assistant:
1. Navigate to `/ha/` and review `docker-compose.yml`.
2. Configure your MQTT broker address, credentials, and run:
   ```bash
   docker-compose up -d
   ```
3. The bridge automatically discovers and registers the UPS device and all entities in Home Assistant via MQTT Discovery.

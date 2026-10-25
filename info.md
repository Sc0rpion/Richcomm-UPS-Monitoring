<p align="center">
  <img src="https://raw.githubusercontent.com/Sc0rpion/Richcomm-UPS-Monitoring/main/images/banner.png" alt="SVC Richcomm UPS" width="100%" />
</p>

# SVC Richcomm UPS Integration for Home Assistant

Complete Home Assistant integration for **SVC / Richcomm Technologies UPS** units (VID: `0x0925`, PID: `0x1234`).

## Features:
* **Dynamic Hardware Identity**: Automatically reads the manufacturer, model name, and firmware version directly from the UPS microcontroller (Megatec `I\r` command).
* **Stabilized Beeper (Sound) Switch**: Mute/unmute switch with state verification and retry logic to avoid UART packet collisions.
* **10-Second Battery Self-Test**: Dedicated buttons to trigger battery self-test under load (`T\r`) and emergency cancel (`CT\r`).
* **Full Live Telemetry**: Input voltage, output voltage, mains frequency, battery voltage, estimated battery charge %, load %, and temperature.
* **Binary Sensors**: Utility 220V power status, battery low warning, AVR Boost/Buck indicator, UPS problem, and test active flag.

---

### Русское описание:
Полнофункциональная интеграция ИБП SVC / Richcomm Technologies для Home Assistant. Поддерживает динамический запрос версии прошивки и модели, надёжный переключатель звука (бипера), 10-секундный тест АКБ и полную телеметрию.

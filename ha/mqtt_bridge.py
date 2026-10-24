#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SVC / Richcomm UPS -> Home Assistant MQTT Auto-Discovery Bridge
===============================================================
Опрашивает ИБП по протоколу Megatec Q1 и автоматически публикует
сенсоры, бинарные сенсоры, переключатель бипера и кнопку селф-теста
в Home Assistant через MQTT Discovery (zero-configuration).

Идеально для запуска в Docker на хосте с подключенным USB (Synology / Proxmox / Linux),
когда Home Assistant работает на другом сервере.
"""

import os
import sys
import time
import json
import signal
import logging
import argparse
import paho.mqtt.client as mqtt

# Импорт низкоуровневого драйвера
from custom_components.svc_ups.driver import SvcUpsHardware, St

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("ha_mqtt_bridge")

DISCOVERY_PREFIX = "homeassistant"
DEVICE_ID = "svc_richcomm_ups"
BASE_TOPIC = f"svc_ups/{DEVICE_ID}"

DEVICE_INFO = {
    "identifiers": [DEVICE_ID],
    "name": "SVC Richcomm UPS",
    "manufacturer": "SVC / Richcomm Technologies",
    "model": "USB-to-UART Bridge (0925:1234)",
    "sw_version": "v2.5 (Megatec Q1)"
}


def publish_discovery(client: mqtt.Client):
    """Публикация конфигураций Home Assistant MQTT Auto-Discovery."""
    logger.info("Публикация конфигурации MQTT Auto-Discovery для Home Assistant...")

    # 1. СЕНСОРЫ (Числовые измерения)
    sensors = [
        ("input_voltage", "Входное напряжение", "V", "voltage", "measurement", "mdi:transmission-tower"),
        ("output_voltage", "Выходное напряжение", "V", "voltage", "measurement", "mdi:power-socket-eu"),
        ("battery_voltage", "Напряжение батареи", "V", "voltage", "measurement", "mdi:battery-charging"),
        ("battery_level", "Заряд батареи", "%", "battery", "measurement", None),
        ("load_percent", "Нагрузка", "%", None, "measurement", "mdi:gauge"),
        ("frequency", "Частота сети", "Hz", "frequency", "measurement", "mdi:sine-wave"),
        ("temperature", "Температура", "°C", "temperature", "measurement", None),
        ("ac_status", "Статус сети 220V", None, None, None, "mdi:power-plug"),
        ("ups_status", "Статус ИБП", None, None, None, "mdi:information-outline"),
    ]

    for key, name, unit, dev_class, state_class, icon in sensors:
        topic = f"{DISCOVERY_PREFIX}/sensor/{DEVICE_ID}/{key}/config"
        payload = {
            "name": f"SVC UPS {name}",
            "unique_id": f"{DEVICE_ID}_{key}",
            "state_topic": f"{BASE_TOPIC}/state",
            "value_template": f"{{{{ value_json.{key} }}}}",
            "device": DEVICE_INFO,
        }
        if unit:
            payload["unit_of_measurement"] = unit
        if dev_class:
            payload["device_class"] = dev_class
        if state_class:
            payload["state_class"] = state_class
        if icon:
            payload["icon"] = icon

        client.publish(topic, json.dumps(payload), retain=True)

    # 2. БИНАРНЫЕ СЕНСОРЫ
    binary_sensors = [
        ("ac_power", "Сеть 220V подключена", "plug", None),
        ("battery_low", "Батарея разряжена", "battery", None),
        ("boost_buck", "Стабилизатор AVR активен", None, "mdi:sine-wave"),
        ("ups_problem", "Авария / Отказ ИБП", "problem", None),
        ("test_in_progress", "Самотестирование активно", None, "mdi:progress-check"),
    ]

    for key, name, dev_class, icon in binary_sensors:
        topic = f"{DISCOVERY_PREFIX}/binary_sensor/{DEVICE_ID}/{key}/config"
        payload = {
            "name": f"SVC UPS {name}",
            "unique_id": f"{DEVICE_ID}_{key}",
            "state_topic": f"{BASE_TOPIC}/state",
            "value_template": f"{{{{ 'ON' if value_json.{key} else 'OFF' }}}}",
            "device": DEVICE_INFO,
        }
        if dev_class:
            payload["device_class"] = dev_class
        if icon:
            payload["icon"] = icon

        client.publish(topic, json.dumps(payload), retain=True)

    # 3. ПЕРЕКЛЮЧАТЕЛЬ БИПЕРА (SWITCH)
    switch_topic = f"{DISCOVERY_PREFIX}/switch/{DEVICE_ID}/beeper/config"
    switch_payload = {
        "name": "SVC UPS Пищалка (Beeper)",
        "unique_id": f"{DEVICE_ID}_beeper",
        "state_topic": f"{BASE_TOPIC}/state",
        "value_template": f"{{{{ 'ON' if value_json.beeper else 'OFF' }}}}",
        "command_topic": f"{BASE_TOPIC}/beeper/set",
        "payload_on": "ON",
        "payload_off": "OFF",
        "icon": "mdi:volume-high",
        "device": DEVICE_INFO,
    }
    client.publish(switch_topic, json.dumps(switch_payload), retain=True)

    # 4. КНОПКА СЕЛФ-ТЕСТА (BUTTON)
    button_topic = f"{DISCOVERY_PREFIX}/button/{DEVICE_ID}/self_test/config"
    button_payload = {
        "name": "SVC UPS Запустить самотестирование (10 сек)",
        "unique_id": f"{DEVICE_ID}_self_test",
        "command_topic": f"{BASE_TOPIC}/self_test/set",
        "payload_press": "PRESS",
        "icon": "mdi:battery-heart-variant",
        "device": DEVICE_INFO,
    }
    client.publish(button_topic, json.dumps(button_payload), retain=True)

    # 5. КНОПКА ОТМЕНЫ ТЕСТА (BUTTON)
    cancel_topic = f"{DISCOVERY_PREFIX}/button/{DEVICE_ID}/cancel_test/config"
    cancel_payload = {
        "name": "SVC UPS Отменить тестирование",
        "unique_id": f"{DEVICE_ID}_cancel_test",
        "command_topic": f"{BASE_TOPIC}/cancel_test/set",
        "payload_press": "PRESS",
        "icon": "mdi:cancel",
        "device": DEVICE_INFO,
    }
    client.publish(cancel_topic, json.dumps(cancel_payload), retain=True)
    logger.info("MQTT Discovery опубликован. Устройства и сущности созданы в Home Assistant!")


def main():
    parser = argparse.ArgumentParser(description="SVC UPS to Home Assistant MQTT Bridge")
    parser.add_argument("--mqtt-host", default=os.getenv("MQTT_HOST", "127.0.0.1"), help="MQTT Broker host")
    parser.add_argument("--mqtt-port", type=int, default=int(os.getenv("MQTT_PORT", 1883)), help="MQTT Broker port")
    parser.add_argument("--mqtt-user", default=os.getenv("MQTT_USER", ""), help="MQTT username")
    parser.add_argument("--mqtt-password", default=os.getenv("MQTT_PASS", ""), help="MQTT password")
    parser.add_argument("--device", default=os.getenv("UPS_DEVICE", "/dev/usb/hiddev0"), help="Path to hiddev device")
    parser.add_argument("--interval", type=int, default=int(os.getenv("POLL_INTERVAL", 10)), help="Polling interval in seconds")

    args = parser.parse_args()

    hw = SvcUpsHardware(dev_path=args.device)
    if not hw.open():
        logger.error(f"Не удалось открыть устройство {args.device}")
        sys.exit(1)

    client = mqtt.Client()
    if args.mqtt_user:
        client.username_pw_set(args.mqtt_user, args.mqtt_password)

    def on_connect(c, userdata, flags, rc):
        if rc == 0:
            logger.info("Подключено к MQTT брокеру!")
            publish_discovery(c)
            # Подписка на команды
            c.subscribe(f"{BASE_TOPIC}/beeper/set")
            c.subscribe(f"{BASE_TOPIC}/self_test/set")
            c.subscribe(f"{BASE_TOPIC}/cancel_test/set")
        else:
            logger.error(f"Ошибка подключения к MQTT, код: {rc}")

    def on_message(c, userdata, msg):
        topic = msg.topic
        payload = msg.payload.decode("utf-8", errors="ignore").strip()
        logger.info(f"Получена команда из Home Assistant: {topic} -> {payload}")

        if topic.endswith("/beeper/set"):
            hw.toggle_beeper()
        elif topic.endswith("/self_test/set"):
            hw.start_self_test()
        elif topic.endswith("/cancel_test/set"):
            hw.cancel_self_test()

    client.on_connect = on_connect
    client.on_message = on_message

    logger.info(f"Подключение к MQTT брокеру {args.mqtt_host}:{args.mqtt_port}...")
    try:
        client.connect(args.mqtt_host, args.mqtt_port, 60)
        client.loop_start()
    except Exception as e:
        logger.error(f"Не удалось подключиться к MQTT: {e}")
        sys.exit(1)

    stop_event = threading.Event()

    def handle_sig(sig, frame):
        stop_event.set()

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    logger.info("Запуск основного цикла опроса ИБП...")
    while not stop_event.is_set():
        try:
            r = hw.query_status()
            if r:
                ac_online = not bool(r.status & St.AC_FAIL)
                batt_low = bool(r.status & St.LOW_BATT)
                bypass = bool(r.status & St.BYPASS)
                ups_fail = bool(r.status & St.UPS_FAIL)
                test = bool(r.status & St.TEST)
                beeper = bool(r.status & St.BEEPER)

                batt_pct = 100.0
                if r.dc is not None:
                    batt_pct = max(0.0, min(100.0, ((r.dc - 10.5) / (13.6 - 10.5)) * 100.0))

                telemetry = {
                    "input_voltage": r.ac_in or 0.0,
                    "output_voltage": r.ac_out or 0.0,
                    "load_percent": r.load or 0.0,
                    "frequency": r.freq or 50.0,
                    "battery_voltage": r.dc or 13.6,
                    "battery_level": round(batt_pct, 1),
                    "temperature": r.temp or 25.0,
                    "ac_status": "Normal" if ac_online else "Utility Fail",
                    "battery_status": "Battery Low" if batt_low else "Normal",
                    "ups_status": "Failed" if ups_fail else "Normal",
                    "ac_power": ac_online,
                    "battery_low": batt_low,
                    "boost_buck": bypass,
                    "ups_problem": ups_fail,
                    "test_in_progress": test,
                    "beeper": beeper,
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
                }

                client.publish(f"{BASE_TOPIC}/state", json.dumps(telemetry))
                logger.debug(f"Опубликовано: {r.ac_in}V in, {r.ac_out}V out, {r.dc}V bat")
        except Exception as e:
            logger.error(f"Ошибка опроса: {e}")

        stop_event.wait(args.interval)

    client.loop_stop()
    client.disconnect()
    hw.close()
    logger.info("Мост MQTT остановлен.")


if __name__ == "__main__":
    main()

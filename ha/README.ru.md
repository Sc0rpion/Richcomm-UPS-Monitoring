# Интеграция SVC / Richcomm UPS для Home Assistant

Полнофункциональная интеграция ИБП SVC (чип Richcomm Technologies, VID `0x0925`, PID `0x1234`) для **Home Assistant**.

Поддерживает чтение телеметрии по протоколу Megatec Q1 (`FRAMED 0xA0`), аппаратное управление звуковой сигнализацией (**Бипер**) и запуск **самотестирования батареи** из интерфейса Home Assistant.

---

## Что создаётся в Home Assistant

### 📊 Числовые сенсоры (Sensors):
* `sensor.svc_ups_vkhodnoe_napriazhenie` — Входное сетевое напряжение (Вольт)
* `sensor.svc_ups_vykhodnoe_napriazhenie` — Выходное напряжение ИБП (Вольт)
* `sensor.svc_ups_napriazhenie_batarei` — Напряжение батареи (Вольт)
* `sensor.svc_ups_zariad_batarei` — Оценочный заряд АКБ (%)
* `sensor.svc_ups_nagruzka` — Текущая нагрузка (%)
* `sensor.svc_ups_chastota_seti` — Частота сети (Гц)
* `sensor.svc_ups_temperatura` — Температура инвертора (°C)
* `sensor.svc_ups_status_seti_220v` — Текстовый статус (`Normal` / `Utility Fail`)
* `sensor.svc_ups_status_ibp` — Текстовый статус ИБП

### ⚡ Бинарные сенсоры (Binary Sensors):
* `binary_sensor.svc_ups_set_220v_podkliuchena` — Наличие сети 220V (ON / OFF)
* `binary_sensor.svc_ups_batareia_razriazhena` — Предупреждение о низком заряде (Battery Low)
* `binary_sensor.svc_ups_stabilizator_avr` — Активность стабилизатора Boost/Buck
* `binary_sensor.svc_ups_avariia_otkaz_ibp` — Авария инвертора
* `binary_sensor.svc_ups_samotestirovanie_aktivno` — Идет ли в данный момент селф-тест

### 🔊 Переключатель (Switch):
* `switch.svc_ups_zvukovaia_signalizatsiia_biper` — **Кнопка отключения звука (Mute/Unmute)**.
  - Посылает команду `Q\r`. 
  - Позволяет выключить писк ИБП ночью или автоматизировать выключение звука через сценарии HA!

### 🔘 Кнопки управления (Buttons):
* `button.svc_ups_zapustit_samotestirovanie_10_sek` — **Кнопка запуска селф-теста**.
  - Посылает команду `T\r` для 10-секундной проверки АКБ под нагрузкой.
* `button.svc_ups_otmenit_testirovanie` — **Кнопка экстренной отмены теста**.
  - Посылает команду `CT\r`.

---

## Способ 1: Установка через HACS (Рекомендуется, с автообновлениями)

Интеграция полностью поддерживает **HACS (Home Assistant Community Store)** как пользовательский репозиторий.

### Добавление репозитория в HACS:
1. Откройте **HACS** в боковой панели Home Assistant.
2. Перейдите в раздел **Интеграции (Integrations)**.
3. В правом верхнем углу нажмите меню из трёх точек (**⋮**) &rarr; выберите **Пользовательские репозитории (Custom repositories)**.
4. В поле **Репозиторий** вставьте URL вашего репозитория GitHub (или путь):
   ```text
   https://github.com/ВАШ_ПОЛЬЗОВАТЕЛЬ/ha-svc-ups
   ```
5. В поле **Тип** выберите **Интеграция (Integration)**.
6. Нажмите **Добавить (Add)**.
7. Найдите в списке интеграцию **SVC / Richcomm UPS** и нажмите **Загрузить (Download)**.
8. Перезапустите Home Assistant.

### Обновление:
HACS будет автоматически отслеживать новые релизы интеграции по тэгам (например, `v1.1.0`), проверять совместимость через `manifest.json` и `hacs.json` и предлагать обновление в один клик.

---

## Способ 2: Ручная установка (Custom Component)
*Используйте этот вариант, если HACS не установлен.*

1. Скопируйте каталог `custom_components/svc_ups` в папку конфигурации вашего Home Assistant:
   ```bash
   cp -r /ha/custom_components/svc_ups /config/custom_components/
   ```
2. Перезапустите Home Assistant:
   **Панель управления &rarr; Инструменты разработчика &rarr; Перезапустить**.
3. Добавьте интеграцию через графический интерфейс:
   **Настройки &rarr; Устройства и службы &rarr; Добавить интеграцию &rarr; поиск: "SVC Richcomm UPS"**.
4. Укажите путь к устройству (по умолчанию `/dev/usb/hiddev0`) и интервал опроса (10 сек).
5. Все устройства, сущности и кнопки сразу появятся в вашей системе!

---

## Способ 3: Через MQTT Auto-Discovery (Docker / Synology / Proxmox)
*Идеально, если ИБП подключен к Synology NAS или серверу виртуализации, а сам Home Assistant работает на другом компьютере.*

1. В папке `/ha/` отредактируйте `docker-compose.yml`:
   Укажите IP-адрес вашего MQTT брокера (Mosquitto), логин и пароль.
2. Запустите мост:
   ```bash
   docker-compose up -d
   ```
3. Мост сам подключится к брокеру, зарегистрирует карточку устройства и все сущности через стандартный механизм **Home Assistant MQTT Discovery**.
4. В интерфейсе Home Assistant появится готовое устройство **SVC Richcomm UPS** со всеми сенсорами, переключателем и кнопками.

---

## Пример автоматизации в Home Assistant (`automations.yaml`)

### Оповещение в Telegram при пропадании 220V:
```yaml
alias: "ИБП: Сбой сети 220V"
trigger:
  - platform: state
    entity_id: binary_sensor.svc_ups_set_220v_podkliuchena
    to: "off"
action:
  - service: notify.telegram
    data:
      message: "⚠️ Внимание! Пропало питание 220V. Сервер перешёл на питание от батареи ИБП (заряд: {{ states('sensor.svc_ups_zariad_batarei') }}%)."
```

### Автоматическое выключение пищалки ночью:
```yaml
alias: "ИБП: Тихий режим на ночь"
trigger:
  - platform: time
    at: "23:00:00"
action:
  - service: switch.turn_off
    target:
      entity_id: switch.svc_ups_zvukovaia_signalizatsiia_biper
```

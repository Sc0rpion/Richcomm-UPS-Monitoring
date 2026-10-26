"""Constants for SVC Richcomm UPS Home Assistant Integration."""

DOMAIN = "svc_ups"
DEFAULT_NAME = "SVC UPS"
DEFAULT_DEVICE = "/dev/usb/hiddev0"
DEFAULT_BAUD = 2400
DEFAULT_SCAN_INTERVAL = 10
MIN_SCAN_INTERVAL = 5
MAX_SCAN_INTERVAL = 300

# Столько неудачных опросов подряд переживаем на прошлых значениях,
# прежде чем пометить сущности недоступными (одиночный сбой HID не должен
# дёргать автоматизации по "unavailable").
MAX_SOFT_FAILURES = 3

CONF_DEVICE = "device"
CONF_BAUD = "baud"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_PHYS = "phys"

ATTR_ENTRY_ID = "entry_id"

SERVICE_TOGGLE_BEEPER = "toggle_beeper"
SERVICE_START_SELF_TEST = "start_self_test"
SERVICE_CANCEL_SELF_TEST = "cancel_self_test"

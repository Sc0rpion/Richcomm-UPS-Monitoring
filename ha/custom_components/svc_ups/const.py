"""Constants for SVC Richcomm UPS Home Assistant Integration."""

DOMAIN = "svc_ups"
DEFAULT_NAME = "SVC UPS"
DEFAULT_DEVICE = "/dev/usb/hiddev0"
DEFAULT_BAUD = 2400
DEFAULT_SCAN_INTERVAL = 10

CONF_DEVICE = "device"
CONF_BAUD = "baud"
CONF_SCAN_INTERVAL = "scan_interval"

ATTR_INPUT_VOLTAGE = "input_voltage"
ATTR_OUTPUT_VOLTAGE = "output_voltage"
ATTR_LOAD_PERCENT = "load_percent"
ATTR_FREQUENCY = "frequency"
ATTR_BATTERY_VOLTAGE = "battery_voltage"
ATTR_BATTERY_LEVEL = "battery_level"
ATTR_TEMPERATURE = "temperature"
ATTR_AC_STATUS = "ac_status"
ATTR_BATTERY_STATUS = "battery_status"
ATTR_UPS_STATUS = "ups_status"
ATTR_BOOST_BUCK = "boost_buck"
ATTR_BEEPER = "beeper"
ATTR_TEST_IN_PROGRESS = "test_in_progress"
ATTR_RAW_RESPONSE = "raw_response"

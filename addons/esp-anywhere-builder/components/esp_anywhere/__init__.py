"""ESP Anywhere ESPHome external component."""

import re

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import binary_sensor, esp32, http_request, mqtt, sensor, switch, text, time
from esphome.const import CONF_ID

DEPENDENCIES = ["mqtt", "time", "http_request"]
AUTO_LOAD = ["json", "sensor", "binary_sensor", "switch", "text"]

CONF_MQTT_ID = "mqtt_id"
CONF_TIME_ID = "time_id"
CONF_HTTP_REQUEST_ID = "http_request_id"
CONF_TENANT_ID = "tenant_id"
CONF_DEVICE_ID = "device_id"
CONF_FRIENDLY_NAME = "friendly_name"
CONF_MANUFACTURER = "manufacturer"
CONF_MODEL = "model"
CONF_HARDWARE_PROFILE = "hardware_profile"
CONF_FIRMWARE_VERSION = "firmware_version"
CONF_UPDATE_MANIFEST_URL = "update_manifest_url"
CONF_SENSORS = "sensors"
CONF_BINARY_SENSORS = "binary_sensors"
CONF_SWITCHES = "switches"
CONF_TEXTS = "texts"
CONF_ENTITY_ID = "entity_id"
CONF_NAME = "name"
CONF_UNIT = "unit_of_measurement"
CONF_DEVICE_CLASS = "device_class"
CONF_AUTO_REGISTER_ENTITIES = "auto_register_entities"
CONF_MANAGED_PROVISIONING = "managed_provisioning"
CONF_CLAIM_URL = "claim_url"
CONF_RELAY_HOST = "relay_host"

SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")


def validate_slug(value):
    """Validate a protocol tenant, device or hardware identifier."""
    value = cv.string_strict(value)
    if SLUG_PATTERN.fullmatch(value) is None:
        raise cv.Invalid("must contain 3-64 lowercase letters, digits, '_' or '-'")
    return value


SLUG = validate_slug

BRIDGED_SENSOR_SCHEMA = cv.Schema({
    cv.Required(CONF_ID): cv.use_id(sensor.Sensor),
    cv.Required(CONF_ENTITY_ID): SLUG,
    cv.Required(CONF_NAME): cv.string_strict,
    cv.Optional(CONF_UNIT, default=""): cv.string_strict,
    cv.Optional(CONF_DEVICE_CLASS, default=""): cv.string_strict,
})

BRIDGED_BINARY_SENSOR_SCHEMA = cv.Schema({
    cv.Required(CONF_ID): cv.use_id(binary_sensor.BinarySensor),
    cv.Required(CONF_ENTITY_ID): SLUG,
    cv.Required(CONF_NAME): cv.string_strict,
    cv.Optional(CONF_DEVICE_CLASS, default=""): cv.string_strict,
})

BRIDGED_SWITCH_SCHEMA = cv.Schema({
    cv.Required(CONF_ID): cv.use_id(switch.Switch),
    cv.Required(CONF_ENTITY_ID): SLUG,
    cv.Required(CONF_NAME): cv.string_strict,
})

BRIDGED_TEXT_SCHEMA = cv.Schema({
    cv.Required(CONF_ID): cv.use_id(text.Text),
    cv.Required(CONF_ENTITY_ID): SLUG,
    cv.Required(CONF_NAME): cv.string_strict,
})

esp_anywhere_ns = cg.esphome_ns.namespace("esp_anywhere")
EspAnywhereComponent = esp_anywhere_ns.class_("EspAnywhereComponent", cg.Component)

CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.declare_id(EspAnywhereComponent),
        cv.GenerateID(CONF_MQTT_ID): cv.use_id(mqtt.MQTTClientComponent),
        cv.GenerateID(CONF_TIME_ID): cv.use_id(time.RealTimeClock),
        cv.GenerateID(CONF_HTTP_REQUEST_ID): cv.use_id(http_request.HttpRequestComponent),
        cv.Required(CONF_TENANT_ID): SLUG,
        cv.Required(CONF_DEVICE_ID): SLUG,
        cv.Required(CONF_FRIENDLY_NAME): cv.string_strict,
        cv.Optional(CONF_MANUFACTURER, default="ESP Anywhere"): cv.string_strict,
        cv.Required(CONF_MODEL): cv.string_strict,
        cv.Optional(CONF_HARDWARE_PROFILE, default="esphome_native"): SLUG,
        cv.Required(CONF_FIRMWARE_VERSION): cv.string_strict,
        cv.Optional(CONF_UPDATE_MANIFEST_URL, default=""): cv.string_strict,
        cv.Optional(CONF_SENSORS, default=[]): cv.ensure_list(BRIDGED_SENSOR_SCHEMA),
        cv.Optional(CONF_BINARY_SENSORS, default=[]): cv.ensure_list(BRIDGED_BINARY_SENSOR_SCHEMA),
        cv.Optional(CONF_SWITCHES, default=[]): cv.ensure_list(BRIDGED_SWITCH_SCHEMA),
        cv.Optional(CONF_TEXTS, default=[]): cv.ensure_list(BRIDGED_TEXT_SCHEMA),
        cv.Optional(CONF_AUTO_REGISTER_ENTITIES, default=False): cv.boolean,
        cv.Optional(CONF_MANAGED_PROVISIONING, default=False): cv.boolean,
        cv.Optional(CONF_CLAIM_URL, default=""): cv.string_strict,
        cv.Optional(CONF_RELAY_HOST, default=""): cv.string_strict,
    }
).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    """Register the ESP Anywhere component."""
    var = cg.new_Pvariable(config[CONF_ID])
    esp32.add_idf_sdkconfig_option("CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE", True)
    # ESP32-C3 hardware crypto can abort when MQTT/TLS and pull-OTA hash concurrently.
    esp32.add_idf_sdkconfig_option("CONFIG_MBEDTLS_HARDWARE_AES", False)
    esp32.add_idf_sdkconfig_option("CONFIG_MBEDTLS_HARDWARE_SHA", False)
    await cg.register_component(var, config)
    mqtt_client = await cg.get_variable(config[CONF_MQTT_ID])
    clock = await cg.get_variable(config[CONF_TIME_ID])
    http_client = await cg.get_variable(config[CONF_HTTP_REQUEST_ID])
    cg.add(var.set_mqtt_client(mqtt_client))
    cg.add(var.set_clock(clock))
    cg.add(var.set_http_client(http_client))
    cg.add(var.set_tenant_id(config[CONF_TENANT_ID]))
    cg.add(var.set_device_id(config[CONF_DEVICE_ID]))
    cg.add(var.set_friendly_name(config[CONF_FRIENDLY_NAME]))
    cg.add(var.set_manufacturer(config[CONF_MANUFACTURER]))
    cg.add(var.set_model(config[CONF_MODEL]))
    cg.add(var.set_hardware_profile(config[CONF_HARDWARE_PROFILE]))
    cg.add(var.set_firmware_version(config[CONF_FIRMWARE_VERSION]))
    cg.add(var.set_update_manifest_url(config[CONF_UPDATE_MANIFEST_URL]))
    cg.add(var.set_auto_register_entities(config[CONF_AUTO_REGISTER_ENTITIES]))
    cg.add(var.set_managed_provisioning(config[CONF_MANAGED_PROVISIONING]))
    cg.add(var.set_claim_url(config[CONF_CLAIM_URL]))
    cg.add(var.set_relay_host(config[CONF_RELAY_HOST]))
    for item in config[CONF_SENSORS]:
        entity = await cg.get_variable(item[CONF_ID])
        cg.add(var.add_sensor(entity, item[CONF_ENTITY_ID], item[CONF_NAME], item[CONF_UNIT], item[CONF_DEVICE_CLASS]))
    for item in config[CONF_BINARY_SENSORS]:
        entity = await cg.get_variable(item[CONF_ID])
        cg.add(var.add_binary_sensor(entity, item[CONF_ENTITY_ID], item[CONF_NAME], item[CONF_DEVICE_CLASS]))
    for item in config[CONF_SWITCHES]:
        entity = await cg.get_variable(item[CONF_ID])
        cg.add(var.add_switch(entity, item[CONF_ENTITY_ID], item[CONF_NAME]))
    for item in config[CONF_TEXTS]:
        entity = await cg.get_variable(item[CONF_ID])
        cg.add(var.add_text(entity, item[CONF_ENTITY_ID], item[CONF_NAME]))

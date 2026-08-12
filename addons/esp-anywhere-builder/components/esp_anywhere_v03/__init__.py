"""ESP Anywhere v0.3 claim/WSS bridge with a codegen entity registry."""

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import binary_sensor, button, esp32, http_request, number, sensor, switch, text
from esphome.const import CONF_ID, CONF_INTERNAL, CONF_NAME
from esphome.core import CORE, CoroPriority, coroutine_with_priority

DEPENDENCIES = ["wifi", "http_request"]
AUTO_LOAD = ["json", "sensor", "binary_sensor", "switch", "button", "number", "text"]

CONF_HTTP_REQUEST_ID = "http_request_id"
CONF_FRIENDLY_NAME = "friendly_name"
CONF_MODEL = "model"
CONF_FIRMWARE_VERSION = "firmware_version"
CONF_OTA_BASE_URL = "ota_base_url"
CONF_AUTO_REGISTER_ENTITIES = "auto_register_entities"

esp_anywhere_ns = cg.esphome_ns.namespace("esp_anywhere_v03")
EspAnywhereV03Component = esp_anywhere_ns.class_("EspAnywhereV03Component", cg.Component)

CONFIG_SCHEMA = cv.Schema({
    cv.GenerateID(): cv.declare_id(EspAnywhereV03Component),
    cv.GenerateID(CONF_HTTP_REQUEST_ID): cv.use_id(http_request.HttpRequestComponent),
    cv.Required(CONF_FRIENDLY_NAME): cv.string_strict,
    cv.Required(CONF_MODEL): cv.string_strict,
    cv.Required(CONF_FIRMWARE_VERSION): cv.string_strict,
    cv.Required(CONF_OTA_BASE_URL): cv.url,
    cv.Optional(CONF_AUTO_REGISTER_ENTITIES, default=True): cv.boolean,
}).extend(cv.COMPONENT_SCHEMA)


def _entries(domain):
    value = CORE.config.get(domain, [])
    return value if isinstance(value, list) else [value]


def _identity(item):
    entity_id = str(item[CONF_ID])
    name = item.get(CONF_NAME, entity_id) or entity_id
    internal = item.get(CONF_INTERNAL, False)
    return entity_id, name, internal, not internal


@coroutine_with_priority(CoroPriority.FINAL)
async def to_code(config):
    """Generate a hardware-neutral v0.3 bridge and explicit entity table."""
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    http_client = await cg.get_variable(config[CONF_HTTP_REQUEST_ID])
    cg.add(var.set_http_client(http_client))
    cg.add(var.set_friendly_name(config[CONF_FRIENDLY_NAME]))
    cg.add(var.set_model(config[CONF_MODEL]))
    cg.add(var.set_firmware_version(config[CONF_FIRMWARE_VERSION]))
    cg.add(var.set_ota_base_url(config[CONF_OTA_BASE_URL]))
    cg.add(var.set_auto_register_entities(config[CONF_AUTO_REGISTER_ENTITIES]))
    if config[CONF_AUTO_REGISTER_ENTITIES]:
        for domain, add_method in (
            ("sensor", var.add_sensor),
            ("binary_sensor", var.add_binary_sensor),
            ("switch", var.add_switch),
            ("button", var.add_button),
            ("number", var.add_number),
            ("text", var.add_text),
        ):
            for item in _entries(domain):
                entity = await cg.get_variable(item[CONF_ID])
                cg.add(add_method(entity, *_identity(item)))
    cg.add_library("links2004/WebSockets", "2.7.3")
    cg.add_library("WiFi", None)
    cg.add_library("HTTPClient", None)
    cg.add_library("NetworkClientSecure", None)
    cg.add_library("rweather/Crypto", "0.4.0")
    esp32.add_idf_sdkconfig_option("CONFIG_MBEDTLS_CERTIFICATE_BUNDLE", True)
    esp32.add_idf_sdkconfig_option("CONFIG_MBEDTLS_CERTIFICATE_BUNDLE_DEFAULT_FULL", True)
    esp32.add_idf_sdkconfig_option("CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE", True)

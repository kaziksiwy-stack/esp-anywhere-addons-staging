#include "esp_anywhere_v03.h"

#include <Arduino.h>
#include <cstdio>
#include <cstring>

#ifdef USE_LOGGER_UART_SELECTION_USB_SERIAL_JTAG
#include <driver/usb_serial_jtag.h>
#endif

#include "esphome/components/json/json_util.h"
#include "esphome/components/wifi/wifi_component.h"
#include "esphome/core/log.h"

namespace esphome::esp_anywhere_v03 {

static const char *const TAG = "esp_anywhere.v03";

void EspAnywhereV03Component::setup() {
  this->identity_store_.begin();
  this->identity_store_.load();
  this->ota_manager_.setup(&this->identity_store_, &this->transport_, this->firmware_version_, this->ota_base_url_);
  this->registry_.set_state_callback([this]() { this->publish_state_(); });
  this->registry_.subscribe();
  this->transport_.setup([this](WStype_t type, uint8_t *payload, size_t length) {
    this->websocket_event_(type, payload, length);
  });
  if (!this->identity_store_.loaded()) this->emit_stage_("detected", "ok");
}

void EspAnywhereV03Component::loop() {
  this->read_serial_();
  this->ota_manager_.loop();
  if (!wifi::global_wifi_component->is_connected() || !this->identity_store_.loaded()) return;
  auto &identity = this->identity_store_.get();
  if (identity.token[0] == '\0') {
    if (millis() >= this->next_claim_attempt_) this->attempt_claim_();
    return;
  }
  if (!this->transport_.started()) this->transport_.connect(identity);
  this->transport_.loop();
}

void EspAnywhereV03Component::dump_config() {
  ESP_LOGCONFIG(TAG, "ESP Anywhere v0.3 claim/WSS bridge");
  ESP_LOGCONFIG(TAG, "  MQTT: disabled");
  ESP_LOGCONFIG(TAG, "  Identity provisioned: %s", YESNO(this->identity_store_.loaded()));
  ESP_LOGCONFIG(TAG, "  Registry entries: %u", this->registry_.size());
  ESP_LOGCONFIG(TAG, "  Exposed sensors: %u", this->registry_.count(EntityDomain::SENSOR, true));
  ESP_LOGCONFIG(TAG, "  Exposed binary sensors: %u", this->registry_.count(EntityDomain::BINARY_SENSOR, true));
  ESP_LOGCONFIG(TAG, "  Exposed switches: %u", this->registry_.count(EntityDomain::SWITCH, true));
  ESP_LOGCONFIG(TAG, "  Exposed buttons: %u", this->registry_.count(EntityDomain::BUTTON, true));
  ESP_LOGCONFIG(TAG, "  Exposed numbers: %u", this->registry_.count(EntityDomain::NUMBER, true));
  ESP_LOGCONFIG(TAG, "  Exposed texts: %u", this->registry_.count(EntityDomain::TEXT, true));
}

float EspAnywhereV03Component::get_setup_priority() const { return setup_priority::AFTER_WIFI + 10.0f; }

void EspAnywhereV03Component::read_serial_() {
  const auto consume = [this](char ch) {
    if (ch == '\n') {
      if (!this->serial_buffer_.empty() && this->serial_buffer_.size() <= 1024) {
        json::parse_json(this->serial_buffer_, [this](JsonObject root) -> bool {
          return strcmp(root["type"] | "", "provision") == 0 && this->apply_provisioning_(root);
        });
      }
      this->serial_buffer_.clear();
    } else if (ch != '\r' && this->serial_buffer_.size() < 1024) {
      this->serial_buffer_.push_back(ch);
    }
  };
#ifdef USE_LOGGER_UART_SELECTION_USB_SERIAL_JTAG
  uint8_t input[64];
  int count;
  while ((count = usb_serial_jtag_read_bytes(input, sizeof(input), 0)) > 0)
    for (int index = 0; index < count; index++) consume(static_cast<char>(input[index]));
#else
  while (Serial.available()) consume(static_cast<char>(Serial.read()));
#endif
}

bool EspAnywhereV03Component::apply_provisioning_(JsonObject root) {
  const char *ssid = root["wifi_ssid"] | "";
  const char *password = root["wifi_password"] | "";
  const char *relay = root["relay_url"] | "";
  const char *installation = root["installation_id"] | "";
  const char *activation = root["activation_code"] | "";
  const char *device = root["device_id"] | "";
  const char *name = root["device_name"] | "ESP Anywhere ESPHome";
  const bool preserve_wifi = root["preserve_wifi"] | false;
  auto &identity = this->identity_store_.get();
  if ((!preserve_wifi && (strlen(ssid) == 0 || strlen(ssid) > 32 || strlen(password) > 64)) ||
      strncmp(relay, "https://", 8) != 0 ||
      strlen(relay) >= sizeof(identity.relay_url) || !IdentityStore::valid_identifier(installation) ||
      !IdentityStore::valid_identifier(device) || strlen(activation) >= sizeof(identity.activation_code) ||
      strncmp(activation, installation, strlen(installation)) != 0 || strlen(name) >= sizeof(identity.device_name)) {
    this->emit_stage_("config_saved", "error", "invalid_configuration");
    return false;
  }
  this->identity_store_.clear_runtime();
  auto &updated = this->identity_store_.get();
  strlcpy(updated.relay_url, relay, sizeof(updated.relay_url));
  strlcpy(updated.installation_id, installation, sizeof(updated.installation_id));
  strlcpy(updated.device_id, device, sizeof(updated.device_id));
  strlcpy(updated.device_name, name, sizeof(updated.device_name));
  strlcpy(updated.activation_code, activation, sizeof(updated.activation_code));
  if (!this->identity_store_.save()) {
    this->emit_stage_("config_saved", "error", "preferences_write_failed");
    return false;
  }
  if (!preserve_wifi) wifi::global_wifi_component->save_wifi_sta(ssid, password);
  wifi::global_wifi_component->enable();
  this->emit_stage_("config_saved", "ok");
  return true;
}

void EspAnywhereV03Component::attempt_claim_() {
  this->next_claim_attempt_ = millis() + 5000;
  auto &identity = this->identity_store_.get();
  if (identity.activation_code[0] == '\0') return;
  const std::string url = std::string(identity.relay_url) + "/claim";
  const std::string body = json::build_json([&identity](JsonObject root) {
    root["code"] = identity.activation_code;
    root["device_id"] = identity.device_id;
  });
  std::vector<http_request::Header> headers{{"Content-Type", "application/json"}};
  auto response = this->http_client_->post(url, body, headers);
  if (response == nullptr || response->status_code != 200 || response->content_length <= 0 ||
      response->content_length > 1024) {
    if (response != nullptr) response->end();
    this->emit_stage_("claim_success", "error", "worker_unavailable");
    return;
  }
  std::string payload(response->content_length, '\0');
  size_t offset = 0;
  while (offset < payload.size()) {
    const int count = response->read(reinterpret_cast<uint8_t *>(payload.data() + offset), payload.size() - offset);
    if (count <= 0) break;
    offset += count;
  }
  response->end();
  bool valid = false;
  if (offset == payload.size()) valid = json::parse_json(payload, [this](JsonObject root) -> bool {
    auto &stored = this->identity_store_.get();
    const char *role = root["role"] | "";
    const char *token = root["token"] | "";
    const char *installation = root["installation_id"] | "";
    if (strcmp(role, "device") != 0 || strcmp(installation, stored.installation_id) != 0 || strlen(token) < 32 ||
        strlen(token) >= sizeof(stored.token)) return false;
    strlcpy(stored.token, token, sizeof(stored.token));
    stored.activation_code[0] = '\0';
    return this->identity_store_.save();
  });
  this->emit_stage_("claim_success", valid ? "ok" : "error", valid ? nullptr : "invalid_claim_response");
}

void EspAnywhereV03Component::websocket_event_(WStype_t type, uint8_t *payload, size_t length) {
  if (type == WStype_CONNECTED) {
    this->emit_stage_("worker_connected", "ok");
    const bool discovery_sent = this->publish_discovery_();
    const bool state_sent = this->publish_state_();
    if (discovery_sent && state_sent) this->ota_manager_.on_protocol_healthy();
  } else if (type == WStype_TEXT && length <= 4096) {
    json::parse_json(std::string(reinterpret_cast<char *>(payload), length), [this](JsonObject root) -> bool {
      if (strcmp(root["type"] | "", "command") == 0) this->handle_command_(root);
      else if (strcmp(root["type"] | "", "ota_start") == 0) this->ota_manager_.handle_start(root);
      return true;
    });
  }
}

void EspAnywhereV03Component::handle_command_(JsonObject root) {
  const char *command = root["command"] | "";
  const char *command_id = root["command_id"] | "";
  if (strlen(command_id) < 16) return;
  if (strcmp(command, "request_state") == 0) {
    this->publish_state_();
    this->publish_command_result_(command_id, "succeeded");
    return;
  }
  if (strcmp(command, "set_entity") != 0) {
    this->publish_command_result_(command_id, "rejected", "unsupported_command");
    return;
  }
  JsonObject parameters = root["parameters"].as<JsonObject>();
  const char *entity_id = parameters["entity_id"] | "";
  const CommandStatus result = this->registry_.set_entity(entity_id, parameters["value"]);
  const char *error = command_status_code(result);
  this->publish_command_result_(command_id, result == CommandStatus::SUCCEEDED ? "succeeded" : "rejected", error);
  if (result == CommandStatus::SUCCEEDED) this->publish_state_();
}

bool EspAnywhereV03Component::publish_discovery_() {
  if (!this->transport_.connected()) return false;
  const auto &identity = this->identity_store_.get();
  const std::string out = json::build_json([this, &identity](JsonObject root) {
    root["type"] = "discovery";
    JsonObject payload = root["payload"].to<JsonObject>();
    payload["name"] = identity.device_name[0] ? identity.device_name : this->friendly_name_;
    payload["manufacturer"] = "ESP Anywhere";
    payload["model"] = this->model_;
    payload["hardware_profile"] = "esphome_native";
    payload["firmware_version"] = this->firmware_version_;
    payload["update_manifest_url"] = this->ota_base_url_ + "/ota/stable/manifest.json";
    const auto &ota = this->ota_manager_.capabilities();
    JsonObject ota_capabilities = payload["ota_capabilities"].to<JsonObject>();
    ota_capabilities["chip_family"] = ota.chip_family;
    ota_capabilities["tier"] = ota.tier;
    ota_capabilities["layout_sha256"] = ota.layout_sha256;
    ota_capabilities["app_slot_count"] = ota.app_slot_count;
    ota_capabilities["app_slot_size"] = ota.app_slot_size;
    ota_capabilities["has_otadata"] = ota.has_otadata;
    ota_capabilities["automatic_rollback"] = ota.automatic_rollback;
    this->registry_.append_discovery(payload["entities"].to<JsonArray>());
  });
  const bool sent = this->transport_.send(out);
  if (sent) this->emit_stage_("discovery_sent", "ok");
  return sent;
}

bool EspAnywhereV03Component::publish_state_() {
  if (!this->transport_.connected()) return false;
  const std::string out = json::build_json([this](JsonObject root) {
    root["type"] = "state";
    this->registry_.append_state(root["payload"].to<JsonObject>());
  });
  return this->transport_.send(out);
}

void EspAnywhereV03Component::publish_command_result_(const std::string &command_id, const char *state,
                                                       const char *code) {
  if (!this->transport_.connected()) return;
  const std::string out = json::build_json([&](JsonObject root) {
    root["type"] = "command_result";
    root["command_id"] = command_id;
    root["state"] = state;
    if (code != nullptr) root["error"]["code"] = code;
  });
  this->transport_.send(out);
}

void EspAnywhereV03Component::emit_stage_(const char *stage, const char *status, const char *error) {
  std::string out = json::build_json([&](JsonObject root) {
    root["stage"] = stage;
    root["status"] = status;
    if (error != nullptr) root["error"] = error;
  });
#ifdef USE_LOGGER_UART_SELECTION_USB_SERIAL_JTAG
  out.push_back('\n');
  fwrite(out.data(), 1, out.size(), stdout);
#else
  Serial.println(out.c_str());
#endif
}

}  // namespace esphome::esp_anywhere_v03

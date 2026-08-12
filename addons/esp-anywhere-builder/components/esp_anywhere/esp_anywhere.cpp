#include "esp_anywhere.h"

#include <cstdio>
#include <cinttypes>
#include <cstring>

#include "mbedtls/sha256.h"
#include "esp_ota_ops.h"
#include "esphome/components/http_request/http_request.h"

#include "esphome/core/application.h"
#include "esphome/core/defines.h"
#include "esphome/core/helpers.h"
#include "esphome/core/log.h"

namespace esphome::esp_anywhere {

static const char *const TAG = "esp_anywhere";
static const char *const PROTOCOL_VERSION = "1.0";
static constexpr uint32_t CREDENTIALS_MAGIC = 0x45415731;
static constexpr uint32_t CREDENTIALS_PREFERENCE_KEY = 0x9F4A04C1;

void EspAnywhereComponent::setup() {
  this->boot_id_ = this->make_uuid_();
  this->initialize_rollback_state_();
  if (this->managed_provisioning_) {
    this->credentials_pref_ = global_preferences->make_preference<StoredCredentials>(CREDENTIALS_PREFERENCE_KEY, true);
    if (!this->load_credentials_()) {
      ESP_LOGI(TAG, "Waiting for ESP Anywhere claim; local ESPHome remains active");
    }
  }

  // PoC registry: ESPHome owns entities; Anywhere only retains public pointers.
  if (this->auto_register_entities_) {
    for (auto *entity : App.get_sensors()) {
      if (!entity->is_internal()) this->add_sensor(entity, entity->get_object_id(), entity->get_name(),
                                                   entity->get_unit_of_measurement(), entity->get_device_class());
    }
    for (auto *entity : App.get_binary_sensors()) {
      if (!entity->is_internal()) this->add_binary_sensor(entity, entity->get_object_id(), entity->get_name(),
                                                          entity->get_device_class());
    }
    for (auto *entity : App.get_switches()) {
      if (!entity->is_internal()) this->add_switch(entity, entity->get_object_id(), entity->get_name());
    }
#ifdef USE_TEXT
    for (auto *entity : App.get_texts()) {
      if (!entity->is_internal()) this->add_text(entity, entity->get_object_id(), entity->get_name());
    }
#endif
  }

  mqtt::MQTTMessage will{
      .topic = this->topic_("presence"),
      .payload = str_sprintf(
          "{\"protocol_version\":\"1.0\",\"device_id\":\"%s\",\"online\":false,"
          "\"boot_id\":null,\"reason\":\"lost\"}",
          this->device_id_.c_str()),
      .qos = 1,
      .retain = true,
  };
  this->mqtt_client_->set_last_will(std::move(will));
  this->mqtt_client_->set_on_connect([this](bool session_present) { this->on_mqtt_connect_(); });
  this->mqtt_client_->subscribe_json(
      this->topic_("command"),
      [this](const std::string &topic, JsonObject root) { this->handle_command_(root); }, 1);
  for (auto &item : this->sensors_) {
    item.entity->add_on_state_callback([this, id = item.id](float value) { this->publish_entity_state_(id, value); });
  }
  for (auto &item : this->binary_sensors_) {
    item.entity->add_on_state_callback([this, id = item.id](bool value) { this->publish_entity_state_(id, value); });
  }
  for (auto &item : this->switches_) {
    item.entity->add_on_state_callback([this, id = item.id](bool value) { this->publish_entity_state_(id, value); });
  }
  for (auto &item : this->texts_) {
    item.entity->add_on_state_callback([this, id = item.id](const std::string &value) { this->publish_entity_state_(id, value); });
  }
}

bool EspAnywhereComponent::load_credentials_() {
  StoredCredentials stored{};
  if (!this->credentials_pref_.load(&stored) || stored.magic != CREDENTIALS_MAGIC || stored.device_id[0] == '\0' ||
      stored.username[0] == '\0' || stored.password[0] == '\0' || this->relay_host_.empty()) {
    return false;
  }
  this->device_id_ = stored.device_id;
  this->mqtt_client_->set_broker_address(this->relay_host_);
  this->mqtt_client_->set_username(stored.username);
  this->mqtt_client_->set_password(stored.password);
  this->mqtt_client_->set_client_id(stored.client_id);
  this->mqtt_enable_pending_ = true;
  ESP_LOGI(TAG, "Loaded ESP Anywhere identity from preferences");
  return true;
}

bool EspAnywhereComponent::save_credentials_(const StoredCredentials &credentials) {
  if (!this->credentials_pref_.save(&credentials) || !global_preferences->sync()) {
    ESP_LOGE(TAG, "Could not persist ESP Anywhere credentials");
    return false;
  }
  return true;
}

bool EspAnywhereComponent::provision_claim(const std::string &claim) {
  if (!this->managed_provisioning_ || claim.size() < 16 || claim.size() > 128 || this->claim_url_.empty()) {
    ESP_LOGW(TAG, "Rejected invalid provisioning request");
    return false;
  }
  const std::string body = json::build_json([&claim](JsonObject root) { root["claim"] = claim; });
  std::vector<http_request::Header> headers{{"Content-Type", "application/json"}};
  auto response = this->http_client_->post(this->claim_url_, body, headers);
  if (response == nullptr || response->status_code != 200 || response->content_length == 0 ||
      response->content_length > 1024) {
    if (response != nullptr) response->end();
    ESP_LOGW(TAG, "Claim exchange failed");
    return false;
  }
  std::string payload(response->content_length, '\0');
  size_t offset = 0;
  while (offset < payload.size()) {
    int count = response->read(reinterpret_cast<uint8_t *>(payload.data() + offset), payload.size() - offset);
    if (count <= 0) break;
    offset += count;
  }
  response->end();
  if (offset != payload.size()) {
    ESP_LOGW(TAG, "Claim response was incomplete");
    return false;
  }
  StoredCredentials stored{};
  bool parsed = json::parse_json(payload, [&stored](JsonObject root) -> bool {
    const char *device_id = root["device_id"] | "";
    const char *username = root["username"] | "";
    const char *password = root["password"] | "";
    const char *client_id = root["client_id"] | "";
    if (strlen(device_id) >= sizeof(stored.device_id) || strlen(username) >= sizeof(stored.username) ||
        strlen(password) >= sizeof(stored.password) || strlen(client_id) >= sizeof(stored.client_id) ||
        device_id[0] == '\0' || username[0] == '\0' || password[0] == '\0' || client_id[0] == '\0') return false;
    stored.magic = CREDENTIALS_MAGIC;
    strlcpy(stored.device_id, device_id, sizeof(stored.device_id));
    strlcpy(stored.username, username, sizeof(stored.username));
    strlcpy(stored.password, password, sizeof(stored.password));
    strlcpy(stored.client_id, client_id, sizeof(stored.client_id));
    return true;
  });
  if (!parsed || !this->save_credentials_(stored)) return false;
  this->mqtt_client_->disable();
  bool loaded = this->load_credentials_();
  ESP_LOGI(TAG, "ESP Anywhere claim completed");
  return loaded;
}

void EspAnywhereComponent::add_sensor(sensor::Sensor *sensor, const std::string &id, const std::string &name,
                                      const std::string &unit, const std::string &device_class) {
  this->sensors_.push_back({sensor, id, name, unit, device_class});
}

void EspAnywhereComponent::add_binary_sensor(binary_sensor::BinarySensor *sensor, const std::string &id,
                                              const std::string &name, const std::string &device_class) {
  this->binary_sensors_.push_back({sensor, id, name, device_class});
}

void EspAnywhereComponent::add_switch(switch_::Switch *sw, const std::string &id, const std::string &name) {
  this->switches_.push_back({sw, id, name});
}

void EspAnywhereComponent::add_text(text::Text *text, const std::string &id, const std::string &name) {
  this->texts_.push_back({text, id, name});
}

void EspAnywhereComponent::loop() {
  if (this->mqtt_enable_pending_) {
    this->mqtt_enable_pending_ = false;
    this->mqtt_client_->enable();
  }
  if (this->discovery_pending_ && this->mqtt_client_->is_connected() && this->clock_->utcnow().is_valid()) {
    this->publish_presence_(true, "connected");
    this->publish_discovery_();
    this->publish_state_();
    this->discovery_pending_ = false;
    this->confirm_running_firmware_();
  }
  if (this->firmware_confirmation_pending_ && millis() - this->firmware_confirmation_started_at_ > 120000) {
    ESP_LOGE(TAG, "New firmware failed health checks; rolling back");
    this->publish_presence_(false, "firmware_health_failed");
    delay(100);
    esp_ota_mark_app_invalid_rollback_and_reboot();
  }
}

void EspAnywhereComponent::dump_config() {
  ESP_LOGCONFIG(TAG, "ESP Anywhere:");
  ESP_LOGCONFIG(TAG, "  Protocol version: %s", PROTOCOL_VERSION);
  ESP_LOGCONFIG(TAG, "  Tenant: %s", this->tenant_id_.c_str());
  ESP_LOGCONFIG(TAG, "  Device ID: %s", this->device_id_.c_str());
  ESP_LOGCONFIG(TAG, "  Hardware profile: %s", this->hardware_profile_.c_str());
  ESP_LOGCONFIG(TAG, "  Firmware version: %s", this->firmware_version_.c_str());
  ESP_LOGCONFIG(TAG, "  MQTT topic root: %s", this->topic_("").c_str());
  ESP_LOGCONFIG(TAG, "  Firmware confirmation pending: %s", YESNO(this->firmware_confirmation_pending_));
}

void EspAnywhereComponent::initialize_rollback_state_() {
  const esp_partition_t *running = esp_ota_get_running_partition();
  esp_ota_img_states_t state;
  if (running != nullptr && esp_ota_get_state_partition(running, &state) == ESP_OK &&
      state == ESP_OTA_IMG_PENDING_VERIFY) {
    this->firmware_confirmation_pending_ = true;
    this->firmware_confirmation_started_at_ = millis();
    ESP_LOGW(TAG, "Firmware is pending verification; waiting for MQTT health check");
  }
}

void EspAnywhereComponent::confirm_running_firmware_() {
  if (!this->firmware_confirmation_pending_) return;
  if (esp_ota_mark_app_valid_cancel_rollback() != ESP_OK) {
    ESP_LOGE(TAG, "Could not confirm the running firmware");
    return;
  }
  this->firmware_confirmation_pending_ = false;
  ESP_LOGI(TAG, "Running firmware confirmed after successful MQTT health check");
  this->publish_update_progress_("boot-validation", "confirmed", 100.0f);
}

void EspAnywhereComponent::on_shutdown() {
  if (this->mqtt_client_ != nullptr && this->mqtt_client_->is_connected()) {
    this->publish_presence_(false, "shutdown");
  }
}

float EspAnywhereComponent::get_setup_priority() const { return setup_priority::AFTER_WIFI + 10.0f; }

void EspAnywhereComponent::on_mqtt_connect_() { this->discovery_pending_ = true; }

void EspAnywhereComponent::handle_command_(JsonObject root) {
  const char *protocol = root["protocol_version"] | "";
  const char *device_id = root["device_id"] | "";
  const char *command_id = root["command_id"] | "";
  const char *command = root["command"] | "";
  const char *expires_at = root["expires_at"] | "";

  if (strcmp(protocol, PROTOCOL_VERSION) != 0 || strcmp(device_id, this->device_id_.c_str()) != 0 ||
      strlen(command_id) != 36) {
    ESP_LOGW(TAG, "Rejected malformed command envelope");
    return;
  }
  if (!this->validate_command_time_(expires_at)) {
    this->publish_command_result_(command_id, "rejected", "expired", "Command has expired or clock is invalid");
    return;
  }
  if (this->command_seen_(command_id)) {
    this->publish_command_result_(command_id, "rejected", "duplicate", "Command was already processed");
    return;
  }

  this->publish_command_result_(command_id, "accepted");
  if (strcmp(command, "request_state") == 0) {
    this->publish_state_();
    this->publish_command_result_(command_id, "succeeded");
    return;
  }
  if (strcmp(command, "restart") == 0) {
    this->publish_command_result_(command_id, "succeeded");
    this->set_timeout("esp_anywhere_restart", 500, []() { App.safe_reboot(); });
    return;
  }
  if (strcmp(command, "install_update") == 0) {
    if (this->install_update_(command_id, root["parameters"].as<JsonObject>())) {
      this->publish_command_result_(command_id, "succeeded");
      this->set_timeout("esp_anywhere_ota_reboot", 750, []() { App.safe_reboot(); });
    }
    return;
  }
  if (strcmp(command, "set_entity") == 0) {
    JsonObject parameters = root["parameters"].as<JsonObject>();
    const char *entity_id = parameters["entity_id"] | "";
    if (parameters["value"].is<bool>()) {
      for (auto &item : this->switches_) {
        if (item.id != entity_id) continue;
        item.entity->control(parameters["value"].as<bool>());
        this->publish_command_result_(command_id, "succeeded");
        return;
      }
    }
    if (parameters["value"].is<const char *>()) {
      const char *value = parameters["value"].as<const char *>();
      if (value == nullptr || strlen(value) > 64) {
        this->publish_command_result_(command_id, "rejected", "invalid_value", "Text must be at most 64 bytes");
        return;
      }
      for (auto &item : this->texts_) {
        if (item.id != entity_id) continue;
        auto call = item.entity->make_call(); call.set_value(value); call.perform();
        this->publish_command_result_(command_id, "succeeded");
        return;
      }
    }
    this->publish_command_result_(command_id, "rejected", "unknown_entity", "Entity or value type is not writable");
    return;
  }

  this->publish_command_result_(command_id, "rejected", "unsupported_command",
                                "Command is not implemented by this firmware");
}

bool EspAnywhereComponent::install_update_(const std::string &command_id, JsonObject payload) {
  const char *url = payload["firmware_url"] | "";
  const char *sha256 = payload["sha256"] | "";
  const char *version = payload["version"] | "";
  const size_t expected_size = payload["size"] | 0;
  if (strncmp(url, "https://", 8) != 0 || strlen(sha256) != 64 || expected_size == 0 || expected_size > 4 * 1024 * 1024 ||
      strlen(version) == 0) {
    this->publish_command_result_(command_id, "rejected", "invalid_update", "Invalid OTA parameters");
    return false;
  }

  this->publish_update_progress_(command_id, "downloading", 0.0f);
  auto container = this->http_client_->get(url);
  if (container == nullptr || container->status_code != 200 || container->content_length != expected_size) {
    if (container != nullptr) container->end();
    this->publish_command_result_(command_id, "rejected", "download_failed", "HTTPS response or size is invalid");
    return false;
  }

  const esp_partition_t *update_partition = esp_ota_get_next_update_partition(nullptr);
  esp_ota_handle_t update_handle = 0;
  if (update_partition == nullptr || esp_ota_begin(update_partition, expected_size, &update_handle) != ESP_OK) {
    container->end();
    this->publish_command_result_(command_id, "rejected", "ota_begin_failed", "Cannot prepare OTA partition");
    return false;
  }

  mbedtls_sha256_context digest;
  mbedtls_sha256_init(&digest);
  mbedtls_sha256_starts(&digest, 0);
  uint8_t buffer[1024];
  uint32_t last_data = millis();
  uint32_t last_report = 0;
  bool failed = false;
  while (container->get_bytes_read() < expected_size) {
    const int count = container->read(buffer, sizeof(buffer));
    const auto read_result = http_request::http_read_loop_result(count, last_data, this->http_client_->get_timeout(),
                                                                 container->is_read_complete());
    if (read_result == http_request::HttpReadLoopResult::RETRY) continue;
    if (read_result != http_request::HttpReadLoopResult::DATA ||
        esp_ota_write(update_handle, buffer, count) != ESP_OK) {
      failed = true;
      break;
    }
    mbedtls_sha256_update(&digest, buffer, count);
    App.feed_wdt();
    yield();
    if (millis() - last_report >= 1000) {
      last_report = millis();
      this->publish_update_progress_(command_id, "downloading",
                                     container->get_bytes_read() * 100.0f / expected_size);
    }
  }
  const size_t received_size = container->get_bytes_read();
  container->end();

  unsigned char hash[32];
  mbedtls_sha256_finish(&digest, hash);
  mbedtls_sha256_free(&digest);
  char actual[65];
  for (size_t i = 0; i < sizeof(hash); i++) snprintf(actual + i * 2, 3, "%02x", hash[i]);
  actual[64] = '\0';

  if (failed || received_size != expected_size || strcmp(actual, sha256) != 0) {
    esp_ota_abort(update_handle);
    this->publish_command_result_(command_id, "rejected", failed ? "write_failed" : "sha256_mismatch",
                                  "Firmware download verification failed");
    return false;
  }
  this->publish_update_progress_(command_id, "installing", 100.0f);
  if (esp_ota_end(update_handle) != ESP_OK || esp_ota_set_boot_partition(update_partition) != ESP_OK) {
    esp_ota_abort(update_handle);
    this->publish_command_result_(command_id, "rejected", "ota_finalize_failed", "Firmware could not be activated");
    return false;
  }
  this->publish_update_progress_(command_id, "rebooting", 100.0f);
  return true;
}

void EspAnywhereComponent::publish_update_progress_(const std::string &command_id, const char *state, float progress,
                                                     const char *error_code) {
  const std::string sent_at = this->utc_now_();
  this->mqtt_client_->publish_json(this->topic_("ota/progress"), [this, command_id, state, progress, error_code, sent_at](JsonObject root) {
    root["protocol_version"] = PROTOCOL_VERSION;
    root["device_id"] = this->device_id_;
    root["boot_id"] = this->boot_id_;
    root["sent_at"] = sent_at;
    root["command_id"] = command_id;
    root["state"] = state;
    root["progress"] = progress;
    if (error_code != nullptr) root["error_code"] = error_code;
  }, 1, false);
}

void EspAnywhereComponent::publish_presence_(bool online, const char *reason) {
  this->mqtt_client_->publish_json(
      this->topic_("presence"),
      [this, online, reason](JsonObject root) {
        root["protocol_version"] = PROTOCOL_VERSION;
        root["device_id"] = this->device_id_;
        root["online"] = online;
        root["boot_id"] = online ? this->boot_id_.c_str() : nullptr;
        root["reason"] = reason;
      },
      1, true);
}

void EspAnywhereComponent::publish_discovery_() {
  const std::string sent_at = this->utc_now_();
  const std::string message_id = this->make_uuid_();
  this->mqtt_client_->publish_json(
      this->topic_("discovery"),
      [this, sent_at, message_id](JsonObject root) {
        root["protocol_version"] = PROTOCOL_VERSION;
        root["message_id"] = message_id;
        root["device_id"] = this->device_id_;
        root["boot_id"] = this->boot_id_;
        root["sent_at"] = sent_at;
        JsonObject payload = root["payload"].to<JsonObject>();
        payload["name"] = this->friendly_name_;
        payload["manufacturer"] = this->manufacturer_;
        payload["model"] = this->model_;
        payload["hardware_profile"] = this->hardware_profile_;
        payload["firmware_version"] = this->firmware_version_;
        if (!this->update_manifest_url_.empty()) payload["update_manifest_url"] = this->update_manifest_url_;
        JsonArray entities = payload["entities"].to<JsonArray>();
        JsonObject restart = entities.add<JsonObject>();
        restart["id"] = "restart";
        restart["platform"] = "button";
        restart["name"] = "Restart";
        restart["entity_category"] = "config";
        restart["enabled_by_default"] = true;
        restart["read_only"] = false;
        restart["command"] = "restart";
        for (const auto &item : this->sensors_) {
          JsonObject entity = entities.add<JsonObject>();
          entity["id"] = item.id;
          entity["platform"] = "sensor";
          entity["name"] = item.name;
          entity["read_only"] = true;
          if (!item.unit.empty()) entity["unit_of_measurement"] = item.unit;
          if (!item.device_class.empty()) entity["device_class"] = item.device_class;
        }
        for (const auto &item : this->binary_sensors_) {
          JsonObject entity = entities.add<JsonObject>();
          entity["id"] = item.id;
          entity["platform"] = "binary_sensor";
          entity["name"] = item.name;
          entity["read_only"] = true;
          if (!item.device_class.empty()) entity["device_class"] = item.device_class;
        }
        for (const auto &item : this->switches_) {
          JsonObject entity = entities.add<JsonObject>();
          entity["id"] = item.id;
          entity["platform"] = "switch";
          entity["name"] = item.name;
          entity["read_only"] = false;
        }
        for (const auto &item : this->texts_) {
          JsonObject entity = entities.add<JsonObject>();
          entity["id"] = item.id; entity["platform"] = "text"; entity["name"] = item.name; entity["read_only"] = false;
        }
      },
      1, true);
}

void EspAnywhereComponent::publish_state_() {
  const std::string sent_at = this->utc_now_();
  const std::string message_id = this->make_uuid_();
  this->mqtt_client_->publish_json(
      this->topic_("state"),
      [this, sent_at, message_id](JsonObject root) {
        root["protocol_version"] = PROTOCOL_VERSION;
        root["message_id"] = message_id;
        root["device_id"] = this->device_id_;
        root["boot_id"] = this->boot_id_;
        root["sent_at"] = sent_at;
        JsonObject payload = root["payload"].to<JsonObject>();
        for (const auto &item : this->sensors_) {
          if (item.entity->has_state()) payload[item.id] = item.entity->state;
        }
        for (const auto &item : this->binary_sensors_) {
          if (item.entity->has_state()) payload[item.id] = item.entity->state;
        }
        for (const auto &item : this->switches_) payload[item.id] = item.entity->state;
        for (const auto &item : this->texts_) payload[item.id] = item.entity->state;
      },
      1, true);
}

void EspAnywhereComponent::publish_entity_state_(const std::string &entity_id, float value) {
  if (!this->mqtt_client_->is_connected()) return;
  this->mqtt_client_->publish_json(this->topic_(("state/" + entity_id).c_str()), [this, value](JsonObject root) {
    root["protocol_version"] = PROTOCOL_VERSION;
    root["device_id"] = this->device_id_;
    JsonObject payload = root["payload"].to<JsonObject>();
    payload["value"] = value;
  }, 1, true);
}

void EspAnywhereComponent::publish_entity_state_(const std::string &entity_id, bool value) {
  if (!this->mqtt_client_->is_connected()) return;
  this->mqtt_client_->publish_json(this->topic_(("state/" + entity_id).c_str()), [this, value](JsonObject root) {
    root["protocol_version"] = PROTOCOL_VERSION;
    root["device_id"] = this->device_id_;
    JsonObject payload = root["payload"].to<JsonObject>();
    payload["value"] = value;
  }, 1, true);
}

void EspAnywhereComponent::publish_entity_state_(const std::string &entity_id, const std::string &value) {
  if (!this->mqtt_client_->is_connected()) return;
  this->mqtt_client_->publish_json(this->topic_(("state/" + entity_id).c_str()), [this, value](JsonObject root) {
    root["protocol_version"] = PROTOCOL_VERSION; root["device_id"] = this->device_id_;
    root["payload"].to<JsonObject>()["value"] = value;
  }, 1, true);
}

void EspAnywhereComponent::publish_command_result_(const std::string &command_id, const char *state,
                                                    const char *error_code, const char *error_message) {
  const std::string sent_at = this->utc_now_();
  const std::string message_id = this->make_uuid_();
  this->mqtt_client_->publish_json(
      this->topic_("command/result"),
      [this, command_id, state, error_code, error_message, sent_at, message_id](JsonObject root) {
        root["protocol_version"] = PROTOCOL_VERSION;
        root["message_id"] = message_id;
        root["device_id"] = this->device_id_;
        root["boot_id"] = this->boot_id_;
        root["sent_at"] = sent_at;
        root["command_id"] = command_id;
        root["state"] = state;
        root["payload"].to<JsonObject>();
        if (error_code != nullptr) {
          JsonObject error = root["error"].to<JsonObject>();
          error["code"] = error_code;
          error["message"] = error_message;
        }
      },
      1, false);
}

bool EspAnywhereComponent::validate_command_time_(const char *expires_at) const {
  const auto now = this->clock_->utcnow();
  if (!now.is_valid() || expires_at == nullptr || strlen(expires_at) != 20) return false;
  unsigned int year, month, day, hour, minute, second;
  if (sscanf(expires_at, "%4u-%2u-%2uT%2u:%2u:%2uZ", &year, &month, &day, &hour, &minute, &second) != 6)
    return false;
  ESPTime expiry{};
  expiry.year = year;
  expiry.month = month;
  expiry.day_of_month = day;
  expiry.hour = hour;
  expiry.minute = minute;
  expiry.second = second;
  if (!expiry.fields_in_range(false, false)) return false;
  expiry.recalc_timestamp_utc(false);
  return expiry.timestamp >= now.timestamp && expiry.timestamp <= now.timestamp + 300;
}

bool EspAnywhereComponent::command_seen_(const std::string &command_id) {
  for (const auto &seen : this->recent_command_ids_) {
    if (seen == command_id) return true;
  }
  this->recent_command_ids_[this->recent_command_index_] = command_id;
  this->recent_command_index_ = (this->recent_command_index_ + 1) % this->recent_command_ids_.size();
  return false;
}

std::string EspAnywhereComponent::make_uuid_() const {
  uint32_t a = random_uint32();
  uint32_t b = random_uint32();
  uint32_t c = random_uint32();
  uint32_t d = random_uint32();
  b = (b & 0xFFFF0FFFUL) | 0x00004000UL;
  c = (c & 0x3FFFFFFFUL) | 0x80000000UL;
  return str_sprintf("%08" PRIx32 "-%04" PRIx32 "-%04" PRIx32 "-%04" PRIx32 "-%04" PRIx32 "%08" PRIx32,
                     a, b >> 16, b & 0xFFFF, c >> 16, c & 0xFFFF, d);
}

std::string EspAnywhereComponent::utc_now_() const {
  char buffer[24];
  auto now = this->clock_->utcnow();
  now.strftime(buffer, sizeof(buffer), "%Y-%m-%dT%H:%M:%SZ");
  return buffer;
}

std::string EspAnywhereComponent::topic_(const char *suffix) const {
  std::string root = "esp-anywhere/v1/" + this->tenant_id_ + "/" + this->device_id_;
  if (suffix != nullptr && suffix[0] != '\0') root += "/" + std::string(suffix);
  return root;
}

}  // namespace esphome::esp_anywhere

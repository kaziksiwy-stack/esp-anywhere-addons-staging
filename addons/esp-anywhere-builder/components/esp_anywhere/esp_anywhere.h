#pragma once

#include <array>

#include "esphome/components/binary_sensor/binary_sensor.h"
#include "esphome/components/json/json_util.h"
#include "esphome/components/http_request/http_request.h"
#include "esphome/components/mqtt/mqtt_client.h"
#include "esphome/components/sensor/sensor.h"
#include "esphome/components/switch/switch.h"
#include "esphome/components/text/text.h"
#include "esphome/components/time/real_time_clock.h"
#include "esphome/core/component.h"
#include "esphome/core/preferences.h"

namespace esphome::esp_anywhere {

class EspAnywhereComponent : public Component {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;
  void on_shutdown() override;
  float get_setup_priority() const override;

  void set_mqtt_client(mqtt::MQTTClientComponent *mqtt_client) { this->mqtt_client_ = mqtt_client; }
  void set_clock(time::RealTimeClock *clock) { this->clock_ = clock; }
  void set_http_client(http_request::HttpRequestComponent *http_client) { this->http_client_ = http_client; }
  void set_tenant_id(const std::string &value) { this->tenant_id_ = value; }
  void set_device_id(const std::string &value) { this->device_id_ = value; }
  void set_friendly_name(const std::string &value) { this->friendly_name_ = value; }
  void set_manufacturer(const std::string &value) { this->manufacturer_ = value; }
  void set_model(const std::string &value) { this->model_ = value; }
  void set_hardware_profile(const std::string &value) { this->hardware_profile_ = value; }
  void set_firmware_version(const std::string &value) { this->firmware_version_ = value; }
  void set_update_manifest_url(const std::string &value) { this->update_manifest_url_ = value; }
  void set_auto_register_entities(bool value) { this->auto_register_entities_ = value; }
  void set_managed_provisioning(bool value) { this->managed_provisioning_ = value; }
  void set_claim_url(const std::string &value) { this->claim_url_ = value; }
  void set_relay_host(const std::string &value) { this->relay_host_ = value; }
  bool provision_claim(const std::string &claim);
  void add_sensor(sensor::Sensor *sensor, const std::string &id, const std::string &name,
                  const std::string &unit, const std::string &device_class);
  void add_binary_sensor(binary_sensor::BinarySensor *sensor, const std::string &id, const std::string &name,
                         const std::string &device_class);
  void add_switch(switch_::Switch *sw, const std::string &id, const std::string &name);
  void add_text(text::Text *text, const std::string &id, const std::string &name);

 protected:
  void on_mqtt_connect_();
  void handle_command_(JsonObject root);
  void publish_presence_(bool online, const char *reason);
  void publish_discovery_();
  void publish_state_();
  void publish_command_result_(const std::string &command_id, const char *state,
                               const char *error_code = nullptr, const char *error_message = nullptr);
  bool install_update_(const std::string &command_id, JsonObject payload);
  void publish_update_progress_(const std::string &command_id, const char *state, float progress,
                                const char *error_code = nullptr);
  void initialize_rollback_state_();
  void confirm_running_firmware_();
  bool validate_command_time_(const char *expires_at) const;
  bool command_seen_(const std::string &command_id);
  std::string make_uuid_() const;
  std::string utc_now_() const;
  std::string topic_(const char *suffix) const;
  void publish_entity_state_(const std::string &entity_id, float value);
  void publish_entity_state_(const std::string &entity_id, bool value);
  void publish_entity_state_(const std::string &entity_id, const std::string &value);

  struct BridgedSensor {
    sensor::Sensor *entity;
    std::string id;
    std::string name;
    std::string unit;
    std::string device_class;
  };
  struct BridgedBinarySensor {
    binary_sensor::BinarySensor *entity;
    std::string id;
    std::string name;
    std::string device_class;
  };
  struct BridgedSwitch {
    switch_::Switch *entity;
    std::string id;
    std::string name;
  };
  struct BridgedText {
    text::Text *entity;
    std::string id;
    std::string name;
  };

  mqtt::MQTTClientComponent *mqtt_client_{nullptr};
  time::RealTimeClock *clock_{nullptr};
  http_request::HttpRequestComponent *http_client_{nullptr};
  std::string tenant_id_;
  std::string device_id_;
  std::string friendly_name_;
  std::string manufacturer_;
  std::string model_;
  std::string hardware_profile_;
  std::string firmware_version_;
  std::string update_manifest_url_;
  std::string boot_id_;
  std::array<std::string, 32> recent_command_ids_{};
  size_t recent_command_index_{0};
  bool discovery_pending_{true};
  bool firmware_confirmation_pending_{false};
  bool auto_register_entities_{false};
  bool managed_provisioning_{false};
  bool mqtt_enable_pending_{false};
  std::string claim_url_;
  std::string relay_host_;
  struct StoredCredentials {
    uint32_t magic;
    char device_id[40];
    char username[128];
    char password[128];
    char client_id[80];
  };
  ESPPreferenceObject credentials_pref_;
  bool load_credentials_();
  bool save_credentials_(const StoredCredentials &credentials);
  uint32_t firmware_confirmation_started_at_{0};
  std::vector<BridgedSensor> sensors_;
  std::vector<BridgedBinarySensor> binary_sensors_;
  std::vector<BridgedSwitch> switches_;
  std::vector<BridgedText> texts_;
};

}  // namespace esphome::esp_anywhere

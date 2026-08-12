#pragma once

#include "entity_registry.h"
#include "identity_store.h"
#include "ota_manager.h"
#include "worker_transport.h"

#include "esphome/components/http_request/http_request.h"
#include "esphome/core/component.h"

namespace esphome::esp_anywhere_v03 {

class EspAnywhereV03Component : public Component {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override;

  void set_http_client(http_request::HttpRequestComponent *value) { this->http_client_ = value; }
  void set_friendly_name(const std::string &value) { this->friendly_name_ = value; }
  void set_model(const std::string &value) { this->model_ = value; }
  void set_firmware_version(const std::string &value) { this->firmware_version_ = value; }
  void set_ota_base_url(const std::string &value) { this->ota_base_url_ = value; }
  void set_auto_register_entities(bool value) { this->auto_register_entities_ = value; }

  void add_sensor(sensor::Sensor *entity, const std::string &id, const std::string &name, bool internal, bool exposed) {
    this->registry_.add_sensor(entity, id, name, internal, exposed);
  }
  void add_binary_sensor(binary_sensor::BinarySensor *entity, const std::string &id, const std::string &name,
                         bool internal, bool exposed) {
    this->registry_.add_binary_sensor(entity, id, name, internal, exposed);
  }
  void add_switch(switch_::Switch *entity, const std::string &id, const std::string &name, bool internal, bool exposed) {
    this->registry_.add_switch(entity, id, name, internal, exposed);
  }
  void add_button(button::Button *entity, const std::string &id, const std::string &name, bool internal, bool exposed) {
    this->registry_.add_button(entity, id, name, internal, exposed);
  }
  void add_number(number::Number *entity, const std::string &id, const std::string &name, bool internal, bool exposed) {
    this->registry_.add_number(entity, id, name, internal, exposed);
  }
  void add_text(text::Text *entity, const std::string &id, const std::string &name, bool internal, bool exposed) {
    this->registry_.add_text(entity, id, name, internal, exposed);
  }

 protected:
  void read_serial_();
  bool apply_provisioning_(JsonObject root);
  void attempt_claim_();
  void websocket_event_(WStype_t type, uint8_t *payload, size_t length);
  void handle_command_(JsonObject root);
  bool publish_discovery_();
  bool publish_state_();
  void publish_command_result_(const std::string &command_id, const char *state, const char *code = nullptr);
  void emit_stage_(const char *stage, const char *status, const char *error = nullptr);

  http_request::HttpRequestComponent *http_client_{nullptr};
  IdentityStore identity_store_;
  OtaManager ota_manager_;
  WorkerTransport transport_;
  EntityRegistry registry_;
  std::string friendly_name_;
  std::string model_;
  std::string firmware_version_;
  std::string ota_base_url_;
  std::string serial_buffer_;
  bool auto_register_entities_{true};
  uint32_t next_claim_attempt_{0};
};

}  // namespace esphome::esp_anywhere_v03

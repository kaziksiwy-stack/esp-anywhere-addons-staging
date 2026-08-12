#pragma once

#include <functional>
#include <string>
#include <vector>

#include "esphome/components/binary_sensor/binary_sensor.h"
#include "esphome/components/button/button.h"
#include "esphome/components/number/number.h"
#include "esphome/components/sensor/sensor.h"
#include "esphome/components/switch/switch.h"
#include "esphome/components/text/text.h"
#include "esphome/components/json/json_util.h"

namespace esphome::esp_anywhere_v03 {

enum class EntityDomain : uint8_t { SENSOR, BINARY_SENSOR, SWITCH, BUTTON, NUMBER, TEXT };

struct RegistryEntry {
  EntityDomain domain;
  void *entity;
  std::string id;
  std::string name;
  bool internal;
  bool exposed;
};

enum class CommandStatus : uint8_t { SUCCEEDED, UNKNOWN_ENTITY, READ_ONLY, INVALID_VALUE };

class EntityRegistry {
 public:
  using StateCallback = std::function<void()>;

  void set_state_callback(StateCallback callback) { this->state_callback_ = std::move(callback); }
  void add_sensor(sensor::Sensor *entity, const std::string &id, const std::string &name, bool internal, bool exposed);
  void add_binary_sensor(binary_sensor::BinarySensor *entity, const std::string &id, const std::string &name,
                         bool internal, bool exposed);
  void add_switch(switch_::Switch *entity, const std::string &id, const std::string &name, bool internal, bool exposed);
  void add_button(button::Button *entity, const std::string &id, const std::string &name, bool internal, bool exposed);
  void add_number(number::Number *entity, const std::string &id, const std::string &name, bool internal, bool exposed);
  void add_text(text::Text *entity, const std::string &id, const std::string &name, bool internal, bool exposed);

  void subscribe();
  void append_discovery(JsonArray entities) const;
  void append_state(JsonObject payload) const;
  CommandStatus set_entity(const std::string &id, JsonVariantConst value);
  size_t count(EntityDomain domain, bool exposed_only = false) const;
  size_t size() const { return this->entries_.size(); }

 protected:
  void add_(EntityDomain domain, void *entity, const std::string &id, const std::string &name, bool internal,
            bool exposed);
  static const char *domain_name_(EntityDomain domain);
  void notify_state_() const;

  std::vector<RegistryEntry> entries_;
  StateCallback state_callback_;
};

const char *command_status_code(CommandStatus status);

}  // namespace esphome::esp_anywhere_v03

#include "entity_registry.h"

#include <cmath>

namespace esphome::esp_anywhere_v03 {

void EntityRegistry::add_(EntityDomain domain, void *entity, const std::string &id, const std::string &name,
                          bool internal, bool exposed) {
  this->entries_.push_back({domain, entity, id, name, internal, exposed && !internal});
}

void EntityRegistry::add_sensor(sensor::Sensor *entity, const std::string &id, const std::string &name, bool internal,
                                bool exposed) {
  this->add_(EntityDomain::SENSOR, entity, id, name, internal, exposed);
}
void EntityRegistry::add_binary_sensor(binary_sensor::BinarySensor *entity, const std::string &id,
                                       const std::string &name, bool internal, bool exposed) {
  this->add_(EntityDomain::BINARY_SENSOR, entity, id, name, internal, exposed);
}
void EntityRegistry::add_switch(switch_::Switch *entity, const std::string &id, const std::string &name, bool internal,
                                bool exposed) {
  this->add_(EntityDomain::SWITCH, entity, id, name, internal, exposed);
}
void EntityRegistry::add_button(button::Button *entity, const std::string &id, const std::string &name, bool internal,
                                bool exposed) {
  this->add_(EntityDomain::BUTTON, entity, id, name, internal, exposed);
}
void EntityRegistry::add_number(number::Number *entity, const std::string &id, const std::string &name, bool internal,
                                bool exposed) {
  this->add_(EntityDomain::NUMBER, entity, id, name, internal, exposed);
}
void EntityRegistry::add_text(text::Text *entity, const std::string &id, const std::string &name, bool internal,
                              bool exposed) {
  this->add_(EntityDomain::TEXT, entity, id, name, internal, exposed);
}

void EntityRegistry::notify_state_() const {
  if (this->state_callback_) this->state_callback_();
}

void EntityRegistry::subscribe() {
  for (auto &entry : this->entries_) {
    if (!entry.exposed) continue;
    switch (entry.domain) {
      case EntityDomain::SENSOR:
        static_cast<sensor::Sensor *>(entry.entity)->add_on_state_callback([this](float) { this->notify_state_(); });
        break;
      case EntityDomain::BINARY_SENSOR:
        static_cast<binary_sensor::BinarySensor *>(entry.entity)->add_on_state_callback(
            [this](bool) { this->notify_state_(); });
        break;
      case EntityDomain::SWITCH:
        static_cast<switch_::Switch *>(entry.entity)->add_on_state_callback([this](bool) { this->notify_state_(); });
        break;
      case EntityDomain::NUMBER:
        static_cast<number::Number *>(entry.entity)->add_on_state_callback([this](float) { this->notify_state_(); });
        break;
      case EntityDomain::TEXT:
        static_cast<text::Text *>(entry.entity)->add_on_state_callback(
            [this](const std::string &) { this->notify_state_(); });
        break;
      case EntityDomain::BUTTON:
        break;
    }
  }
}

const char *EntityRegistry::domain_name_(EntityDomain domain) {
  switch (domain) {
    case EntityDomain::SENSOR: return "sensor";
    case EntityDomain::BINARY_SENSOR: return "binary_sensor";
    case EntityDomain::SWITCH: return "switch";
    case EntityDomain::BUTTON: return "button";
    case EntityDomain::NUMBER: return "number";
    case EntityDomain::TEXT: return "text";
  }
  return "unknown";
}

void EntityRegistry::append_discovery(JsonArray entities) const {
  for (const auto &entry : this->entries_) {
    if (!entry.exposed) continue;
    JsonObject out = entities.add<JsonObject>();
    out["id"] = entry.id;
    out["platform"] = domain_name_(entry.domain);
    out["name"] = entry.name;
    out["internal"] = entry.internal;
    const bool read_only = entry.domain == EntityDomain::SENSOR || entry.domain == EntityDomain::BINARY_SENSOR;
    out["read_only"] = read_only;
    if (entry.domain == EntityDomain::SENSOR) {
      auto *entity = static_cast<sensor::Sensor *>(entry.entity);
      const std::string unit = entity->get_unit_of_measurement();
      if (!unit.empty()) out["unit_of_measurement"] = unit;
    } else if (entry.domain == EntityDomain::NUMBER) {
      const auto &traits = static_cast<number::Number *>(entry.entity)->traits;
      out["min_value"] = traits.get_min_value();
      out["max_value"] = traits.get_max_value();
      out["step"] = traits.get_step();
    } else if (entry.domain == EntityDomain::TEXT) {
      const auto &traits = static_cast<text::Text *>(entry.entity)->traits;
      out["min_length"] = traits.get_min_length();
      out["max_length"] = traits.get_max_length();
    } else if (entry.domain == EntityDomain::BUTTON) {
      // Uses the existing set_entity envelope; Worker routing is unchanged.
      out["command"] = "set_entity";
    }
  }
}

void EntityRegistry::append_state(JsonObject payload) const {
  for (const auto &entry : this->entries_) {
    if (!entry.exposed) continue;
    switch (entry.domain) {
      case EntityDomain::SENSOR: {
        auto *entity = static_cast<sensor::Sensor *>(entry.entity);
        if (entity->has_state()) payload[entry.id] = entity->state;
        break;
      }
      case EntityDomain::BINARY_SENSOR: {
        auto *entity = static_cast<binary_sensor::BinarySensor *>(entry.entity);
        if (entity->has_state()) payload[entry.id] = entity->state;
        break;
      }
      case EntityDomain::SWITCH:
        payload[entry.id] = static_cast<switch_::Switch *>(entry.entity)->state;
        break;
      case EntityDomain::NUMBER: {
        const float state = static_cast<number::Number *>(entry.entity)->state;
        if (!std::isnan(state)) payload[entry.id] = state;
        break;
      }
      case EntityDomain::TEXT:
        payload[entry.id] = static_cast<text::Text *>(entry.entity)->state;
        break;
      case EntityDomain::BUTTON:
        break;
    }
  }
}

CommandStatus EntityRegistry::set_entity(const std::string &id, JsonVariantConst value) {
  for (auto &entry : this->entries_) {
    if (!entry.exposed || entry.id != id) continue;
    switch (entry.domain) {
      case EntityDomain::SENSOR:
      case EntityDomain::BINARY_SENSOR:
        return CommandStatus::READ_ONLY;
      case EntityDomain::SWITCH:
        if (!value.is<bool>()) return CommandStatus::INVALID_VALUE;
        static_cast<switch_::Switch *>(entry.entity)->control(value.as<bool>());
        return CommandStatus::SUCCEEDED;
      case EntityDomain::BUTTON:
        if (!value.is<bool>() || !value.as<bool>()) return CommandStatus::INVALID_VALUE;
        static_cast<button::Button *>(entry.entity)->press();
        return CommandStatus::SUCCEEDED;
      case EntityDomain::NUMBER: {
        if (!value.is<float>() || value.is<bool>()) return CommandStatus::INVALID_VALUE;
        const float number_value = value.as<float>();
        const auto &traits = static_cast<number::Number *>(entry.entity)->traits;
        if (std::isnan(number_value) || number_value < traits.get_min_value() || number_value > traits.get_max_value())
          return CommandStatus::INVALID_VALUE;
        auto call = static_cast<number::Number *>(entry.entity)->make_call();
        call.set_value(number_value);
        call.perform();
        return CommandStatus::SUCCEEDED;
      }
      case EntityDomain::TEXT: {
        if (!value.is<const char *>()) return CommandStatus::INVALID_VALUE;
        const std::string text_value = value.as<std::string>();
        const auto &traits = static_cast<text::Text *>(entry.entity)->traits;
        if (text_value.size() < static_cast<size_t>(traits.get_min_length()) ||
            text_value.size() > static_cast<size_t>(traits.get_max_length()))
          return CommandStatus::INVALID_VALUE;
        auto call = static_cast<text::Text *>(entry.entity)->make_call();
        call.set_value(text_value);
        call.perform();
        return CommandStatus::SUCCEEDED;
      }
    }
  }
  return CommandStatus::UNKNOWN_ENTITY;
}

size_t EntityRegistry::count(EntityDomain domain, bool exposed_only) const {
  size_t result = 0;
  for (const auto &entry : this->entries_)
    if (entry.domain == domain && (!exposed_only || entry.exposed)) result++;
  return result;
}

const char *command_status_code(CommandStatus status) {
  switch (status) {
    case CommandStatus::SUCCEEDED: return nullptr;
    case CommandStatus::UNKNOWN_ENTITY: return "unknown_entity";
    case CommandStatus::READ_ONLY: return "read_only";
    case CommandStatus::INVALID_VALUE: return "invalid_value";
  }
  return "invalid_command";
}

}  // namespace esphome::esp_anywhere_v03

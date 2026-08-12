#pragma once

#include <cstdint>
#include <cstring>

#include "esphome/core/preferences.h"

namespace esphome::esp_anywhere_v03 {

struct StoredIdentity {
  uint32_t magic;
  char relay_url[160];
  char installation_id[65];
  char device_id[65];
  char device_name[65];
  char activation_code[96];
  char token[96];
};

class IdentityStore {
 public:
  void begin();
  bool load();
  bool save();
  void clear_runtime();

  StoredIdentity &get() { return this->identity_; }
  const StoredIdentity &get() const { return this->identity_; }
  bool loaded() const { return this->loaded_; }
  void set_loaded(bool value) { this->loaded_ = value; }

  static bool valid_identifier(const char *value);

 protected:
  ESPPreferenceObject preference_;
  StoredIdentity identity_{};
  bool loaded_{false};
};

}  // namespace esphome::esp_anywhere_v03

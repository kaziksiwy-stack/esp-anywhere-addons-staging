#include "identity_store.h"

#include "esphome/core/preferences.h"

namespace esphome::esp_anywhere_v03 {

static constexpr uint32_t IDENTITY_MAGIC = 0x45415733;
static constexpr uint32_t IDENTITY_KEY = 0x7B35C291;

void IdentityStore::begin() {
  this->preference_ = global_preferences->make_preference<StoredIdentity>(IDENTITY_KEY, true);
}

bool IdentityStore::load() {
  memset(&this->identity_, 0, sizeof(this->identity_));
  this->loaded_ = this->preference_.load(&this->identity_) && this->identity_.magic == IDENTITY_MAGIC &&
                  valid_identifier(this->identity_.installation_id) && valid_identifier(this->identity_.device_id) &&
                  strncmp(this->identity_.relay_url, "https://", 8) == 0;
  return this->loaded_;
}

bool IdentityStore::save() {
  this->identity_.magic = IDENTITY_MAGIC;
  const bool saved = this->preference_.save(&this->identity_) && global_preferences->sync();
  if (saved) this->loaded_ = true;
  return saved;
}

void IdentityStore::clear_runtime() {
  memset(&this->identity_, 0, sizeof(this->identity_));
  this->loaded_ = false;
}

bool IdentityStore::valid_identifier(const char *value) {
  if (value == nullptr) return false;
  const size_t length = strlen(value);
  if (length < 3 || length > 64 || value[0] < 'a' || value[0] > 'z') return false;
  for (size_t i = 0; i < length; i++) {
    const char ch = value[i];
    if (!((ch >= 'a' && ch <= 'z') || (ch >= '0' && ch <= '9') || ch == '_' || ch == '-')) return false;
  }
  return true;
}

}  // namespace esphome::esp_anywhere_v03

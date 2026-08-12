#include "ota_manager.h"

// Arduino must not accept a pending image before Anywhere validates protocol health.
extern "C" bool verifyRollbackLater() { return true; }

#include <HTTPClient.h>
#include <NetworkClientSecure.h>
#include <algorithm>
#include <esp_ota_ops.h>
#include <esp_partition.h>
#include <esp_task_wdt.h>
#include <mbedtls/sha256.h>
#include <memory>
#include <vector>

#include "esphome/components/json/json_util.h"
#include "esphome/core/log.h"

namespace esphome::esp_anywhere_v03 {
namespace {
constexpr uint32_t STATE_MAGIC = 0x41574f54;
constexpr uint32_t HEALTH_TIMEOUT_MS = 60000;
constexpr uint32_t TERMINAL_REPORT_INTERVAL_MS = 1000;
constexpr uint8_t TERMINAL_REPORT_COPIES = 3;
constexpr uint32_t DOWNLOAD_TIMEOUT_MS = 15000;
constexpr size_t MAX_MANIFEST_BYTES = 16384;
constexpr const char *COMPONENT_VERSION = "1.1.0";
constexpr const char *PROTOCOL_VERSION = "1.0";
constexpr const char *TAG = "esp_anywhere.ota";
extern const uint8_t x509_crt_bundle[] asm("_binary_x509_crt_bundle_start");
extern const uint8_t x509_crt_bundle_end[] asm("_binary_x509_crt_bundle_end");

const char *compiled_chip_family() {
#if defined(CONFIG_IDF_TARGET_ESP32S3)
  return "ESP32S3";
#elif defined(CONFIG_IDF_TARGET_ESP32C3)
  return "ESP32C3";
#elif defined(CONFIG_IDF_TARGET_ESP32S2)
  return "ESP32S2";
#elif defined(CONFIG_IDF_TARGET_ESP32C6)
  return "ESP32C6";
#else
  return "ESP32";
#endif
}

String sha256_hex(const String &input) {
  uint8_t digest[32];
  mbedtls_sha256(reinterpret_cast<const unsigned char *>(input.c_str()), input.length(), digest, 0);
  char output[65];
  for (size_t index = 0; index < sizeof(digest); index++)
    snprintf(output + index * 2, 3, "%02x", digest[index]);
  return String(output);
}

String endpoint_host(const char *endpoint) {
  String value(endpoint == nullptr ? "" : endpoint);
  if (!value.startsWith("https://")) return {};
  value.remove(0, 8);
  int cut = value.indexOf('/');
  if (cut >= 0) value.remove(cut);
  cut = value.indexOf(':');
  if (cut >= 0) value.remove(cut);
  return value;
}
}  // namespace

RuntimeOtaCapabilities OtaManager::detect_capabilities_() const {
  RuntimeOtaCapabilities result;
  result.chip_family = compiled_chip_family();
  struct Entry { uint8_t type; uint8_t subtype; uint32_t offset; uint32_t size; };
  std::vector<Entry> entries;
  esp_partition_iterator_t iterator =
      esp_partition_find(ESP_PARTITION_TYPE_ANY, ESP_PARTITION_SUBTYPE_ANY, nullptr);
  while (iterator != nullptr) {
    const esp_partition_t *partition = esp_partition_get(iterator);
    const bool ota_app = partition->type == ESP_PARTITION_TYPE_APP &&
                         partition->subtype >= ESP_PARTITION_SUBTYPE_APP_OTA_MIN &&
                         partition->subtype <= ESP_PARTITION_SUBTYPE_APP_OTA_MAX;
    const bool otadata = partition->type == ESP_PARTITION_TYPE_DATA &&
                         partition->subtype == ESP_PARTITION_SUBTYPE_DATA_OTA;
    if (ota_app || otadata) {
      entries.push_back({static_cast<uint8_t>(partition->type), static_cast<uint8_t>(partition->subtype),
                         partition->address, partition->size});
      if (ota_app) {
        result.app_slot_count++;
        result.app_slot_size = result.app_slot_size == 0
                                   ? partition->size
                                   : std::min(result.app_slot_size, static_cast<size_t>(partition->size));
      } else {
        result.has_otadata = true;
      }
    }
    iterator = esp_partition_next(iterator);
  }
  std::sort(entries.begin(), entries.end(), [](const Entry &left, const Entry &right) {
    return left.offset < right.offset;
  });
  String descriptor;
  char encoded[32];
  for (const auto &entry : entries) {
    if (!descriptor.isEmpty()) descriptor += ';';
    snprintf(encoded, sizeof(encoded), "%02x:%02x:%08x:%08x", entry.type, entry.subtype,
             entry.offset, entry.size);
    descriptor += encoded;
  }
  result.layout_sha256 = sha256_hex(descriptor);
#ifdef CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE
  result.automatic_rollback = result.app_slot_count >= 2 && result.has_otadata;
#endif
  result.tier = result.app_slot_count >= 2 && result.has_otadata
                    ? (result.automatic_rollback ? "A" : "B")
                    : "C";
  return result;
}

void OtaManager::setup(IdentityStore *identity, WorkerTransport *transport, const std::string &version, const std::string &ota_base_url) {
  this->identity_ = identity;
  this->transport_ = transport;
  this->current_version_ = version.c_str();
  this->ota_base_url_ = ota_base_url.c_str();
  this->capabilities_ = this->detect_capabilities_();
  this->preference_ = global_preferences->make_preference<State>(STATE_MAGIC, true);
  if (!this->preference_.load(&this->state_) || this->state_.magic != STATE_MAGIC) this->state_ = {};
  this->verifier_.configure(this->capabilities_, endpoint_host(this->ota_base_url_.c_str()),
                            COMPONENT_VERSION, PROTOCOL_VERSION);
  if (!this->state_.pending) return;
  const esp_partition_t *running = esp_ota_get_running_partition();
  esp_ota_img_states_t image_state = ESP_OTA_IMG_UNDEFINED;
  const bool image_state_known = running != nullptr && esp_ota_get_state_partition(running, &image_state) == ESP_OK;
  const bool pending_image = image_state_known && image_state == ESP_OTA_IMG_PENDING_VERIFY;
  const bool valid_image = image_state_known && image_state == ESP_OTA_IMG_VALID;
  if (this->current_version_ == this->state_.target) {
    this->pending_verification_ = true;
    if (this->capabilities_.automatic_rollback) {
      if (!pending_image && !valid_image) { this->rollback_detected_ = true; this->pending_verification_ = false; return; }
      this->image_confirmed_ = valid_image;
      if (pending_image) {
        this->health_deadline_ = millis() + HEALTH_TIMEOUT_MS;
        if (++this->state_.attempts > 2) esp_ota_mark_app_invalid_rollback_and_reboot();
        this->preference_.save(&this->state_);
      }
    }
  } else if (this->current_version_ != this->state_.target) {
    this->rollback_detected_ = true;
  }
}

bool OtaManager::fetch_manifest_(const String &channel, String &document) {
  String url = this->ota_base_url_ + "/ota/" + channel + "/manifest.json";
  NetworkClientSecure client;
  client.setCACertBundle(x509_crt_bundle, x509_crt_bundle_end - x509_crt_bundle);
  HTTPClient http;
  if (!http.begin(client, url)) return false;
  http.setFollowRedirects(HTTPC_DISABLE_FOLLOW_REDIRECTS);
  http.setTimeout(DOWNLOAD_TIMEOUT_MS);
  const int status = http.GET();
  const int length = http.getSize();
  if (status != 200 || length <= 0 || static_cast<size_t>(length) > MAX_MANIFEST_BYTES) {
    http.end();
    return false;
  }
  document = http.getString();
  http.end();
  return document.length() == static_cast<size_t>(length);
}

void OtaManager::handle_start(JsonObjectConst message) {
  ESP_LOGI(TAG, "Received signed OTA request");
  const String command = message["command_id"] | "";
  const String channel = message["channel"] | "stable";
  const String requested = message["target_version"] | "";
  const bool recovery = message["recovery"] | false;
  if (command.isEmpty() || (channel != "stable" && channel != "beta" && channel != "recovery")) return;
  if (this->capabilities_.tier == "C") {
    this->fail_(command, "ota_unsupported");
    return;
  }
  String document;
  if (!this->fetch_manifest_(channel, document)) {
    this->fail_(command, "manifest_unavailable");
    return;
  }
  ESP_LOGI(TAG, "Signed manifest downloaded");
  VerifiedOtaManifest manifest;
  String error;
  this->verifier_.configure(this->capabilities_, endpoint_host(this->ota_base_url_.c_str()),
                            COMPONENT_VERSION, PROTOCOL_VERSION);
  if (!this->verifier_.verify(document, manifest, error)) {
    this->fail_(command, error.c_str());
    return;
  }
  ESP_LOGI(TAG, "Ed25519 manifest verification passed");
  if (manifest.channel != channel || (!requested.isEmpty() && manifest.version != requested)) {
    this->fail_(command, "target_mismatch");
    return;
  }
  if (!this->verifier_.authorize_version(manifest.version, this->current_version_, recovery,
                                         channel, manifest.recovery)) {
    this->fail_(command, "downgrade_blocked");
    return;
  }
  ESP_LOGI(TAG, "Starting firmware download to inactive slot");
  if (!this->download_(manifest, command)) return;
  this->state_ = {};
  this->state_.magic = STATE_MAGIC;
  this->state_.pending = 1;
  strlcpy(this->state_.command, command.c_str(), sizeof(this->state_.command));
  strlcpy(this->state_.target, manifest.version.c_str(), sizeof(this->state_.target));
  strlcpy(this->state_.previous, this->current_version_.c_str(), sizeof(this->state_.previous));
  if (!this->preference_.save(&this->state_)) {
    this->fail_(command, "preferences_write_failed");
    return;
  }
  ESP_LOGI(TAG, "Image verified and staged; rebooting");
  this->event_("ota_progress", command, "rebooting", 100);
  delay(500);
  ESP.restart();
}

bool OtaManager::download_(const VerifiedOtaManifest &manifest, const String &command) {
  const esp_partition_t *target = esp_ota_get_next_update_partition(nullptr);
  if (target == nullptr || manifest.firmware_size == 0 || manifest.firmware_size > target->size) {
    this->fail_(command, "no_space");
    return false;
  }
  NetworkClientSecure client;
  client.setCACertBundle(x509_crt_bundle, x509_crt_bundle_end - x509_crt_bundle);
  HTTPClient http;
  if (!http.begin(client, manifest.firmware_url)) {
    this->fail_(command, "download_start");
    return false;
  }
  http.setFollowRedirects(HTTPC_DISABLE_FOLLOW_REDIRECTS);
  http.setTimeout(DOWNLOAD_TIMEOUT_MS);
  const int status = http.GET();
  const int length = http.getSize();
  if (status != 200 || length != static_cast<int>(manifest.firmware_size)) {
    http.end();
    this->fail_(command, "download_response");
    return false;
  }
  esp_ota_handle_t handle;
  if (esp_ota_begin(target, manifest.firmware_size, &handle) != ESP_OK) {
    http.end();
    this->fail_(command, "ota_begin");
    return false;
  }
  mbedtls_sha256_context sha;
  mbedtls_sha256_init(&sha);
  mbedtls_sha256_starts(&sha, 0);
  NetworkClient *stream = http.getStreamPtr();
  constexpr size_t buffer_size = 4096;
  std::unique_ptr<uint8_t[]> buffer(new (std::nothrow) uint8_t[buffer_size]);
  if (!buffer) {
    mbedtls_sha256_free(&sha);
    esp_ota_abort(handle);
    http.end();
    this->fail_(command, "no_memory");
    return false;
  }
  size_t written = 0;
  uint32_t last_data = millis();
  bool ok = true;
  while (written < manifest.firmware_size) {
    const size_t available = stream->available();
    if (available > 0) {
      const size_t wanted = std::min(buffer_size, std::min(available, manifest.firmware_size - written));
      const int count = stream->readBytes(buffer.get(), wanted);
      if (count <= 0 || esp_ota_write(handle, buffer.get(), count) != ESP_OK) {
        ok = false;
        break;
      }
      mbedtls_sha256_update(&sha, buffer.get(), count);
      written += count;
      last_data = millis();
      esp_task_wdt_reset();
      delay(1);
      if ((written % (64 * 1024)) < static_cast<size_t>(count))
        this->event_("ota_progress", command, "downloading", 100.0f * written / manifest.firmware_size);
    } else if (!http.connected() || millis() - last_data > DOWNLOAD_TIMEOUT_MS) {
      ok = false;
      break;
    } else {
      delay(10);
    }
  }
  uint8_t digest[32];
  mbedtls_sha256_finish(&sha, digest);
  mbedtls_sha256_free(&sha);
  http.end();
  if (!ok || written != manifest.firmware_size) {
    esp_ota_abort(handle);
    this->fail_(command, "download_interrupted");
    return false;
  }
  char actual[65];
  for (size_t index = 0; index < sizeof(digest); index++)
    snprintf(actual + index * 2, 3, "%02x", digest[index]);
  if (!manifest.firmware_sha256.equalsIgnoreCase(actual)) {
    esp_ota_abort(handle);
    this->fail_(command, "sha256_mismatch");
    return false;
  }
  if (esp_ota_end(handle) != ESP_OK || esp_ota_set_boot_partition(target) != ESP_OK) {
    this->fail_(command, "image_invalid");
    return false;
  }
  return true;
}

bool OtaManager::event_(const char *type, const String &command, const char *state, float progress,
                        const char *error) {
  return this->transport_->send(json::build_json([&](JsonObject root) {
    root["type"] = type;
    root["command_id"] = command;
    root["state"] = state;
    root["progress"] = progress;
    if (error != nullptr) root["error_code"] = error;
  }));
}

bool OtaManager::result_(const String &command, const char *state, const char *code) {
  return this->transport_->send(json::build_json([&](JsonObject root) {
    root["type"] = "command_result";
    root["command_id"] = command;
    root["state"] = state;
    if (code != nullptr) {
      root["error"]["code"] = code;
      root["error"]["message"] = code;
    }
  }));
}

void OtaManager::fail_(const String &command, const char *code) {
  ESP_LOGE(TAG, "OTA failed: %s", code);
  this->event_("ota_failed", command, "failed", 0, code);
  this->result_(command, "failed", code);
}

void OtaManager::clear_pending_() {
  this->state_ = {};
  this->preference_.save(&this->state_);
}

void OtaManager::on_protocol_healthy() {
  this->protocol_healthy_ = true;
  this->report_terminal_();
}

void OtaManager::report_terminal_() {
  if (!this->protocol_healthy_ || !this->transport_->connected()) return;
  this->next_terminal_report_ = millis() + TERMINAL_REPORT_INTERVAL_MS;
  if (this->pending_verification_) {
    if (this->capabilities_.automatic_rollback && !this->image_confirmed_) {
      if (esp_ota_mark_app_valid_cancel_rollback() != ESP_OK) return;
      this->image_confirmed_ = true;
      ESP_LOGI(TAG, "Protocol health-check passed; image confirmed");
    }
    if (this->event_("ota_success", this->state_.command, "confirmed", 100) &&
        this->result_(this->state_.command, "succeeded") &&
        ++this->terminal_reports_sent_ >= TERMINAL_REPORT_COPIES) {
      this->clear_pending_();
      this->pending_verification_ = false;
    }
  } else if (this->rollback_detected_) {
    if (this->event_("ota_rollback", this->state_.command, "rollback", 100, "health_check_failed") &&
        this->result_(this->state_.command, "failed", "rollback") &&
        ++this->terminal_reports_sent_ >= TERMINAL_REPORT_COPIES) {
      this->clear_pending_();
      this->rollback_detected_ = false;
    }
  }
}

void OtaManager::loop() {
  if (this->protocol_healthy_ && (this->pending_verification_ || this->rollback_detected_) &&
      static_cast<int32_t>(millis() - this->next_terminal_report_) >= 0)
    this->report_terminal_();
  if (this->pending_verification_ && !this->image_confirmed_ && this->capabilities_.automatic_rollback &&
      static_cast<int32_t>(millis() - this->health_deadline_) >= 0)
    esp_ota_mark_app_invalid_rollback_and_reboot();
}

}  // namespace esphome::esp_anywhere_v03

#include "ota_manifest.h"

#include <Ed25519.h>
#include <ArduinoJson.h>
#include <memory>
#include <mbedtls/base64.h>

namespace esphome::esp_anywhere_v03 {
namespace {
constexpr size_t MAX_MANIFEST_BYTES = 16384;
struct TrustedKey { const char *id; uint8_t public_key[32]; };
constexpr TrustedKey TRUSTED_KEYS[] = {
    {"staging-2026-01", {0xf6, 0x49, 0x58, 0xcc, 0x03, 0x5b, 0x5a, 0xd8, 0x54, 0x41, 0x95,
                          0xeb, 0x7d, 0xbb, 0x7a, 0xb0, 0xdd, 0xe8, 0xe6, 0xc9, 0xec, 0x20,
                          0x22, 0x4b, 0x0f, 0x85, 0xb5, 0x20, 0x7c, 0xe8, 0x9f, 0xa2}},
    {"staging-esphome-2026-08", {0x9b, 0x1f, 0xb7, 0xee, 0x2d, 0x12, 0xda, 0x8d, 0xab, 0x21, 0xb2,
                                  0x34, 0x02, 0xaf, 0x08, 0xec, 0x2a, 0xfe, 0x12, 0x72, 0x4a, 0x03,
                                  0xe7, 0xb9, 0x01, 0x8f, 0x78, 0x8c, 0xa3, 0xc4, 0x9a, 0x71}},
};

bool decode_base64(const String &input, uint8_t *output, size_t capacity, size_t &length) {
  length = 0;
  return mbedtls_base64_decode(output, capacity, &length,
                               reinterpret_cast<const unsigned char *>(input.c_str()), input.length()) == 0;
}

bool parse_version(const String &value, int parts[3]) {
  const int first = value.indexOf('.');
  const int second = value.indexOf('.', first + 1);
  if (first <= 0 || second <= first + 1) return false;
  String values[3] = {value.substring(0, first), value.substring(first + 1, second), value.substring(second + 1)};
  if (values[2].indexOf('-') >= 0) values[2] = values[2].substring(0, values[2].indexOf('-'));
  for (auto &part : values) {
    if (part.isEmpty()) return false;
    for (char character : part)
      if (character < '0' || character > '9') return false;
  }
  for (int index = 0; index < 3; index++) parts[index] = values[index].toInt();
  return true;
}
}  // namespace

void OtaManifestVerifier::configure(const RuntimeOtaCapabilities &capabilities, const String &relay_host,
                                    const String &external_component_version, const String &protocol_version) {
  this->capabilities_ = capabilities;
  this->relay_host_ = relay_host;
  this->external_component_version_ = external_component_version;
  this->protocol_version_ = protocol_version;
}

int OtaManifestVerifier::compare_versions_(const String &left, const String &right) {
  int left_parts[3], right_parts[3];
  if (!parse_version(left, left_parts) || !parse_version(right, right_parts)) return -99;
  for (int index = 0; index < 3; index++)
    if (left_parts[index] != right_parts[index]) return left_parts[index] < right_parts[index] ? -1 : 1;
  return 0;
}

bool OtaManifestVerifier::safe_https_url_(const String &url) const {
  if (!url.startsWith("https://") || url.indexOf('?') >= 0 || url.indexOf('#') >= 0 || url.indexOf('@') >= 0)
    return false;
  const int slash = url.indexOf('/', 8);
  if (slash < 0) return false;
  String host = url.substring(8, slash);
  const int colon = host.indexOf(':');
  if (colon >= 0) host = host.substring(0, colon);
  return host.equalsIgnoreCase(this->relay_host_) || host.equalsIgnoreCase("raw.githubusercontent.com");
}

bool OtaManifestVerifier::verify(const String &document, VerifiedOtaManifest &result, String &error) const {
  if (document.length() == 0 || document.length() > MAX_MANIFEST_BYTES) { error = "manifest_too_large"; return false; }
  DynamicJsonDocument outer(3072);
  if (deserializeJson(outer, document)) { error = "manifest_json"; return false; }
  if (outer["schema_version"] != 2 || String(outer["security"]["algorithm"] | "") != "Ed25519") {
    error = "manifest_schema"; return false;
  }
  const String key_id = outer["security"]["key_id"] | "";
  const String signature_text = outer["security"]["signature"] | "";
  const String payload_text = outer["signed_payload"] | "";
  const uint8_t *public_key = nullptr;
  for (const auto &candidate : TRUSTED_KEYS)
    if (key_id == candidate.id) { public_key = candidate.public_key; break; }
  if (public_key == nullptr || signature_text.length() != 88 || payload_text.isEmpty()) {
    error = "untrusted_key"; return false;
  }
  uint8_t signature[64];
  size_t signature_length;
  if (!decode_base64(signature_text, signature, sizeof(signature), signature_length) || signature_length != 64) {
    error = "bad_signature"; return false;
  }
  const size_t capacity = (payload_text.length() * 3) / 4 + 4;
  if (capacity > MAX_MANIFEST_BYTES) { error = "manifest_too_large"; return false; }
  std::unique_ptr<uint8_t[]> payload(new (std::nothrow) uint8_t[capacity + 1]);
  if (!payload) { error = "no_memory"; return false; }
  size_t payload_length;
  if (!decode_base64(payload_text, payload.get(), capacity, payload_length)) { error = "payload_base64"; return false; }
  if (!Ed25519::verify(signature, public_key, payload.get(), payload_length)) { error = "bad_signature"; return false; }
  payload[payload_length] = 0;
  DynamicJsonDocument signed_document(3072);
  if (deserializeJson(signed_document, payload.get(), payload_length)) { error = "signed_payload_json"; return false; }
  JsonObjectConst compatibility = signed_document["compatibility"];
  if (signed_document["manifest_version"] != 2 || String(signed_document["project"] | "") != "esp-anywhere" ||
      String(signed_document["chip_family"] | "") != this->capabilities_.chip_family ||
      String(compatibility["layout_sha256"] | "") != this->capabilities_.layout_sha256 ||
      String(compatibility["required_tier"] | "") != this->capabilities_.tier ||
      compatibility["app_slot_count"].as<size_t>() != this->capabilities_.app_slot_count ||
      compatibility["app_slot_size"].as<size_t>() != this->capabilities_.app_slot_size ||
      compatibility["has_otadata"].as<bool>() != this->capabilities_.has_otadata ||
      String(signed_document["min_protocol_version"] | "") != this->protocol_version_ ||
      compare_versions_(String(signed_document["min_external_component_version"] | ""),
                        this->external_component_version_) > 0) {
    error = "incompatible_target"; return false;
  }
  result.version = String(signed_document["version"] | "");
  result.channel = String(signed_document["channel"] | "");
  result.recovery = signed_document["recovery"] | false;
  result.downgrade_policy = String(signed_document["downgrade_policy"] | "");
  result.firmware_url = String(signed_document["firmware"]["url"] | "");
  result.firmware_sha256 = String(signed_document["firmware"]["sha256"] | "");
  result.firmware_size = signed_document["firmware"]["size"] | 0;
  int version_parts[3];
  if (!parse_version(result.version, version_parts)) { error = "manifest_version_value"; return false; }
  if (result.firmware_size == 0 || result.firmware_size > this->capabilities_.app_slot_size ||
      result.firmware_sha256.length() != 64 || !this->safe_https_url_(result.firmware_url) ||
      (result.recovery && (result.channel != "recovery" || result.downgrade_policy != "recovery_authorized")) ||
      (!result.recovery && result.downgrade_policy != "upgrade_only")) {
    error = "manifest_policy"; return false;
  }
  return true;
}

bool OtaManifestVerifier::authorize_version(const String &target, const String &current, bool recovery_requested,
                                            const String &channel, bool manifest_recovery) const {
  const int comparison = compare_versions_(target, current);
  return comparison != -99 && comparison != 0 &&
         (comparison > 0 || (recovery_requested && channel == "recovery" && manifest_recovery));
}

}  // namespace esphome::esp_anywhere_v03

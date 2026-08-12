#pragma once

#include <Arduino.h>

namespace esphome::esp_anywhere_v03 {

struct RuntimeOtaCapabilities {
  String chip_family;
  String tier;
  String layout_sha256;
  size_t app_slot_count{0};
  size_t app_slot_size{0};
  bool has_otadata{false};
  bool automatic_rollback{false};
};

struct VerifiedOtaManifest {
  String version;
  String channel;
  String firmware_url;
  String firmware_sha256;
  size_t firmware_size{0};
  bool recovery{false};
  String downgrade_policy;
};

class OtaManifestVerifier {
 public:
  void configure(const RuntimeOtaCapabilities &capabilities, const String &relay_host,
                 const String &external_component_version, const String &protocol_version);
  bool verify(const String &document, VerifiedOtaManifest &result, String &error) const;
  bool authorize_version(const String &target, const String &current, bool recovery_requested,
                         const String &channel, bool manifest_recovery) const;

 protected:
  bool safe_https_url_(const String &url) const;
  static int compare_versions_(const String &left, const String &right);

  RuntimeOtaCapabilities capabilities_;
  String relay_host_;
  String external_component_version_;
  String protocol_version_;
};

}  // namespace esphome::esp_anywhere_v03

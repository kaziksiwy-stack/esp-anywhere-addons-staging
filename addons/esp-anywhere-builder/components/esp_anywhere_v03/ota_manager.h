#pragma once

#include "identity_store.h"
#include "ota_manifest.h"
#include "worker_transport.h"

#include <ArduinoJson.h>
#include "esphome/core/preferences.h"

namespace esphome::esp_anywhere_v03 {

class OtaManager {
 public:
  void setup(IdentityStore *identity, WorkerTransport *transport, const std::string &version, const std::string &ota_base_url);
  void loop();
  void handle_start(JsonObjectConst message);
  void on_protocol_healthy();
  const RuntimeOtaCapabilities &capabilities() const { return this->capabilities_; }

 protected:
  struct State {
    uint32_t magic;
    uint8_t pending;
    uint8_t attempts;
    char command[65];
    char target[40];
    char previous[40];
  };

  RuntimeOtaCapabilities detect_capabilities_() const;
  bool fetch_manifest_(const String &channel, String &document);
  bool download_(const VerifiedOtaManifest &manifest, const String &command);
  bool event_(const char *type, const String &command, const char *state, float progress,
              const char *error = nullptr);
  bool result_(const String &command, const char *state, const char *code = nullptr);
  void fail_(const String &command, const char *code);
  void clear_pending_();
  void report_terminal_();

  IdentityStore *identity_{nullptr};
  WorkerTransport *transport_{nullptr};
  OtaManifestVerifier verifier_;
  RuntimeOtaCapabilities capabilities_;
  ESPPreferenceObject preference_;
  State state_{};
  String current_version_;
  String ota_base_url_;
  bool pending_verification_{false};
  bool rollback_detected_{false};
  bool protocol_healthy_{false};
  bool image_confirmed_{false};
  uint8_t terminal_reports_sent_{0};
  uint32_t next_terminal_report_{0};
  uint32_t health_deadline_{0};
};

}  // namespace esphome::esp_anywhere_v03

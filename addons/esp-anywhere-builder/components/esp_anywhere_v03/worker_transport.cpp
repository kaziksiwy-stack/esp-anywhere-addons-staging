#include "worker_transport.h"

#include <cstdlib>

namespace esphome::esp_anywhere_v03 {

extern const uint8_t x509_crt_bundle[] asm("_binary_x509_crt_bundle_start");
extern const uint8_t x509_crt_bundle_end[] asm("_binary_x509_crt_bundle_end");

void WorkerTransport::setup(EventCallback callback) {
  this->callback_ = std::move(callback);
  this->websocket_.onEvent([this](WStype_t type, uint8_t *payload, size_t length) {
    this->on_event_(type, payload, length);
  });
  this->websocket_.setReconnectInterval(5000);
}

void WorkerTransport::loop() {
  if (this->started_) this->websocket_.loop();
}

bool WorkerTransport::parse_endpoint_(const char *endpoint, std::string &host, uint16_t &port) const {
  std::string value = endpoint == nullptr ? "" : endpoint;
  if (value.rfind("https://", 0) != 0) return false;
  value.erase(0, 8);
  if (!value.empty() && value.back() == '/') value.pop_back();
  if (value.empty() || value.find('/') != std::string::npos) return false;
  port = 443;
  const size_t colon = value.rfind(':');
  if (colon != std::string::npos) {
    char *end = nullptr;
    const long parsed = strtol(value.substr(colon + 1).c_str(), &end, 10);
    if (end == nullptr || *end != '\0' || parsed < 1 || parsed > 65535) return false;
    port = static_cast<uint16_t>(parsed);
    value.resize(colon);
  }
  host = value;
  return !host.empty();
}

bool WorkerTransport::connect(const StoredIdentity &identity) {
  if (this->started_) return true;
  std::string host;
  uint16_t port;
  if (!this->parse_endpoint_(identity.relay_url, host, port)) return false;
  const std::string path = "/ws?role=device&installation_id=" + std::string(identity.installation_id) +
                           "&device_id=" + identity.device_id;
  const size_t ca_size = static_cast<size_t>(x509_crt_bundle_end - x509_crt_bundle);
  this->websocket_.beginSslWithBundle(host.c_str(), port, path.c_str(), x509_crt_bundle, ca_size);
  this->websocket_.setExtraHeaders(("Authorization: Bearer " + std::string(identity.token)).c_str());
  this->started_ = true;
  return true;
}

bool WorkerTransport::send(const std::string &message) {
  return this->connected_ && this->websocket_.sendTXT(message.c_str());
}

void WorkerTransport::on_event_(WStype_t type, uint8_t *payload, size_t length) {
  if (type == WStype_CONNECTED) this->connected_ = true;
  if (type == WStype_DISCONNECTED) this->connected_ = false;
  if (this->callback_) this->callback_(type, payload, length);
}

}  // namespace esphome::esp_anywhere_v03

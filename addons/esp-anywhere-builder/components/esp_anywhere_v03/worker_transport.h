#pragma once

#include <functional>
#include <string>

#include <WebSocketsClient.h>

#include "identity_store.h"

namespace esphome::esp_anywhere_v03 {

class WorkerTransport {
 public:
  using EventCallback = std::function<void(WStype_t, uint8_t *, size_t)>;

  void setup(EventCallback callback);
  void loop();
  bool connect(const StoredIdentity &identity);
  bool send(const std::string &message);
  bool connected() const { return this->connected_; }
  bool started() const { return this->started_; }

 protected:
  bool parse_endpoint_(const char *endpoint, std::string &host, uint16_t &port) const;
  void on_event_(WStype_t type, uint8_t *payload, size_t length);

  WebSocketsClient websocket_;
  EventCallback callback_;
  bool started_{false};
  bool connected_{false};
};

}  // namespace esphome::esp_anywhere_v03

#pragma once

#include "ecg_ble_protocol.h"
#include <Arduino.h>
#include <BLE2902.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <esp_gatt_common_api.h>

struct BleLink {
  bool connected = false;
  bool subscribed = false;
  bool congested = false;
  bool changed = false;
  bool start = false;
  uint16_t id = 0;
  uint16_t mtu = 23;
  esp_gatt_if_t interface = ESP_GATT_IF_NONE;
};

static portMUX_TYPE bleMux = portMUX_INITIALIZER_UNLOCKED;
static BleLink bleState;
static BLEServer *bleServer;
static BLECharacteristic *dataCharacteristic;
static BLECharacteristic *commandCharacteristic;
static BLE2902 *subscription;
static uint32_t packetNumber = 0; // Only the acquisition loop writes this.

inline void handleGatt(esp_gatts_cb_event_t event, esp_gatt_if_t gatt,
                       esp_ble_gatts_cb_param_t *p) {
  portENTER_CRITICAL(&bleMux);
  switch (event) {
  case ESP_GATTS_CONNECT_EVT:
    bleState = BleLink{};
    bleState.connected = bleState.changed = true;
    bleState.id = p->connect.conn_id;
    bleState.interface = gatt;
    break;
  case ESP_GATTS_DISCONNECT_EVT:
    if (p->disconnect.conn_id == bleState.id) {
      bleState = BleLink{};
      bleState.changed = true;
    }
    break;
  case ESP_GATTS_MTU_EVT:
    if (p->mtu.conn_id == bleState.id)
      bleState.mtu = p->mtu.mtu;
    break;
  case ESP_GATTS_CONGEST_EVT:
    if (p->congest.conn_id == bleState.id)
      bleState.congested = p->congest.congested;
    break;
  case ESP_GATTS_WRITE_EVT:
    if (!bleState.connected || p->write.conn_id != bleState.id ||
        p->write.is_prep)
      break;
    if (p->write.handle == subscription->getHandle() && p->write.len == 2) {
      bleState.subscribed = (p->write.value[0] & 1) != 0;
      if (!bleState.subscribed) {
        bleState.start = false;
        bleState.changed = true;
      }
    } else if (p->write.handle == commandCharacteristic->getHandle() &&
               p->write.len <= 16 && bleState.subscribed) {
      StartCommand parser;
      for (uint16_t i = 0; i < p->write.len; ++i) {
        if (parser.feed(p->write.value[i]) == 1)
          bleState.start = true;
      }
    }
    break;
  default:
    break;
  }
  portEXIT_CRITICAL(&bleMux);

  if (event == ESP_GATTS_CONNECT_EVT) {
    bleServer->updateConnParams(p->connect.remote_bda, 12, 16, 0, 400);
    esp_ble_gap_set_pkt_data_len(p->connect.remote_bda, 251);
  }
}

inline void setupBle() {
  BLEDevice::init("Capstone ECG SPI");
  BLEDevice::setMTU(ECG_MTU);
  bleServer = BLEDevice::createServer();
  BLEService *service = bleServer->createService(ECG_SERVICE);
  dataCharacteristic = service->createCharacteristic(
      ECG_DATA, BLECharacteristic::PROPERTY_NOTIFY);
  subscription = new BLE2902();
  dataCharacteristic->addDescriptor(subscription);
  commandCharacteristic = service->createCharacteristic(
      ECG_COMMAND, BLECharacteristic::PROPERTY_WRITE);
  BLEDevice::setCustomGattsHandler(handleGatt);
  service->start();
  BLEAdvertisementData announcement;
  announcement.setFlags(ESP_BLE_ADV_FLAG_GEN_DISC |
                        ESP_BLE_ADV_FLAG_BREDR_NOT_SPT);
  announcement.setCompleteServices(BLEUUID(ECG_SERVICE));
  BLEAdvertisementData scanReply;
  scanReply.setName("Capstone ECG SPI");
  BLEAdvertising *advertising = BLEDevice::getAdvertising();
  advertising->setAdvertisementData(announcement);
  advertising->setScanResponseData(scanReply);
  advertising->start();
}

inline BleLink getBleLink() {
  portENTER_CRITICAL(&bleMux);
  const BleLink result = bleState;
  portEXIT_CRITICAL(&bleMux);
  return result;
}

inline bool takeBleChange() {
  portENTER_CRITICAL(&bleMux);
  const bool changed = bleState.changed;
  bleState.changed = false;
  portEXIT_CRITICAL(&bleMux);
  return changed;
}

inline bool takeStartRequest() {
  portENTER_CRITICAL(&bleMux);
  const bool start =
      bleState.connected && bleState.subscribed && bleState.start;
  bleState.start = false;
  portEXIT_CRITICAL(&bleMux);
  return start;
}

inline bool bleClientActive() {
  const BleLink current = getBleLink();
  return current.connected && current.subscribed && !current.changed;
}

inline uint16_t bleSendCapacity(const BleLink &current) {
  if (!current.connected || !current.subscribed || current.changed ||
      current.congested || current.mtu <= 3 + BLE_HEADER) {
    return 0;
  }
  return min<uint16_t>(BLE_BODY, current.mtu - 3 - BLE_HEADER);
}

inline bool notifyBle(const BleLink &current, uint8_t *packet,
                      uint16_t length) {
  if (esp_ble_get_sendable_packets_num() == 0)
    return false;
  writePacketNumber(packet, packetNumber);
  const esp_err_t result = esp_ble_gatts_send_indicate(
      current.interface, current.id, dataCharacteristic->getHandle(),
      length + BLE_HEADER, packet, false);
  if (result != ESP_OK)
    return false;
  ++packetNumber;
  return true;
}

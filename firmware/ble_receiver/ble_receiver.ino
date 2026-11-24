#include <BLEDevice.h>
#include <BLERemoteDescriptor.h>
#include <atomic>
#include <ecg_ble_protocol.h>
#include <start_command.h>

struct Packet {
  uint16_t length;
  uint8_t data[BLE_BODY];
};

constexpr uint8_t PACKET_CAPACITY = 48;
BLEClient *client;
BLEScan *scanner;
BLERemoteCharacteristic *commandCharacteristic;
StartCommand usbCommand;
std::atomic<bool> usbDisconnected{false};
// The BLE callback and USB loop share the packet queue and sequence state.
portMUX_TYPE packetMux = portMUX_INITIALIZER_UNLOCKED;
Packet packets[PACKET_CAPACITY];
uint8_t packetHead = 0;
uint8_t packetTail = 0;
bool acceptingPackets = false;
uint32_t expectedPacket = 0;
const char *linkFault = nullptr;
bool streamRequested = false;
bool startPending = false;
bool usbWasConnected = false;
Packet usbPacket{};
uint16_t usbOffset = 0;

void markLinkFault(const char *reason) {
  portENTER_CRITICAL(&packetMux);
  if (acceptingPackets) {
    acceptingPackets = false;
    linkFault = reason;
  }
  portEXIT_CRITICAL(&packetMux);
}

class ClientEvents : public BLEClientCallbacks {
  void onConnect(BLEClient *) override {}

  void onDisconnect(BLEClient *) override { markLinkFault("disconnected"); }
};

void receivePacket(BLERemoteCharacteristic *, uint8_t *data, size_t size,
                   bool) {
  portENTER_CRITICAL(&packetMux);
  if (acceptingPackets) {
    const uint8_t next = (packetHead + 1) % PACKET_CAPACITY;
    if (size <= BLE_HEADER || size > BLE_HEADER + BLE_BODY) {
      linkFault = "invalid packet length";
    } else if (readPacketNumber(data) != expectedPacket) {
      linkFault = "packet sequence gap";
    } else if (next == packetTail) {
      linkFault = "receiver queue full";
    } else {
      Packet &packet = packets[packetHead];
      packet.length = size - BLE_HEADER;
      memcpy(packet.data, data + BLE_HEADER, packet.length);
      packetHead = next;
      ++expectedPacket;
    }
    if (linkFault) {
      acceptingPackets = false;
    }
  }
  portEXIT_CRITICAL(&packetMux);
}

bool popPacket() {
  portENTER_CRITICAL(&packetMux);
  const bool available = acceptingPackets && packetTail != packetHead;
  if (available) {
    usbPacket = packets[packetTail];
    packetTail = (packetTail + 1) % PACKET_CAPACITY;
  }
  portEXIT_CRITICAL(&packetMux);
  return available;
}

const char *takeLinkFault() {
  portENTER_CRITICAL(&packetMux);
  const char *reason = linkFault;
  linkFault = nullptr;
  portEXIT_CRITICAL(&packetMux);
  return reason;
}

void onUsbDisconnect(void *, esp_event_base_t, int32_t, void *) {
  usbDisconnected.store(true);
}

void setUsbNotice(const char *message) {
  usbOffset = 0;
  usbPacket.length = snprintf(reinterpret_cast<char *>(usbPacket.data),
                              sizeof(usbPacket.data), "\nINFO: %s\n", message);
}

void pumpUsb() {
  for (size_t i = 0; i < 4; ++i) {
    if (usbOffset == usbPacket.length) {
      if (!popPacket())
        return;
      usbOffset = 0;
    }
    const int space = Serial.availableForWrite();
    if (space <= 0)
      return;
    const size_t count = min<size_t>(space, usbPacket.length - usbOffset);
    const size_t sent = Serial.write(usbPacket.data + usbOffset, count);
    if (!sent)
      return;
    usbOffset += sent;
  }
}

void closeLink() {
  portENTER_CRITICAL(&packetMux);
  acceptingPackets = false;
  packetHead = packetTail = 0;
  linkFault = nullptr;
  portEXIT_CRITICAL(&packetMux);
  commandCharacteristic = nullptr;
  startPending = false;
  usbCommand.reset();
  usbOffset = usbPacket.length = 0;
  if (client->isConnected())
    client->disconnect();
}

bool subscribeToTransmitter() {
  BLEScanResults matches = scanner->start(1, false);
  bool connected = false;
  for (int i = 0; i < matches.getCount(); ++i) {
    BLEAdvertisedDevice target = matches.getDevice(i);
    if (target.isAdvertisingService(BLEUUID(ECG_SERVICE))) {
      connected = client->connect(&target);
      break;
    }
  }
  scanner->clearResults();
  if (!connected)
    return false;

  if (!client->setMTU(ECG_MTU)) {
    client->disconnect();
    return false;
  }
  // Refresh handles when either board has rebooted since the last connection.
  client->getServices();
  BLERemoteService *service = client->getService(ECG_SERVICE);
  auto *data = service ? service->getCharacteristic(ECG_DATA) : nullptr;
  auto *command = service ? service->getCharacteristic(ECG_COMMAND) : nullptr;
  auto *cccd = data ? data->getDescriptor(BLEUUID(uint16_t(0x2902))) : nullptr;
  if (!data || !data->canNotify() || !command || !command->canWrite() ||
      !cccd) {
    client->disconnect();
    return false;
  }

  data->registerForNotify(receivePacket, true, false);
  uint8_t enable[] = {1, 0};
  cccd->writeValue(enable, sizeof(enable), true);
  const auto subscribed = cccd->readValue();
  if (!client->isConnected() || subscribed.size() != 2 || subscribed[0] != 1) {
    client->disconnect();
    return false;
  }

  portENTER_CRITICAL(&packetMux);
  packetHead = packetTail = 0;
  expectedPacket = 0;
  acceptingPackets = true;
  portEXIT_CRITICAL(&packetMux);
  commandCharacteristic = command;
  startPending = streamRequested;
  setUsbNotice("BLE_READY");
  return true;
}

void setup() {
  Serial.onEvent(ARDUINO_USB_CDC_DISCONNECTED_EVENT, onUsbDisconnect);
  Serial.begin(115200);
  Serial.setTxTimeoutMs(0);
  BLEDevice::init("Capstone ECG SPI Receiver");
  BLEDevice::setMTU(ECG_MTU);
  client = BLEDevice::createClient();
  client->setClientCallbacks(new ClientEvents());
  scanner = BLEDevice::getScan();
  scanner->setActiveScan(true);
}

void loop() {
  const bool usbDropped = usbDisconnected.exchange(false);
  if (!Serial || usbDropped) {
    if (usbWasConnected)
      closeLink();
    usbWasConnected = false;
    streamRequested = false;
    if (!Serial) {
      for (uint8_t i = 0; i < 64 && Serial.available(); ++i)
        Serial.read();
      delay(10);
      return;
    }
  }
  usbWasConnected = true;

  if (commandCharacteristic && !client->isConnected()) {
    markLinkFault("disconnected");
  }
  if (const char *reason = takeLinkFault()) {
    closeLink();
    char message[80];
    snprintf(message, sizeof(message), "BLE_LOST %s", reason);
    setUsbNotice(message);
  }

  for (uint8_t i = 0; i < 64 && Serial.available(); ++i) {
    if (usbCommand.feed(Serial.read()) == 1) {
      streamRequested = true;
      startPending = true;
    }
  }
  pumpUsb();

  if (!commandCharacteristic) {
    // Drain any pending loss message before blocking briefly in BLE discovery.
    if (usbOffset == usbPacket.length && !client->isConnected()) {
      subscribeToTransmitter();
    }
  } else if (startPending && client->isConnected()) {
    startPending = false;
    commandCharacteristic->writeValue(std::string("go\n"), true);
  }
  delay(1);
}

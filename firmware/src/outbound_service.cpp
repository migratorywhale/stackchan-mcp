#include "outbound_service.h"

#include <M5Unified.h>
#include <WebSocketsClient.h>
#include <WiFi.h>
#include <atomic>
#include <mbedtls/x509_crt.h>
#include <sys/time.h>
#include <time.h>

#include "config_loader.h"
#include "firmware_command.h"
#include "playback_service.h"

namespace {

static_assert(WEBSOCKETS_MAX_DATA_SIZE == outbound::MAX_FRAME_BYTES,
              "The pinned WebSockets bounded-frame build patch must be applied");

enum class Phase : uint8_t { IDLE, RECEIVING, QUEUED, RUNNING, READY };

static std::atomic<Phase> phase{Phase::IDLE};
static std::atomic<uint32_t> generation{1};
static std::atomic<bool> connected{false};
static FirmwareCommand command;
static TaskHandle_t networkTask = nullptr;
static WebSocketsClient socket;
static outbound::MessageBuffer fragments(ps_malloc);
static uint32_t fragmentStartedMs = 0;
static uint32_t lastHeartbeatMs = 0;

static int64_t utcNowMs() {
    timeval now;
    if (gettimeofday(&now, nullptr) != 0) return 0;
    return static_cast<int64_t>(now.tv_sec) * 1000 + now.tv_usec / 1000;
}

static bool currentCommand() {
    return connected.load() && outbound::commandIsCurrent(
        command.generation, generation.load(), millis(), command.startedMs, utcNowMs(), command.info.expiresAtMs);
}

static void invalidateConnection() {
    if (connected.exchange(false)) generation.fetch_add(1);
    fragments.clear();
}

static void closeConnection() {
    invalidateConnection();
    socket.disconnect();
}

// Only the network task releases the slot. The main loop publishes READY after
// its last access, so disconnect can cancel queued work without freeing a live
// camera/audio command out from under the main loop.
static void releaseSlot() {
    command.clear();
    phase.store(Phase::IDLE);
}

static void receiveText(const uint8_t* data, size_t size) {
    if (phase.load() != Phase::IDLE) {
        closeConnection();
        return;
    }
    command.clear();
    if (!outbound::parseRequest(data, size, command.request, command.info)) {
        closeConnection();
        return;
    }
    command.generation = generation.load();
    command.startedMs = millis();
    phase.store(command.info.binarySize ? Phase::RECEIVING : Phase::QUEUED);
}

static void receiveBinary(const uint8_t* data, size_t size) {
    if (phase.load() != Phase::RECEIVING || !currentCommand() ||
        !outbound::validBinary(data, size, command.info)) {
        closeConnection();
        return;
    }
    command.pcmData = static_cast<uint8_t*>(ps_malloc(command.info.binarySize));
    if (!command.pcmData) {
        closeConnection();
        return;
    }
    memcpy(command.pcmData, data + outbound::BINARY_HEADER_BYTES, command.info.binarySize);
    phase.store(Phase::QUEUED);
}

static void receiveMessage(bool binary, const uint8_t* data, size_t size) {
    if (binary) receiveBinary(data, size);
    else receiveText(data, size);
}

static void onSocketEvent(WStype_t type, uint8_t* data, size_t size) {
    switch (type) {
        case WStype_CONNECTED:
            generation.fetch_add(1);
            connected.store(true);
            lastHeartbeatMs = millis();
            if (!socket.sendTXT("{\"type\":\"hello\",\"v\":1}")) closeConnection();
            break;
        case WStype_DISCONNECTED:
            invalidateConnection();
            break;
        case WStype_TEXT:
        case WStype_BIN:
            if (fragments.active()) closeConnection();
            else receiveMessage(type == WStype_BIN, data, size);
            break;
        case WStype_FRAGMENT_TEXT_START:
        case WStype_FRAGMENT_BIN_START:
            fragmentStartedMs = millis();
            if (!fragments.begin(type == WStype_FRAGMENT_BIN_START) || !fragments.append(data, size)) {
                closeConnection();
            }
            break;
        case WStype_FRAGMENT:
        case WStype_FRAGMENT_FIN:
            if (!fragments.append(data, size)) {
                closeConnection();
            } else if (type == WStype_FRAGMENT_FIN) {
                receiveMessage(fragments.binary(), fragments.data(), fragments.size());
                fragments.clear();
            }
            break;
        case WStype_ERROR:
            closeConnection();
            break;
        default:
            break;
    }
}

static bool sendResponse() {
    if (!currentCommand()) return false;
    JsonDocument response;
    response["id"] = command.info.id;
    response["status"] = command.status;
    if (command.binaryFrame) {
        response["binary_size"] = command.binarySize;
        response["content_type"] = command.contentType;
    } else {
        JsonDocument body;
        if (deserializeJson(body, command.body)) {
            response["status"] = 500;
            response["body"]["success"] = false;
            response["body"]["error"] = "invalid command response";
        } else {
            response["body"] = body;
        }
    }
    if (response.overflowed() || measureJson(response) > outbound::MAX_TEXT_BYTES) return false;
    String text;
    serializeJson(response, text);
    if (!currentCommand() || !socket.sendTXT(text) || !currentCommand()) return false;
    return !command.binaryFrame || socket.sendBIN(
        command.binaryFrame, command.binarySize + outbound::BINARY_HEADER_BYTES);
}

static void outboundNetworkTask(void*) {
    // begin() clears authorization. Configure it afterwards and never log it.
    socket.beginSslWithCA(STACKCHAN_OUTBOUND_HOST, STACKCHAN_OUTBOUND_PORT,
                          "/stackchan/ws", STACKCHAN_OUTBOUND_CA_PEM, "");
    String authorization = String("Bearer ") + STACKCHAN_OUTBOUND_DEVICE_TOKEN;
    socket.setAuthorization(authorization.c_str());
    socket.setExtraHeaders("");
    socket.setReconnectInterval(5000);
    socket.enableHeartbeat(15000, 5000, 2);
    socket.onEvent(onSocketEvent);
    bool timeRequested = false;

    for (;;) {
        // READY is an acquire/release handoff; no request fields are read while
        // the main loop owns RUNNING, including during a connection timeout.
        Phase state = phase.load();
        if (state == Phase::READY) {
            if (connected.load() && command.generation == generation.load() && !sendResponse()) closeConnection();
            releaseSlot();
        } else if (state == Phase::RECEIVING && !connected.load()) {
            releaseSlot();
        }

        const bool wifiReady = WiFi.status() == WL_CONNECTED;
        if (!wifiReady) {
            if (connected.load()) closeConnection();
        } else {
            if (!timeRequested) {
                configTime(0, 0, STACKCHAN_OUTBOUND_NTP_SERVER);
                timeRequested = true;
            }
            // Certificate validity needs a plausible clock. Never bypass TLS
            // verification when time sync or provisioning is unavailable.
            if (time(nullptr) >= 1704067200) socket.loop();
        }

        state = phase.load();
        if (connected.load()) {
            const uint32_t now = millis();
            if (state != Phase::IDLE && !currentCommand()) {
                closeConnection();
            } else if (state == Phase::RECEIVING && outbound::deadlineExpired(
                           now, command.startedMs, outbound::BINARY_TIMEOUT_MS)) {
                closeConnection();
            } else if (fragments.active() && outbound::deadlineExpired(
                           now, fragmentStartedMs, outbound::BINARY_TIMEOUT_MS)) {
                closeConnection();
            } else if (outbound::deadlineExpired(now, lastHeartbeatMs, outbound::HEARTBEAT_MS)) {
                lastHeartbeatMs = now;
                if (!socket.sendTXT("{\"type\":\"heartbeat\"}")) closeConnection();
            }
        }
        vTaskDelay(pdMS_TO_TICKS(10));
    }
}

}  // namespace

void initOutboundService() {
    if (networkTask) return;
    if (!outbound::validConfiguration(STACKCHAN_OUTBOUND_ENABLED,
            STACKCHAN_OUTBOUND_HOST, STACKCHAN_OUTBOUND_PORT,
            STACKCHAN_OUTBOUND_DEVICE_TOKEN, STACKCHAN_OUTBOUND_CA_PEM)) {
        Serial.println("[OUTBOUND] Disabled (opt-in or TLS configuration missing/invalid)");
        return;
    }
    mbedtls_x509_crt ca;
    mbedtls_x509_crt_init(&ca);
    int parsed = mbedtls_x509_crt_parse(&ca,
        reinterpret_cast<const unsigned char*>(STACKCHAN_OUTBOUND_CA_PEM),
        strlen(STACKCHAN_OUTBOUND_CA_PEM) + 1);
    mbedtls_x509_crt_free(&ca);
    if (parsed != 0) {
        Serial.println("[OUTBOUND] Disabled (invalid CA certificate)");
        return;
    }
    if (xTaskCreatePinnedToCore(outboundNetworkTask, "outboundWss", 12288,
            nullptr, 1, &networkTask, 0) != pdPASS) {
        networkTask = nullptr;
        Serial.println("[OUTBOUND] Disabled (network task allocation failed)");
    }
}

void serviceOutboundCommands() {
    clearStaleOutboundPcm();
    Phase expected = Phase::QUEUED;
    if (!phase.compare_exchange_strong(expected, Phase::RUNNING)) return;
    if (currentCommand()) {
        executeFirmwareCommand(command);
    }
    phase.store(Phase::READY);
}

bool isOutboundGenerationCurrent(uint32_t commandGeneration) {
    return connected.load() && generation.load() == commandGeneration;
}

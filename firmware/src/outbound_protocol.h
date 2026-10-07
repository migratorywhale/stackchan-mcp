#pragma once

#include <ArduinoJson.h>
#include <stddef.h>
#include <stdint.h>

namespace outbound {

constexpr size_t MAX_TEXT_BYTES = 8 * 1024;
constexpr size_t MAX_BINARY_BYTES = 128 * 1024;
// Response-only allowance for up to 8 seconds of 16 kHz mono recorded WAV.
// Incoming PCM, receive frames, and JPEG responses keep the 128 KiB bound.
constexpr size_t MAX_WAV_RESPONSE_BYTES = 512 * 1024;
constexpr size_t ID_BYTES = 32;
constexpr size_t BINARY_HEADER_BYTES = 4 + ID_BYTES;
constexpr size_t MAX_FRAME_BYTES = MAX_BINARY_BYTES + BINARY_HEADER_BYTES;
constexpr uint32_t COMMAND_TIMEOUT_MS = 20000;
constexpr uint32_t CLOCK_SKEW_TOLERANCE_MS = 5000;
constexpr uint32_t BINARY_TIMEOUT_MS = 5000;
constexpr uint32_t HEARTBEAT_MS = 10000;

struct RequestInfo {
    char id[ID_BYTES + 1] = {};
    size_t binarySize = 0;
    int64_t expiresAtMs = 0;
};

bool validId(const char* id, size_t size);
bool parseRequest(const uint8_t* data, size_t size, JsonDocument& doc, RequestInfo& info);
bool hasRequestBody(const JsonDocument& doc);
bool validBinary(const uint8_t* data, size_t size, const RequestInfo& info);
void writeBinaryHeader(uint8_t* data, const char* id);
enum class BinaryCopyResult { OK, TOO_LARGE, NO_MEMORY, INVALID };
size_t binaryResponseLimit(const char* contentType);
// On success the caller owns *frame (SCB1 header + body); failures leave it null.
BinaryCopyResult copyBinaryResponse(const char* id, const char* contentType,
                                   const uint8_t* data, size_t size, uint8_t** frame,
                                   void* (*allocate)(size_t));
bool deadlineExpired(uint32_t now, uint32_t started, uint32_t timeout);
bool absoluteDeadlineIsCurrent(int64_t utcNowMs, int64_t expiresAtMs);
bool commandIsCurrent(uint32_t generation, uint32_t currentGeneration, uint32_t now, uint32_t started,
                      int64_t utcNowMs, int64_t expiresAtMs);
bool validConfiguration(bool enabled, const char* host, unsigned port, const char* token, const char* ca);

// Bound aggregate size too, even if the peer fragments a message into frames
// that each pass the pinned library's (build-overridden) frame limit.
class MessageBuffer {
public:
    using Allocate = void* (*)(size_t);
    explicit MessageBuffer(Allocate allocate);
    ~MessageBuffer();
    MessageBuffer(const MessageBuffer&) = delete;
    MessageBuffer& operator=(const MessageBuffer&) = delete;
    bool begin(bool binary);
    bool append(const uint8_t* data, size_t size);
    void clear();
    bool active() const { return data_ != nullptr; }
    bool binary() const { return binary_; }
    const uint8_t* data() const { return data_; }
    size_t size() const { return size_; }

private:
    Allocate allocate_;
    uint8_t* data_ = nullptr;
    size_t size_ = 0;
    size_t limit_ = 0;
    bool binary_ = false;
};

}  // namespace outbound

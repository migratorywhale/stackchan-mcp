#include "outbound_protocol.h"

#include <stdlib.h>
#include <string.h>

namespace outbound {

bool validId(const char* id, size_t size) {
    if (!id || size != ID_BYTES) return false;
    for (size_t i = 0; i < size; ++i) {
        if (!((id[i] >= '0' && id[i] <= '9') || (id[i] >= 'a' && id[i] <= 'f'))) return false;
    }
    return true;
}

static bool boundedString(JsonVariantConst value, size_t limit) {
    if (!value.is<const char*>()) return false;
    JsonString str = value.as<JsonString>();
    return str.size() <= limit && strlen(str.c_str()) == str.size();
}

bool parseRequest(const uint8_t* data, size_t size, JsonDocument& doc, RequestInfo& info) {
    doc.clear();
    info = {};
    if (!data || size == 0 || size > MAX_TEXT_BYTES || memchr(data, 0, size)) return false;
    if (deserializeJson(doc, data, size, DeserializationOption::NestingLimit(6)) ||
        !doc.is<JsonObject>() || !doc["v"].is<int>() || doc["v"].as<int>() != 1) return false;

    if (!boundedString(doc["id"], ID_BYTES)) return false;
    JsonString id = doc["id"].as<JsonString>();
    if (!validId(id.c_str(), id.size())) return false;
    if (!doc["expires_at_ms"].is<int64_t>() || doc["expires_at_ms"].as<int64_t>() <= 0) return false;
    info.expiresAtMs = doc["expires_at_ms"];
    if (!boundedString(doc["method"], 6) || !boundedString(doc["path"], 128)) return false;
    const char* method = doc["method"];
    const char* path = doc["path"];
    if (strcmp(method, "GET") != 0 && strcmp(method, "POST") != 0) return false;
    if (path[0] != '/' || strchr(path, '?') || strchr(path, '#')) return false;

    if (!doc["query"].isUnbound()) {
        if (!doc["query"].is<JsonObject>() || doc["query"].size() > 16) return false;
        for (JsonPairConst pair : doc["query"].as<JsonObjectConst>()) {
            if (pair.key().size() > 64 || strlen(pair.key().c_str()) != pair.key().size() ||
                !boundedString(pair.value(), 256)) return false;
            // "plain" is reserved for the JSON body in Arduino WebServer.
            if (strcmp(pair.key().c_str(), "plain") == 0) return false;
        }
    }
    if (!doc["body"].isUnbound() && !doc["body"].is<JsonObject>()) return false;

    const bool pcm = strcmp(method, "POST") == 0 && strcmp(path, "/play/pcm") == 0;
    if (pcm) {
        if (!doc["binary_size"].is<size_t>()) return false;
        info.binarySize = doc["binary_size"];
        if (!info.binarySize || info.binarySize > MAX_BINARY_BYTES || info.binarySize % 2) return false;
        if (!doc["body"].isUnbound() && doc["body"].size() != 0) return false;
    } else if (!doc["binary_size"].isUnbound()) {
        return false;
    }
    memcpy(info.id, id.c_str(), ID_BYTES);
    return !doc.overflowed();
}

bool validBinary(const uint8_t* data, size_t size, const RequestInfo& info) {
    return data && info.binarySize > 0 && info.binarySize <= MAX_BINARY_BYTES &&
        size == BINARY_HEADER_BYTES + info.binarySize &&
        memcmp(data, "SCB1", 4) == 0 && memcmp(data + 4, info.id, ID_BYTES) == 0;
}

bool hasRequestBody(const JsonDocument& doc) {
    return strcmp(doc["method"] | "", "POST") == 0 && doc["body"].is<JsonObjectConst>();
}

void writeBinaryHeader(uint8_t* data, const char* id) {
    memcpy(data, "SCB1", 4);
    memcpy(data + 4, id, ID_BYTES);
}

size_t binaryResponseLimit(const char* contentType) {
    return contentType && strcmp(contentType, "audio/wav") == 0 ? MAX_WAV_RESPONSE_BYTES : MAX_BINARY_BYTES;
}

BinaryCopyResult copyBinaryResponse(const char* id, const char* contentType,
                                   const uint8_t* data, size_t size, uint8_t** frame,
                                   void* (*allocate)(size_t)) {
    if (!frame) return BinaryCopyResult::INVALID;
    *frame = nullptr;
    if (!id || !validId(id, strlen(id)) || !contentType || !data || !size || !allocate) {
        return BinaryCopyResult::INVALID;
    }
    if (size > binaryResponseLimit(contentType)) return BinaryCopyResult::TOO_LARGE;
    uint8_t* copy = static_cast<uint8_t*>(allocate(BINARY_HEADER_BYTES + size));
    if (!copy) return BinaryCopyResult::NO_MEMORY;
    writeBinaryHeader(copy, id);
    memcpy(copy + BINARY_HEADER_BYTES, data, size);
    *frame = copy;
    return BinaryCopyResult::OK;
}

bool deadlineExpired(uint32_t now, uint32_t started, uint32_t timeout) {
    return static_cast<uint32_t>(now - started) >= timeout;
}

bool commandIsCurrent(uint32_t generation, uint32_t currentGeneration, uint32_t now, uint32_t started,
                      int64_t utcNowMs, int64_t expiresAtMs) {
    return generation == currentGeneration && !deadlineExpired(now, started, COMMAND_TIMEOUT_MS) &&
        absoluteDeadlineIsCurrent(utcNowMs, expiresAtMs);
}

bool absoluteDeadlineIsCurrent(int64_t utcNowMs, int64_t expiresAtMs) {
    return utcNowMs > 0 && expiresAtMs > utcNowMs &&
        expiresAtMs - utcNowMs <= COMMAND_TIMEOUT_MS + CLOCK_SKEW_TOLERANCE_MS;
}

bool validConfiguration(bool enabled, const char* host, unsigned port, const char* token, const char* ca) {
    if (!enabled || !host || !*host || strlen(host) > 253 || port == 0 || port > 65535 ||
        !token || !*token || strlen(token) > 512 || !ca || !*ca) return false;
    for (const char* p = host; *p; ++p) {
        if (!((*p >= 'a' && *p <= 'z') || (*p >= 'A' && *p <= 'Z') ||
              (*p >= '0' && *p <= '9') || *p == '-' || *p == '.')) return false;
    }
    for (const char* p = token; *p; ++p) {
        if (!((*p >= 'a' && *p <= 'z') || (*p >= 'A' && *p <= 'Z') ||
              (*p >= '0' && *p <= '9') || strchr("-._~+/=", *p))) return false;
    }
    return strstr(ca, "-----BEGIN CERTIFICATE-----") && strstr(ca, "-----END CERTIFICATE-----");
}

MessageBuffer::MessageBuffer(Allocate allocate) : allocate_(allocate) {}
MessageBuffer::~MessageBuffer() { clear(); }

bool MessageBuffer::begin(bool binary) {
    if (active()) return false;
    binary_ = binary;
    limit_ = binary ? MAX_FRAME_BYTES : MAX_TEXT_BYTES;
    data_ = static_cast<uint8_t*>(allocate_(limit_));
    size_ = 0;
    return data_ != nullptr;
}

bool MessageBuffer::append(const uint8_t* data, size_t size) {
    if (!active() || size > limit_ - size_ || (!data && size)) return false;
    if (size) memcpy(data_ + size_, data, size);
    size_ += size;
    return true;
}

void MessageBuffer::clear() {
    free(data_);
    data_ = nullptr;
    size_ = 0;
    limit_ = 0;
}

}  // namespace outbound

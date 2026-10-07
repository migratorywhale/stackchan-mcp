#include <unity.h>
#include <stdlib.h>
#include <string.h>
#include <algorithm>
#include <string>
#include <vector>

#include "outbound_protocol.h"

static constexpr const char* ID = "0123456789abcdef0123456789abcdef";
static constexpr const char* REQUEST =
    "{\"v\":1,\"id\":\"0123456789abcdef0123456789abcdef\","
    "\"expires_at_ms\":1800000000000,"
    "\"method\":\"POST\",\"path\":\"/face\",\"query\":{},\"body\":{\"face\":\"happy\"}}";

void setUp() {}
void tearDown() {}

static bool parse(const std::string& text, outbound::RequestInfo* result = nullptr) {
    JsonDocument doc;
    outbound::RequestInfo info;
    bool ok = outbound::parseRequest(reinterpret_cast<const uint8_t*>(text.data()), text.size(), doc, info);
    if (result) *result = info;
    return ok;
}

static std::string edited(const char* key, const char* value) {
    JsonDocument doc;
    deserializeJson(doc, REQUEST);
    doc[key] = value;
    std::string text;
    serializeJson(doc, text);
    return text;
}

static std::string pcmRequest(size_t size) {
    JsonDocument doc;
    deserializeJson(doc, REQUEST);
    doc["path"] = "/play/pcm";
    doc.remove("body");
    doc["binary_size"] = size;
    doc["query"]["session"] = "utterance";
    doc["query"]["seq"] = "0";
    doc["query"]["final"] = "1";
    std::string text;
    serializeJson(doc, text);
    return text;
}

static void test_valid_text() {
    outbound::RequestInfo info;
    TEST_ASSERT_TRUE(parse(REQUEST, &info));
    TEST_ASSERT_EQUAL_STRING(ID, info.id);
    TEST_ASSERT_EQUAL_UINT(0, info.binarySize);
    TEST_ASSERT_EQUAL_INT64(INT64_C(1800000000000), info.expiresAtMs);
    TEST_ASSERT_TRUE(parse(edited("method", "GET")));
}

static void test_ids_are_exact_lower_hex() {
    TEST_ASSERT_FALSE(parse(edited("id", "0123456789ABCDEF0123456789abcdef")));
    TEST_ASSERT_FALSE(parse(edited("id", "0123456789abcdef0123456789abcdeg")));
    TEST_ASSERT_FALSE(parse(edited("id", "short")));
    TEST_ASSERT_FALSE(parse(edited("id", "0123456789abcdef0123456789abcdef0")));
    TEST_ASSERT_FALSE(outbound::validId(nullptr, 32));
}

static void test_bad_envelope() {
    TEST_ASSERT_FALSE(parse("[]"));
    TEST_ASSERT_FALSE(parse("{}"));
    TEST_ASSERT_FALSE(parse("{"));
    TEST_ASSERT_FALSE(parse(edited("v", "1")));
    TEST_ASSERT_FALSE(parse(edited("body", "not an object")));
    TEST_ASSERT_FALSE(parse(edited("query", "not an object")));
    TEST_ASSERT_FALSE(parse(edited("method", "DELETE")));
    TEST_ASSERT_FALSE(parse(edited("path", "https://relay.test/face")));
    TEST_ASSERT_FALSE(parse(edited("path", "/face?token=x")));
}

static void test_text_size_boundary() {
    std::string exact(REQUEST);
    exact.append(outbound::MAX_TEXT_BYTES - exact.size(), ' ');
    TEST_ASSERT_TRUE(parse(exact));
    exact.push_back(' ');
    TEST_ASSERT_FALSE(parse(exact));
    TEST_ASSERT_FALSE(parse(""));
    TEST_ASSERT_FALSE(parse(std::string(REQUEST) + std::string(1, '\0')));
}

static void test_query_must_be_strings() {
    JsonDocument doc;
    deserializeJson(doc, REQUEST);
    doc["query"]["seq"] = 0;
    std::string text;
    serializeJson(doc, text);
    TEST_ASSERT_FALSE(parse(text));
    doc["query"]["seq"] = "0";
    doc["query"]["plain"] = "{}";
    text.clear();
    serializeJson(doc, text);
    TEST_ASSERT_FALSE(parse(text));
}

static void test_get_face_empty_body_is_not_a_post() {
    JsonDocument doc;
    deserializeJson(doc, REQUEST);
    TEST_ASSERT_TRUE(outbound::hasRequestBody(doc));
    doc["body"].to<JsonObject>();
    doc["method"] = "GET";
    TEST_ASSERT_FALSE(outbound::hasRequestBody(doc));
    doc["method"] = "POST";
    TEST_ASSERT_TRUE(outbound::hasRequestBody(doc));
    doc.remove("body");
    TEST_ASSERT_FALSE(outbound::hasRequestBody(doc));
}

static void test_get_audio_has_no_incoming_binary() {
    JsonDocument doc;
    deserializeJson(doc, REQUEST);
    doc["method"] = "GET";
    doc["path"] = "/audio";
    doc["body"].to<JsonObject>();
    std::string text;
    serializeJson(doc, text);
    outbound::RequestInfo info;
    TEST_ASSERT_TRUE(parse(text, &info));
    TEST_ASSERT_EQUAL_UINT(0, info.binarySize);
    TEST_ASSERT_FALSE(outbound::hasRequestBody(doc));
    doc["binary_size"] = 256044;
    text.clear();
    serializeJson(doc, text);
    TEST_ASSERT_FALSE(parse(text));
}

static void test_pcm_size_bounds() {
    TEST_ASSERT_TRUE(parse(pcmRequest(2)));
    TEST_ASSERT_TRUE(parse(pcmRequest(48 * 1024)));
    TEST_ASSERT_TRUE(parse(pcmRequest(outbound::MAX_BINARY_BYTES)));
    TEST_ASSERT_FALSE(parse(pcmRequest(0)));
    TEST_ASSERT_FALSE(parse(pcmRequest(3)));
    TEST_ASSERT_FALSE(parse(pcmRequest(outbound::MAX_BINARY_BYTES + 2)));
    TEST_ASSERT_FALSE(parse(pcmRequest(outbound::MAX_WAV_RESPONSE_BYTES)));
}

static void test_binary_only_on_pcm() {
    TEST_ASSERT_FALSE(parse(edited("binary_size", "2")));
    TEST_ASSERT_FALSE(parse(edited("path", "/play/pcm")));
    JsonDocument doc;
    deserializeJson(doc, pcmRequest(2));
    doc["method"] = "GET";
    std::string text;
    serializeJson(doc, text);
    TEST_ASSERT_FALSE(parse(text));
}

static void test_binary_matching_and_length() {
    outbound::RequestInfo info;
    TEST_ASSERT_TRUE(parse(pcmRequest(2), &info));
    uint8_t frame[outbound::BINARY_HEADER_BYTES + 2] = {};
    outbound::writeBinaryHeader(frame, info.id);
    TEST_ASSERT_TRUE(outbound::validBinary(frame, sizeof(frame), info));
    TEST_ASSERT_FALSE(outbound::validBinary(frame, sizeof(frame) - 1, info));
    TEST_ASSERT_FALSE(outbound::validBinary(frame, sizeof(frame) + 1, info));
    frame[0] = 'X';
    TEST_ASSERT_FALSE(outbound::validBinary(frame, sizeof(frame), info));
    frame[0] = 'S';
    frame[4] = 'f';
    TEST_ASSERT_FALSE(outbound::validBinary(frame, sizeof(frame), info));
    TEST_ASSERT_FALSE(outbound::validBinary(nullptr, sizeof(frame), info));
}

static void test_fragmented_binary() {
    outbound::RequestInfo info;
    TEST_ASSERT_TRUE(parse(pcmRequest(48 * 1024), &info));
    std::vector<uint8_t> frame(outbound::BINARY_HEADER_BYTES + info.binarySize, 0x55);
    outbound::writeBinaryHeader(frame.data(), info.id);
    outbound::MessageBuffer buffer(malloc);
    TEST_ASSERT_TRUE(buffer.begin(true));
    for (size_t offset = 0; offset < frame.size();) {
        size_t count = std::min(size_t(12 * 1024), frame.size() - offset);
        TEST_ASSERT_TRUE(buffer.append(frame.data() + offset, count));
        offset += count;
    }
    TEST_ASSERT_TRUE(outbound::validBinary(buffer.data(), buffer.size(), info));
    TEST_ASSERT_TRUE(buffer.binary());
    buffer.clear();
    TEST_ASSERT_FALSE(buffer.active());
    TEST_ASSERT_EQUAL_UINT(0, buffer.size());
}

static void test_fragment_overflow_and_interleaving() {
    outbound::MessageBuffer buffer(malloc);
    std::vector<uint8_t> text(outbound::MAX_TEXT_BYTES, ' ');
    TEST_ASSERT_TRUE(buffer.begin(false));
    TEST_ASSERT_FALSE(buffer.begin(true));
    TEST_ASSERT_TRUE(buffer.append(text.data(), text.size()));
    TEST_ASSERT_FALSE(buffer.append(text.data(), 1));
    buffer.clear();
    TEST_ASSERT_FALSE(buffer.append(text.data(), 1));
    TEST_ASSERT_TRUE(buffer.begin(true));
    std::vector<uint8_t> binary(outbound::MAX_FRAME_BYTES, 0);
    TEST_ASSERT_TRUE(buffer.append(binary.data(), binary.size()));
    TEST_ASSERT_FALSE(buffer.append(binary.data(), 1));
    TEST_ASSERT_FALSE(buffer.append(binary.data(), SIZE_MAX));
}

static void* failAllocation(size_t) { return nullptr; }

static size_t responseAllocationSize = 0;

static void* countedAllocation(size_t size) {
    responseAllocationSize = size;
    return malloc(size);
}

static void test_full_recording_copy_owns_payload() {
    // Eight seconds of 16 kHz mono s16le plus the WAV header.
    std::vector<uint8_t> recording(256044, 0x5a);
    memcpy(recording.data(), "RIFF", 4);
    uint8_t* frame = nullptr;
    responseAllocationSize = 0;
    TEST_ASSERT_TRUE(outbound::copyBinaryResponse(ID, "audio/wav", recording.data(),
        recording.size(), &frame, countedAllocation) == outbound::BinaryCopyResult::OK);
    TEST_ASSERT_EQUAL_UINT(recording.size() + outbound::BINARY_HEADER_BYTES, responseAllocationSize);
    TEST_ASSERT_NOT_NULL(frame);
    TEST_ASSERT_EQUAL_MEMORY("SCB1", frame, 4);
    TEST_ASSERT_EQUAL_MEMORY(ID, frame + 4, outbound::ID_BYTES);
    TEST_ASSERT_EQUAL_MEMORY(recording.data(), frame + outbound::BINARY_HEADER_BYTES, recording.size());
    std::fill(recording.begin(), recording.end(), 0);
    TEST_ASSERT_EQUAL_MEMORY("RIFF", frame + outbound::BINARY_HEADER_BYTES, 4);
    TEST_ASSERT_EQUAL_UINT8(0x5a, frame[outbound::BINARY_HEADER_BYTES + 256043]);
    free(frame);
}

static void test_response_limits_are_content_specific() {
    std::vector<uint8_t> data(outbound::MAX_WAV_RESPONSE_BYTES, 0x3c);
    TEST_ASSERT_EQUAL_UINT(512 * 1024, outbound::binaryResponseLimit("audio/wav"));
    TEST_ASSERT_EQUAL_UINT(128 * 1024, outbound::binaryResponseLimit("image/jpeg"));
    TEST_ASSERT_EQUAL_UINT(128 * 1024, outbound::binaryResponseLimit("application/octet-stream"));
    TEST_ASSERT_EQUAL_UINT(128 * 1024 + 36, outbound::MAX_FRAME_BYTES);
    for (const char* contentType : {"audio/wav", "image/jpeg", "application/octet-stream"}) {
        const size_t limit = outbound::binaryResponseLimit(contentType);
        uint8_t* frame = nullptr;
        TEST_ASSERT_TRUE(outbound::copyBinaryResponse(ID, contentType, data.data(), limit,
            &frame, countedAllocation) == outbound::BinaryCopyResult::OK);
        TEST_ASSERT_EQUAL_UINT(limit + outbound::BINARY_HEADER_BYTES, responseAllocationSize);
        TEST_ASSERT_EQUAL_MEMORY(data.data(), frame + outbound::BINARY_HEADER_BYTES, limit);
        free(frame);
        frame = nullptr;
        responseAllocationSize = 0;
        TEST_ASSERT_TRUE(outbound::copyBinaryResponse(ID, contentType, data.data(), limit + 1,
            &frame, countedAllocation) == outbound::BinaryCopyResult::TOO_LARGE);
        TEST_ASSERT_NULL(frame);
        TEST_ASSERT_EQUAL_UINT(0, responseAllocationSize);
    }
    uint8_t* frame = nullptr;
    TEST_ASSERT_TRUE(outbound::copyBinaryResponse(ID, "audio/wav", data.data(), SIZE_MAX,
        &frame, countedAllocation) == outbound::BinaryCopyResult::TOO_LARGE);
    TEST_ASSERT_NULL(frame);
    TEST_ASSERT_EQUAL_UINT(0, responseAllocationSize);
}

static void test_failed_response_copy_leaves_recording_untouched() {
    const uint8_t recording[] = {'R', 'I', 'F', 'F'};
    uint8_t* frame = nullptr;
    TEST_ASSERT_TRUE(outbound::copyBinaryResponse(ID, "audio/wav", recording, sizeof(recording),
        &frame, failAllocation) == outbound::BinaryCopyResult::NO_MEMORY);
    TEST_ASSERT_NULL(frame);
    TEST_ASSERT_EQUAL_MEMORY("RIFF", recording, sizeof(recording));
    TEST_ASSERT_TRUE(outbound::copyBinaryResponse(ID, "audio/wav", recording,
        outbound::MAX_WAV_RESPONSE_BYTES + 1, &frame, failAllocation) == outbound::BinaryCopyResult::TOO_LARGE);
    TEST_ASSERT_NULL(frame);
    TEST_ASSERT_EQUAL_MEMORY("RIFF", recording, sizeof(recording));
}

static void test_response_copy_rejects_invalid_input() {
    const uint8_t data[] = {0};
    uint8_t* frame = nullptr;
    TEST_ASSERT_TRUE(outbound::copyBinaryResponse("invalid", "audio/wav", data, 1,
        &frame, malloc) == outbound::BinaryCopyResult::INVALID);
    TEST_ASSERT_TRUE(outbound::copyBinaryResponse(ID, "audio/wav", nullptr, 1,
        &frame, malloc) == outbound::BinaryCopyResult::INVALID);
    TEST_ASSERT_TRUE(outbound::copyBinaryResponse(ID, "audio/wav", data, 0,
        &frame, malloc) == outbound::BinaryCopyResult::INVALID);
    TEST_ASSERT_TRUE(outbound::copyBinaryResponse(ID, nullptr, data, 1,
        &frame, malloc) == outbound::BinaryCopyResult::INVALID);
    TEST_ASSERT_TRUE(outbound::copyBinaryResponse(ID, "audio/wav", data, 1,
        &frame, nullptr) == outbound::BinaryCopyResult::INVALID);
    TEST_ASSERT_TRUE(outbound::copyBinaryResponse(ID, "audio/wav", data, 1,
        nullptr, malloc) == outbound::BinaryCopyResult::INVALID);
    TEST_ASSERT_NULL(frame);
}

static void test_allocation_failure() {
    outbound::MessageBuffer buffer(failAllocation);
    TEST_ASSERT_FALSE(buffer.begin(true));
    TEST_ASSERT_FALSE(buffer.active());
    TEST_ASSERT_FALSE(buffer.append(nullptr, 1));
}

static void test_stale_generation_and_deadline() {
    TEST_ASSERT_TRUE(outbound::commandIsCurrent(1, 1, 1100, 1000, 100000, 120000));
    TEST_ASSERT_FALSE(outbound::commandIsCurrent(1, 2, 1100, 1000, 100000, 120000));
    TEST_ASSERT_FALSE(outbound::commandIsCurrent(1, 1, 21000, 1000, 100000, 120000));
    TEST_ASSERT_TRUE(outbound::commandIsCurrent(1, 1, 0x20, 0xfffffff0, 100000, 120000));
    TEST_ASSERT_TRUE(outbound::deadlineExpired(0x20, 0xfffffff0, 48));
}

static void test_absolute_deadline_rejects_tcp_delayed_request() {
    TEST_ASSERT_FALSE(outbound::commandIsCurrent(1, 1, 1100, 1000, 120000, 120000));
    TEST_ASSERT_FALSE(outbound::commandIsCurrent(1, 1, 1100, 1000, 120001, 120000));
    TEST_ASSERT_FALSE(outbound::commandIsCurrent(1, 1, 1100, 1000, 0, 120000));
    TEST_ASSERT_FALSE(outbound::commandIsCurrent(1, 1, 1100, 1000, 100000, 0));
}

static void test_deadline_is_mandatory_positive_int64() {
    TEST_ASSERT_FALSE(parse(edited("expires_at_ms", "1800000000000")));
    for (const char* value : {"0", "-1", "1.5", "true", "null", "9223372036854775808"}) {
        JsonDocument doc;
        deserializeJson(doc, REQUEST);
        JsonDocument deadline;
        deserializeJson(deadline, value);
        doc["expires_at_ms"] = deadline;
        std::string text;
        serializeJson(doc, text);
        TEST_ASSERT_FALSE(parse(text));
    }
    JsonDocument doc;
    deserializeJson(doc, REQUEST);
    doc.remove("expires_at_ms");
    std::string text;
    serializeJson(doc, text);
    TEST_ASSERT_FALSE(parse(text));
}

static void test_far_future_deadline_fails_closed() {
    constexpr int64_t now = INT64_C(1800000000000);
    TEST_ASSERT_TRUE(outbound::absoluteDeadlineIsCurrent(now, now + 25000));
    TEST_ASSERT_FALSE(outbound::absoluteDeadlineIsCurrent(now, now + 25001));
    TEST_ASSERT_FALSE(outbound::absoluteDeadlineIsCurrent(1, INT64_MAX));
    TEST_ASSERT_TRUE(outbound::absoluteDeadlineIsCurrent(INT64_MAX - 100, INT64_MAX));
    TEST_ASSERT_FALSE(outbound::absoluteDeadlineIsCurrent(INT64_MAX, INT64_MAX - 1));
    TEST_ASSERT_FALSE(outbound::absoluteDeadlineIsCurrent(-1, now));
}

static void test_configuration_fails_closed() {
    const char* ca = "-----BEGIN CERTIFICATE-----\nplaceholder\n-----END CERTIFICATE-----";
    TEST_ASSERT_TRUE(outbound::validConfiguration(true, "relay.example", 443, "test-token", ca));
    TEST_ASSERT_FALSE(outbound::validConfiguration(false, "relay.example", 443, "test-token", ca));
    TEST_ASSERT_FALSE(outbound::validConfiguration(true, "relay.example", 443, "", ca));
    TEST_ASSERT_FALSE(outbound::validConfiguration(true, "relay.example", 443, "token\r\nX: y", ca));
    TEST_ASSERT_FALSE(outbound::validConfiguration(true, "https://relay.example", 443, "token", ca));
    TEST_ASSERT_FALSE(outbound::validConfiguration(true, "relay.example@evil", 443, "token", ca));
    TEST_ASSERT_FALSE(outbound::validConfiguration(true, "relay.example", 0, "token", ca));
    TEST_ASSERT_FALSE(outbound::validConfiguration(true, "relay.example", 65536, "token", ca));
    TEST_ASSERT_FALSE(outbound::validConfiguration(true, "relay.example", 443, "token", ""));
}

int main() {
    UNITY_BEGIN();
    RUN_TEST(test_valid_text);
    RUN_TEST(test_ids_are_exact_lower_hex);
    RUN_TEST(test_bad_envelope);
    RUN_TEST(test_text_size_boundary);
    RUN_TEST(test_query_must_be_strings);
    RUN_TEST(test_get_face_empty_body_is_not_a_post);
    RUN_TEST(test_get_audio_has_no_incoming_binary);
    RUN_TEST(test_pcm_size_bounds);
    RUN_TEST(test_binary_only_on_pcm);
    RUN_TEST(test_binary_matching_and_length);
    RUN_TEST(test_fragmented_binary);
    RUN_TEST(test_fragment_overflow_and_interleaving);
    RUN_TEST(test_allocation_failure);
    RUN_TEST(test_full_recording_copy_owns_payload);
    RUN_TEST(test_response_limits_are_content_specific);
    RUN_TEST(test_failed_response_copy_leaves_recording_untouched);
    RUN_TEST(test_response_copy_rejects_invalid_input);
    RUN_TEST(test_stale_generation_and_deadline);
    RUN_TEST(test_absolute_deadline_rejects_tcp_delayed_request);
    RUN_TEST(test_deadline_is_mandatory_positive_int64);
    RUN_TEST(test_far_future_deadline_fails_closed);
    RUN_TEST(test_configuration_fails_closed);
    return UNITY_END();
}

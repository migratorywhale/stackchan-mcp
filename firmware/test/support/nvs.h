#pragma once
#include <stdint.h>

using nvs_handle_t = unsigned;
using esp_err_t = int;
constexpr int ESP_OK = 0;
constexpr int ESP_FAIL = -1;
constexpr int ESP_ERR_NVS_NOT_FOUND = 1;
constexpr int ESP_ERR_NVS_TYPE_MISMATCH = 2;
constexpr int NVS_READWRITE = 1;
struct TestNvs {
    bool exists = false;
    uint8_t value = 0;
    bool beginFails = false;
    bool writeFails = false;
    bool readFails = false;
    bool commitFails = false;
    bool invalidType = false;
    unsigned writes = 0;
};
inline TestNvs testNvs;

inline esp_err_t nvs_open(const char*, int, nvs_handle_t* handle) {
    *handle = 1;
    return testNvs.beginFails ? ESP_FAIL : ESP_OK;
}
inline void nvs_close(nvs_handle_t) {}
inline esp_err_t nvs_get_u8(nvs_handle_t, const char*, uint8_t* value) {
    if (testNvs.readFails) return ESP_FAIL;
    if (!testNvs.exists) return ESP_ERR_NVS_NOT_FOUND;
    if (testNvs.invalidType) return ESP_ERR_NVS_TYPE_MISMATCH;
    *value = testNvs.value;
    return ESP_OK;
}
inline esp_err_t nvs_set_u8(nvs_handle_t, const char*, uint8_t value) {
    ++testNvs.writes;
    if (testNvs.writeFails) return ESP_FAIL;
    testNvs.exists = true;
    testNvs.value = value;
    return ESP_OK;
}
inline esp_err_t nvs_commit(nvs_handle_t) { return testNvs.commitFails ? ESP_FAIL : ESP_OK; }

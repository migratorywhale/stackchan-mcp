#pragma once

#include <algorithm>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <string>

using std::min;

class String {
public:
    String(const char* text = "") : text_(text) {}
    size_t length() const { return text_.size(); }
    bool isEmpty() const { return text_.empty(); }
    const char* c_str() const { return text_.c_str(); }
    void toCharArray(char* out, size_t size) const {
        if (size) { strncpy(out, text_.c_str(), size); out[size - 1] = 0; }
    }
    bool operator==(const String& other) const { return text_ == other.text_; }
    bool operator!=(const String& other) const { return !(*this == other); }
private:
    std::string text_;
};

template <typename T> T constrain(T value, T low, T high) {
    return std::max(low, std::min(high, value));
}

inline uint32_t testMillis = 10000;
inline void (*testDelayHook)(uint32_t) = nullptr;
inline uint32_t millis() { return testMillis; }
inline void delay(uint32_t ms) { testMillis += ms; }
inline void* ps_malloc(size_t size) { return malloc(size); }

using QueueHandle_t = void*;
using TaskHandle_t = void*;
using BaseType_t = int;
constexpr int pdTRUE = 1;
constexpr int pdPASS = 1;
constexpr uint32_t portMAX_DELAY = UINT32_MAX;
inline uint32_t pdMS_TO_TICKS(uint32_t ms) { return ms; }
inline void vTaskDelay(uint32_t ms) { delay(ms); if (testDelayHook) testDelayHook(ms); }
inline QueueHandle_t xQueueCreate(size_t, size_t) { return reinterpret_cast<void*>(1); }
inline int xQueueSend(QueueHandle_t, const void*, uint32_t) { return pdTRUE; }
inline int xQueueReceive(QueueHandle_t, void*, uint32_t) { return 0; }
inline void vQueueDelete(QueueHandle_t) {}
inline size_t uxQueueMessagesWaiting(QueueHandle_t) { return 0; }
inline int xTaskCreatePinnedToCore(void (*)(void*), const char*, size_t, void*, int, TaskHandle_t* task, int) {
    *task = reinterpret_cast<void*>(1);
    return pdPASS;
}

struct TestSerial {
    template <typename... T> void printf(const char*, T...) {}
    void println(const char*) {}
    void flush() {}
};
struct TestEsp { void restart() {} };
inline TestSerial Serial;
inline TestEsp ESP;

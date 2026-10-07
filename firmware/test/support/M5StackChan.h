#pragma once
#include <array>
#include <stdint.h>

struct TestTouchSensor {
    std::array<uint8_t, 3> intensities = {};
    bool hold = false;
    bool clicked = false;
    void begin() {}
    const std::array<uint8_t, 3>& getIntensities() const { return intensities; }
    bool wasHold() const { return hold; }
    bool wasClicked() const { return clicked; }
};
struct TestStackChan { TestTouchSensor TouchSensor; };
inline TestStackChan M5StackChan;

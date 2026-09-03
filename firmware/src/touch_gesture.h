#pragma once

#include <stdint.h>

enum class TouchSwipeDirection : uint8_t {
    FORWARD,
    BACKWARD,
};

struct TouchTrajectoryResult {
    bool hasSwipe = false;
    TouchSwipeDirection direction = TouchSwipeDirection::FORWARD;
    bool suppressClick = false;
};

class TouchTrajectoryDetector {
public:
    TouchTrajectoryResult update(const uint8_t intensities[3]) {
        TouchTrajectoryResult result;
        const bool anyTouched = intensities[0] > 0 || intensities[1] > 0 || intensities[2] > 0;
        if (!anyTouched) {
            result.suppressClick = contactActive_ && strokeObserved_;
            reset();
            return result;
        }

        if (!contactActive_) {
            contactActive_ = true;
            lastZone_ = Zone::NONE;
            strokeObserved_ = false;
        }

        result.suppressClick = strokeObserved_;
        const Zone zone = classifyZone(intensities);
        if (zone == Zone::NONE) {
            return result;
        }
        if (lastZone_ == Zone::NONE) {
            lastZone_ = zone;
            return result;
        }
        if (zone == lastZone_ || !shouldTransition(lastZone_, zone, intensities)) {
            return result;
        }

        result.hasSwipe = true;
        result.direction = zoneIndex(zone) > zoneIndex(lastZone_) ? TouchSwipeDirection::FORWARD
                                                                 : TouchSwipeDirection::BACKWARD;
        result.suppressClick = true;
        strokeObserved_ = true;
        lastZone_ = zone;
        return result;
    }

    void reset() {
        contactActive_ = false;
        strokeObserved_ = false;
        lastZone_ = Zone::NONE;
    }

private:
    enum class Zone : uint8_t {
        NONE,
        FRONT,
        MIDDLE,
        BACK,
    };

    static Zone classifyZone(const uint8_t intensities[3]) {
        int8_t bestIndex = -1;
        uint8_t bestIntensity = 0;
        for (int8_t index = 0; index < 3; ++index) {
            if (intensities[index] > 0 && intensities[index] >= bestIntensity) {
                bestIndex = index;
                bestIntensity = intensities[index];
            }
        }
        return bestIndex < 0 ? Zone::NONE : static_cast<Zone>(bestIndex + 1);
    }

    static int8_t zoneIndex(Zone zone) {
        return static_cast<int8_t>(zone) - 1;
    }

    static bool shouldTransition(Zone current, Zone candidate, const uint8_t intensities[3]) {
        constexpr uint8_t MIN_FORWARD_INTENSITY = 2;
        constexpr uint8_t REVERSE_HYSTERESIS = 2;

        const uint8_t candidateIntensity = intensities[zoneIndex(candidate)];
        if (zoneIndex(candidate) > zoneIndex(current)) {
            return candidateIntensity >= MIN_FORWARD_INTENSITY;
        }

        const uint8_t currentIntensity = intensities[zoneIndex(current)];
        return candidateIntensity >= currentIntensity + REVERSE_HYSTERESIS;
    }

    bool contactActive_ = false;
    bool strokeObserved_ = false;
    Zone lastZone_ = Zone::NONE;
};

class PettingDetector {
public:
    explicit PettingDetector(uint32_t windowMs = 1500) : windowMs_(windowMs) {}

    bool observe(TouchSwipeDirection direction, uint32_t nowMs) {
        if (direction == TouchSwipeDirection::FORWARD) {
            if (hasBackward_ && nowMs - backwardMs_ <= windowMs_) {
                reset();
                return true;
            }
            forwardMs_ = nowMs;
            hasForward_ = true;
            return false;
        }

        if (hasForward_ && nowMs - forwardMs_ <= windowMs_) {
            reset();
            return true;
        }
        backwardMs_ = nowMs;
        hasBackward_ = true;
        return false;
    }

    void reset() {
        hasForward_ = false;
        hasBackward_ = false;
        forwardMs_ = 0;
        backwardMs_ = 0;
    }

private:
    uint32_t windowMs_;
    bool hasForward_ = false;
    bool hasBackward_ = false;
    uint32_t forwardMs_ = 0;
    uint32_t backwardMs_ = 0;
};

#include "face_names.h"

#include <string.h>

static const int NUM_FACES = 7;

const char* whaleFaceName(WhaleFace face) {
    switch (face) {
        case WHALE_CALM:     return "calm";
        case WHALE_THINKING: return "thinking";
        case WHALE_HAPPY:    return "happy";
        case WHALE_SLEEPY:   return "sleepy";
        case WHALE_SHY:      return "shy";
        case WHALE_SMUG:     return "smug";
        case WHALE_POUTY:    return "pouty";
        default:             return "unknown";
    }
}

bool whaleFaceFromName(const char* name, WhaleFace* face) {
    if (!name || !face) {
        return false;
    }
    for (int i = 0; i < NUM_FACES; i++) {
        WhaleFace candidate = (WhaleFace)i;
        if (strcmp(name, whaleFaceName(candidate)) == 0) {
            *face = candidate;
            return true;
        }
    }
    return false;
}


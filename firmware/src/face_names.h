#pragma once

// Whale face names (for HTTP/MCP direct control)
enum WhaleFace {
    WHALE_CALM     = 0,
    WHALE_THINKING = 1,
    WHALE_HAPPY    = 2,
    WHALE_SLEEPY   = 3,
    WHALE_SHY      = 4,
    WHALE_SMUG     = 5,
    WHALE_POUTY    = 6,
};

const char* whaleFaceName(WhaleFace face);
bool whaleFaceFromName(const char* name, WhaleFace* face);


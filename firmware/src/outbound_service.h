#pragma once
#include <stdint.h>

void initOutboundService();
void serviceOutboundCommands();
// Thread-safe cancellation check; does not access WebSockets or hardware.
bool isOutboundGenerationCurrent(uint32_t generation);

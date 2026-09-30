#pragma once

#include <Arduino.h>
#include "outbound_protocol.h"

struct FirmwareCommand {
    JsonDocument request;
    outbound::RequestInfo info;
    uint32_t generation = 0;
    uint32_t startedMs = 0;
    uint8_t* pcmData = nullptr;
    int status = 500;
    String body;
    String contentType;
    uint8_t* binaryFrame = nullptr;
    size_t binarySize = 0;

    void clear() {
        request.clear();
        info = {};
        free(pcmData);
        pcmData = nullptr;
        free(binaryFrame);
        binaryFrame = nullptr;
        binarySize = 0;
        status = 500;
        body = "";
        contentType = "";
    }
};

// Main-loop only. Shares handlers with local HTTP, never called by the WSS task.
void executeFirmwareCommand(FirmwareCommand& command);

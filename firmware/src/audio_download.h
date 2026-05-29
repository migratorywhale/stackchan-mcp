#pragma once

#include <Arduino.h>

bool downloadVoice(const String& url, uint8_t** outData, size_t* outSize);

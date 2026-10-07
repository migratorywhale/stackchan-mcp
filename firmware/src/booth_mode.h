#pragma once

// Main-loop only. The persisted setting is loaded before microphone startup.
bool initBoothMode();
bool isBoothMode();
bool isBoothModePersisted();
bool setBoothMode(bool enabled);

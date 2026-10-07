#ifndef STACKCHAN_CONFIG_LOADER_H
#define STACKCHAN_CONFIG_LOADER_H

#if defined(STACKCHAN_USE_PUBLIC_CONFIG)
#include "../config.h.example"
#elif __has_include("config.h")
#include "config.h"
#endif

#include "config_defaults.h"

#endif

# Third-Party Notices

Stack-chan MCP is distributed under the MIT License. It uses third-party code
and libraries whose licenses remain with their respective copyright holders.

## Vendored code

### SCServo

Files under `firmware/src/drivers/SCServo/` are an adapted Arduino/C++ port of
the SCServo communication library published at
<https://github.com/IS2511/SCServo>. The upstream project is MIT licensed,
copyright (c) 2018 Ivan. Its license text is preserved in
`firmware/src/drivers/SCServo/LICENSE`.

The copies in this repository include formatting, translation, and integration
changes. Do not remove the upstream notice when redistributing them.

## Build-time dependencies

The host application resolves its Python dependencies from `uv.lock`. Firmware
dependencies and their versions are declared in `firmware/platformio.ini`.
Those packages are not relicensed by this repository. Binary distributors must
review and comply with the license files shipped by each resolved dependency,
including transitive PlatformIO packages.

The principal directly declared projects are:

- MCP Python SDK: <https://github.com/modelcontextprotocol/python-sdk>
- Requests: <https://github.com/psf/requests>
- M5Unified, M5GFX, and StackChan-BSP: <https://github.com/m5stack>
- ArduinoJson: <https://github.com/bblanchon/ArduinoJson>
- AnimatedGIF: <https://github.com/bitbank2/AnimatedGIF>

The current firmware dependency graph also resolves additional PlatformIO
packages transitively through the directly declared libraries above. At the
time of writing, that includes M5Unit-NFC, M5UnitUnified, M5HAL, M5Utility,
and IRremoteESP8266 (LGPL-2.1-or-later via `StackChan-BSP`). Distributors must
review the resolved lockstep package set in their build output, not just the
direct declarations in `firmware/platformio.ini`.

## Face artwork

The face artwork in `faces/`, `firmware/data/`, and the compiled representation
in `firmware/src/gif_assets.h` is distributed as part of this project under the
repository MIT License. Maintainers must verify and record the authorship of
new or replacement artwork before accepting it. Do not submit artwork copied
from another project unless its license permits redistribution and its source
and license are added to this file.

This notice is informational and is not a substitute for the license texts
included with resolved dependencies or release artifacts.

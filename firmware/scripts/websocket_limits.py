"""Bound the pinned WebSockets receive frame size in this build's local copy."""

import json
from pathlib import Path

Import("env")  # noqa: F821 - SCons injects Import and the requested environment.


def patch_websocket_header(env, node):
    source = Path(node.srcnode().get_abspath()).resolve()
    root = Path(env.subst("$PROJECT_LIBDEPS_DIR")).resolve()
    if root not in source.parents or source.name != "WebSockets.cpp":
        raise RuntimeError(f"Refusing to patch a non-local WebSockets dependency: {source}")
    manifest = source.parent.parent / "library.json"
    if json.loads(manifest.read_text())["version"] != "2.7.2":
        raise RuntimeError("Recheck the WebSockets frame-limit patch for this version")
    header = source.with_name("WebSockets.h")
    original = "#define WEBSOCKETS_MAX_DATA_SIZE (15 * 1024)"
    replacement = (
        "#ifndef WEBSOCKETS_MAX_DATA_SIZE\n"
        + original
        + "\n#endif // Stackchan bounded frame override"
    )
    text = header.read_text()
    if "#endif // Stackchan bounded frame override" not in text:
        if original not in text:
            raise RuntimeError("WebSockets frame-limit declaration changed")
        header.write_text(text.replace(original, replacement))
    original = "payload = (uint8_t *)malloc(header->payloadLen + 1);"
    replacement = (
        "#if defined(ESP32)\n"
        "        payload = (uint8_t *)ps_malloc(header->payloadLen + 1);\n"
        "#else\n        " + original + "\n"
        "#endif // Stackchan PSRAM receive buffer"
    )
    text = source.read_text()
    if "#endif // Stackchan PSRAM receive buffer" not in text:
        if text.count(original) != 1:
            raise RuntimeError("WebSockets receive allocation changed")
        source.write_text(text.replace(original, replacement))
    return node


env.AddBuildMiddleware(patch_websocket_header, "*/WebSockets/src/WebSockets.cpp")  # noqa: F821 - SCons supplies env.

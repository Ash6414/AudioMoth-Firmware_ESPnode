#!/usr/bin/env python3
"""Patch AudioMoth ESP bridge so PA7 is not a hard UART-service gate.

The ESP32 still drives ESP_REQ, but this prototype build must also answer UART
when PA7 is not being sampled correctly. File commands remain guarded by the
scheduler-controlled uploadAllowed flag.
"""

from __future__ import annotations

from pathlib import Path

BRIDGE_C = Path("project/src/espbridge.c")

OLD_REQUIRE = "#define BRIDGE_REQUIRE_REQ_PIN              1"
NEW_REQUIRE = "#define BRIDGE_REQUIRE_REQ_PIN              0"

OLD_SERVICE_READ = "        bool requestActive = rawRequestPinActive();"
NEW_SERVICE_READ = "        bool requestActive = ESPBridge_isRequestActive();"


def replace_once(text: str, old: str, new: str, label: str) -> tuple[str, bool]:
    if new in text:
        return text, False
    if old not in text:
        raise SystemExit(f"Could not find ESP bridge {label} text to patch")
    return text.replace(old, new, 1), True


def main() -> None:
    text = BRIDGE_C.read_text(encoding="utf-8")
    text, require_changed = replace_once(text, OLD_REQUIRE, NEW_REQUIRE, "request-gate define")
    text, service_changed = replace_once(text, OLD_SERVICE_READ, NEW_SERVICE_READ, "service request check")
    BRIDGE_C.write_text(text, encoding="utf-8")

    if require_changed:
        print("Applied ESP optional request-gate patch")
    else:
        print("ESP optional request-gate patch already applied")

    if service_changed:
        print("Applied ESP optional service-loop patch")
    else:
        print("ESP optional service-loop patch already applied")


if __name__ == "__main__":
    main()

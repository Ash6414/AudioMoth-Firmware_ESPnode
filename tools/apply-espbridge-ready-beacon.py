#!/usr/bin/env python3
"""Patch AudioMoth ESP bridge for transfer-ready service windows.

AudioMoth emits READY periodically so the ESP32 can catch the bridge even after
Wi-Fi/server startup delays. The service cap is long enough for real WAV chunk
transfer while ESP_REQ is held high.
"""

from __future__ import annotations

from pathlib import Path

BRIDGE_C = Path("project/src/espbridge.c")

REPLACEMENTS = [
    (
        "#define SERVICE_IDLE_TIMEOUT_MS             3000",
        "#define SERVICE_IDLE_TIMEOUT_MS             30000",
        "service idle timeout",
    ),
    (
        "#define SERVICE_MAX_WINDOW_MS               30000",
        "#define SERVICE_MAX_WINDOW_MS               7200000",
        "service max window",
    ),
    (
        "#define MILLISECONDS_PER_SECOND             1000",
        "#define SERVICE_READY_BEACON_MS             1000\n#define MILLISECONDS_PER_SECOND             1000",
        "ready beacon constant",
    ),
    (
        "    uint32_t idleMs = 0;\n    uint32_t serviceStartSeconds, serviceStartMilliseconds;",
        "    uint32_t idleMs = 0;\n    uint32_t readyBeaconMs = 0;\n    uint32_t serviceStartSeconds, serviceStartMilliseconds;",
        "ready beacon counter",
    ),
    (
        "        if (deadlineDone && !requestActive && !rxAvailable) break;",
        "        if (deadlineDone && !requestActive && !rxAvailable && idleMs >= SERVICE_IDLE_TIMEOUT_MS) break;",
        "deadline idle grace",
    ),
    (
        "        } else {\n            idleMs += RX_LINE_TIMEOUT_MS;\n        }",
        "        } else {\n            idleMs += RX_LINE_TIMEOUT_MS;\n            readyBeaconMs += RX_LINE_TIMEOUT_MS;\n            if (readyBeaconMs >= SERVICE_READY_BEACON_MS) {\n                sendLine(\"OK BRIDGE_READY\");\n                readyBeaconMs = 0;\n            }\n        }",
        "ready beacon send",
    ),
]


def replace_once(text: str, old: str, new: str, label: str) -> tuple[str, bool]:
    if new in text:
        return text, False
    if label == "service max window" and "#define SERVICE_MAX_WINDOW_MS               120000" in text:
        return text.replace("#define SERVICE_MAX_WINDOW_MS               120000", new, 1), True
    if old not in text:
        raise SystemExit(f"Could not find ESP bridge {label} text to patch")
    return text.replace(old, new, 1), True


def main() -> None:
    text = BRIDGE_C.read_text(encoding="utf-8")
    changed_labels = []
    for old, new, label in REPLACEMENTS:
        text, changed = replace_once(text, old, new, label)
        if changed:
            changed_labels.append(label)
    BRIDGE_C.write_text(text, encoding="utf-8")

    if changed_labels:
        print("Applied ESP bridge READY beacon patch: " + ", ".join(changed_labels))
    else:
        print("ESP bridge READY beacon patch already applied")


if __name__ == "__main__":
    main()

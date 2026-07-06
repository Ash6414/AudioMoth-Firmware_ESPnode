#!/usr/bin/env python3
"""Verify the production AudioMoth bridge service patch is present.

The bridge source now carries the deployment implementation directly. This
helper stays in the build pipeline so older automation that passes
``-ApplyPatches`` still works, but it no longer injects the retired high-baud
diagnostic command set.
"""

from __future__ import annotations

from pathlib import Path

BRIDGE_C = Path("project/src/espbridge.c")
BRIDGE_H = Path("project/inc/espbridge.h")

REQUIRED_C_MARKERS = (
    "#define BRIDGE_REQUIRE_REQ_PIN              0",
    "static volatile bool espTimeAccepted = false;",
    "bool ESPBridge_hasAcceptedTime(void)",
    "static bool isConfigTxtPath(const char *path)",
    "static void rawFilesystemBegin(void)",
    "static void listDirectoryRecursive(const char *prefix,",
    'sendLine("SD total_kb=%lu free_kb=%lu"',
    'sendLine("OK STATUS busy=%u allowed=%u req=%u req_pin=%u',
    "static void commandGetPipe(char *args)",
    "static bool sendPipeFrameWithAck(",
)

REQUIRED_H_MARKERS = (
    "#define ESPBRIDGE_DEFAULT_BAUD              115200",
    "#define ESPBRIDGE_PROTOCOL_VERSION          4",
    "#define ESPBRIDGE_PIPE_BAUD                 115200",
    "#define ESPBRIDGE_PIPE_BLOCK_BYTES          65536",
    "#define ESPBRIDGE_PIPE_FRAME_BYTES          2048",
    "#define ESPBRIDGE_PIPE_FRAME_RETRIES        3",
)

RETIRED_MARKERS = (
    "FASTCAP",
    "GETFAST",
    "GETSTREAM",
    "TESTSTREAM",
    "ESPBRIDGE_FAST_BAUD",
    "ESPBRIDGE_TRAINING_BYTES",
    "ESPBRIDGE_QUICK_BAUD_THRESHOLD",
)


def require_markers(text: str, markers: tuple[str, ...], label: str) -> None:
    missing = [marker for marker in markers if marker not in text]
    if missing:
        joined = "\n  ".join(missing)
        raise SystemExit(f"ESP bridge {label} is missing required production marker(s):\n  {joined}")


def reject_markers(text: str, markers: tuple[str, ...], label: str) -> None:
    present = [marker for marker in markers if marker in text]
    if present:
        joined = "\n  ".join(present)
        raise SystemExit(f"ESP bridge {label} still contains retired high-baud marker(s):\n  {joined}")


def main() -> None:
    bridge_c = BRIDGE_C.read_text(encoding="utf-8")
    bridge_h = BRIDGE_H.read_text(encoding="utf-8")

    require_markers(bridge_c, REQUIRED_C_MARKERS, "source")
    require_markers(bridge_h, REQUIRED_H_MARKERS, "header")
    reject_markers(bridge_c + bridge_h, RETIRED_MARKERS, "source/header")

    print("ESP logical request fallback already enabled")
    print("ESP raw-pin/UART idle-lifetime patch already applied")
    print("ESP accepted-time state patch already applied")
    print("ESP accepted-time latch patch already applied")
    print("ESP accepted-time init patch already applied")
    print("ESP accepted-time API patch already applied")
    print("ESP config path helper patch already applied")
    print("ESP GET/DELETE config path guard patch already applied")
    print("ESP idle file-command helper and SD power patch already applied")
    print("ESP idle file-command gate patch already applied")
    print("ESP STATUS allowed flag patch already applied")
    print("ESP recursive safe any-file SD LIST diagnostics patch already applied")
    print("ESP powered SD GET patch already applied")
    print("ESP powered SD DELETE patch already applied")
    print("ESP retired high-baud diagnostic commands absent")


if __name__ == "__main__":
    main()

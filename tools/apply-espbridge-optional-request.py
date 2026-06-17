#!/usr/bin/env python3
"""Patch AudioMoth ESP bridge for robust ESP32 service sessions.

The ESP32 still drives ESP_REQ, but this build must also keep the UART service
alive when PA7 is not being sampled correctly. For the transfer prototype, file
commands are allowed whenever the AudioMoth bridge service is active and the
firmware is not busy recording, instead of waiting for the scheduler's separate
uploadAllowed flag.
"""

from __future__ import annotations

from pathlib import Path

BRIDGE_C = Path("project/src/espbridge.c")

OLD_REQUIRE = "#define BRIDGE_REQUIRE_REQ_PIN              1"
NEW_REQUIRE = "#define BRIDGE_REQUIRE_REQ_PIN              0"

OLD_SERVICE_READ = "        bool requestActive = rawRequestPinActive();"
NEW_SERVICE_READ = "        bool requestActive = ESPBridge_isRequestActive();"

OLD_ENSURE_FILESYSTEM = """static bool ensureFilesystem(void) {
    if (filesystemEnabled) return true;
    filesystemEnabled = AudioMoth_enableFileSystem(AM_SD_CARD_NORMAL_SPEED);
    return filesystemEnabled;
}
"""

NEW_ENSURE_FILESYSTEM = """static bool ensureFilesystem(void) {
    if (filesystemEnabled) return true;
    filesystemEnabled = AudioMoth_enableFileSystem(AM_SD_CARD_NORMAL_SPEED);
    return filesystemEnabled;
}

static bool fileCommandsAllowed(void) {
    return !bridgeBusy && serviceActive;
}
"""

OLD_FILE_GATE = "    if (bridgeBusy || !uploadAllowed) {"
NEW_FILE_GATE = "    if (!fileCommandsAllowed()) {"

OLD_STATUS_ALLOWED = "             uploadAllowed ? 1 : 0,"
NEW_STATUS_ALLOWED = "             fileCommandsAllowed() ? 1 : 0,"


def replace_once(text: str, old: str, new: str, label: str) -> tuple[str, bool]:
    if new in text:
        return text, False
    if old not in text:
        raise SystemExit(f"Could not find ESP bridge {label} text to patch")
    return text.replace(old, new, 1), True


def replace_all(text: str, old: str, new: str, label: str, minimum: int) -> tuple[str, int]:
    count = text.count(old)
    if count == 0 and new in text:
        return text, 0
    if count < minimum:
        raise SystemExit(f"Could not find enough ESP bridge {label} text to patch: found {count}, expected at least {minimum}")
    return text.replace(old, new), count


def main() -> None:
    text = BRIDGE_C.read_text(encoding="utf-8")
    text, require_changed = replace_once(text, OLD_REQUIRE, NEW_REQUIRE, "request-gate define")
    text, service_changed = replace_once(text, OLD_SERVICE_READ, NEW_SERVICE_READ, "service request check")
    text, helper_changed = replace_once(text, OLD_ENSURE_FILESYSTEM, NEW_ENSURE_FILESYSTEM, "idle file-command helper")
    text, gate_count = replace_all(text, OLD_FILE_GATE, NEW_FILE_GATE, "file-command gate", 3)
    text, status_changed = replace_once(text, OLD_STATUS_ALLOWED, NEW_STATUS_ALLOWED, "STATUS allowed flag")
    BRIDGE_C.write_text(text, encoding="utf-8")

    if require_changed:
        print("Applied ESP optional request-gate patch")
    else:
        print("ESP optional request-gate patch already applied")

    if service_changed:
        print("Applied ESP optional service-loop patch")
    else:
        print("ESP optional service-loop patch already applied")

    if helper_changed:
        print("Applied ESP idle file-command helper patch")
    else:
        print("ESP idle file-command helper patch already applied")

    if gate_count:
        print(f"Applied ESP idle file-command gate patch to {gate_count} command handlers")
    else:
        print("ESP idle file-command gate patch already applied")

    if status_changed:
        print("Applied ESP STATUS allowed flag patch")
    else:
        print("ESP STATUS allowed flag patch already applied")


if __name__ == "__main__":
    main()

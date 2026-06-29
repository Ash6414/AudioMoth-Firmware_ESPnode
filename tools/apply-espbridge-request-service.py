#!/usr/bin/env python3
"""Patch AudioMoth main.c so ESP_REQ always gets a UART command window.

The canonical Basic firmware scheduler only reaches the upload-service branch
when a recording-safe window is already open. For the ESP bridge, the request
pin itself must be enough to get a command service window; LIST/GET/DELETE
remain gated by uploadAllowed.
"""

from __future__ import annotations

from pathlib import Path

MAIN_C = Path("project/src/main.c")

STARTUP_ANCHOR = r'''    AM_switchPosition_t switchPosition = AudioMoth_getSwitchPosition();
'''

STARTUP_BLOCK = r'''
    /* ESP32 startup request service.
     *
     * If the ESP32 is already asserting ESP_REQ when bridge firmware starts,
     * answer UART immediately before normal configuration/scheduler handling.
     * File upload remains disabled in this early window. */
    if ((switchPosition == AM_SWITCH_CUSTOM || switchPosition == AM_SWITCH_DEFAULT) && ESPBridge_isRequestActive()) {

        ESPBridge_setBusy(false);
        ESPBridge_setUploadAllowed(false);
        ESPBridge_serviceUntil(UINT32_MAX - 1);
        ESPBridge_setUploadAllowed(false);
        ESPBridge_setBusy(true);

    }
'''

OLD_BLOCK = r'''        /* ESP32 upload service.
         *
         * No SD MUX is used: AudioMoth remains the only SD-card master.
         * The ESP32 asserts ESP_REQ, waits for MOTH_BUSY to drop, then talks UART.
         * Service is refused inside the guard window before preparation/recording. */
        if (switchPosition == AM_SWITCH_CUSTOM &&
            getBackupFlag(BACKUP_WAITING_FOR_MAGNETIC_SWITCH) == false &&
            timeUntilPreparationStart > (int64_t)ESPBRIDGE_UPLOAD_GUARD_SECONDS * MILLISECONDS_IN_SECOND) {

            uint32_t bridgeDeadline = *timeOfNextRecording - ESPBRIDGE_UPLOAD_GUARD_SECONDS;

            ESPBridge_setBusy(false);
            ESPBridge_setUploadAllowed(true);

            if (ESPBridge_isRequestActive()) {
                ESPBridge_serviceUntil(bridgeDeadline);
            }

            ESPBridge_setUploadAllowed(false);
            ESPBridge_setBusy(true);

        }
'''

NEW_BLOCK = r'''        /* ESP32 request service.
         *
         * No SD MUX is used: AudioMoth remains the only SD-card master.
         * Any asserted ESP_REQ gets a UART command window, so the ESP32 can
         * PING, STATUS, TIME, or DONE even when file upload is not safe.
         * LIST, GET, and DELETE remain gated by uploadAllowed. */
        if ((switchPosition == AM_SWITCH_CUSTOM || switchPosition == AM_SWITCH_DEFAULT) &&
            getBackupFlag(BACKUP_WAITING_FOR_MAGNETIC_SWITCH) == false &&
            ESPBridge_isRequestActive()) {

            bool noScheduledRecording = *timeOfNextRecording == UINT32_MAX;
            bool uploadServiceAllowed = noScheduledRecording ||
                timeUntilPreparationStart > (int64_t)ESPBRIDGE_UPLOAD_GUARD_SECONDS * MILLISECONDS_IN_SECOND;
            uint32_t bridgeDeadline = noScheduledRecording
                ? UINT32_MAX - 1
                : uploadServiceAllowed
                    ? *timeOfNextRecording - ESPBRIDGE_UPLOAD_GUARD_SECONDS
                    : currentTime + ESP_TIME_SERVICE_WINDOW_SECONDS;

            ESPBridge_setBusy(false);
            ESPBridge_setUploadAllowed(uploadServiceAllowed);
            ESPBridge_serviceUntil(bridgeDeadline);
            ESPBridge_setUploadAllowed(false);
            ESPBridge_setBusy(true);

        }
'''


def apply_startup_patch(text: str) -> tuple[str, bool]:
    if STARTUP_BLOCK in text:
        return text, False
    if STARTUP_ANCHOR not in text:
        raise SystemExit("Could not find switchPosition anchor for startup ESP service")
    return text.replace(STARTUP_ANCHOR, STARTUP_ANCHOR + STARTUP_BLOCK, 1), True


def apply_scheduler_patch(text: str) -> tuple[str, bool]:
    if NEW_BLOCK in text:
        return text, False
    if OLD_BLOCK not in text:
        raise SystemExit("Could not find ESP32 upload service block to patch")
    return text.replace(OLD_BLOCK, NEW_BLOCK), True


def main() -> None:
    text = MAIN_C.read_text(encoding="utf-8")
    text, startup_changed = apply_startup_patch(text)
    text, scheduler_changed = apply_scheduler_patch(text)
    MAIN_C.write_text(text, encoding="utf-8")

    if startup_changed:
        print("Applied ESP startup request service patch")
    else:
        print("ESP startup request service patch already applied")

    if scheduler_changed:
        print("Applied ESP scheduler request service patch")
    else:
        print("ESP scheduler request service patch already applied")


if __name__ == "__main__":
    main()

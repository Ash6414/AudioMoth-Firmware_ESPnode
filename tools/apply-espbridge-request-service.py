#!/usr/bin/env python3
"""Patch AudioMoth main.c so ESP_REQ always gets a UART command window.

The canonical Basic firmware scheduler only reaches the upload-service branch
when a recording-safe window is already open. For the ESP bridge, the request
pin itself must be enough to get a short command service window; LIST/GET/DELETE
remain gated by uploadAllowed.
"""

from __future__ import annotations

from pathlib import Path

MAIN_C = Path("project/src/main.c")

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
        if (switchPosition == AM_SWITCH_CUSTOM &&
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


def main() -> None:
    text = MAIN_C.read_text(encoding="utf-8")
    if NEW_BLOCK in text:
        print("ESP request service patch already applied")
        return
    if OLD_BLOCK not in text:
        raise SystemExit("Could not find ESP32 upload service block to patch")
    MAIN_C.write_text(text.replace(OLD_BLOCK, NEW_BLOCK), encoding="utf-8")
    print("Applied ESP request service patch")


if __name__ == "__main__":
    main()

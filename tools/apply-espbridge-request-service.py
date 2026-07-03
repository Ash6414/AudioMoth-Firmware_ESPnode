#!/usr/bin/env python3
"""Patch AudioMoth main.c so the ESP32 gets a guarded UART command window.

The canonical Basic firmware scheduler only reaches the upload-service branch
when a recording-safe window is already open. For the ESP bridge, the request
pin itself must be enough to get a command service window; LIST/GET/DELETE
remain gated by uploadAllowed.

The patch also keeps schedule-less or newly flashed nodes recoverable:
AudioMoth opens a guarded upload-safe service immediately in CUSTOM/DEFAULT
instead of requiring a fragile handoff into a later scheduler window. The
bridge service itself idles out when no ESP_REQ or UART traffic is present.
"""

from __future__ import annotations

from pathlib import Path

MAIN_C = Path("project/src/main.c")

STARTUP_ANCHOR = r'''    AM_switchPosition_t switchPosition = AudioMoth_getSwitchPosition();
'''

STARTUP_BLOCK = r'''
    /* ESP32 startup upload service.
     *
     * Open a guarded upload-capable UART window immediately whenever firmware starts
     * in CUSTOM or DEFAULT. The bridge itself idles out if no ESP request or UART
     * traffic is present, which removes the reset-order race without leaving an
     * unclaimed AudioMoth awake forever. */
    if (switchPosition == AM_SWITCH_CUSTOM || switchPosition == AM_SWITCH_DEFAULT) {

        ESPBridge_setBusy(false);
        ESPBridge_setUploadAllowed(true);
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

NEW_TIME_GATE_EXPR = "(AudioMoth_hasTimeBeenSet() == false && ESPBridge_hasAcceptedTime() == false)"
OLD_TIME_GATE_EXPR = "AudioMoth_hasTimeBeenSet() == false"

OLD_NOT_READY_BLOCK = r'''    /* If not ready to make a recording then flash LED and power down */

    if (getBackupFlag(BACKUP_READY_TO_MAKE_RECORDING) == false) {

        FLASH_LED(Both, SHORT_LED_FLASH_DURATION)

        SAVE_SWITCH_POSITION_AND_POWER_DOWN(DEFAULT_WAIT_INTERVAL);

    }
'''

NEW_NOT_READY_BLOCK = r'''    /* If not ready to make a recording, still allow the ESP32 to pull files.
     * This covers newly flashed or schedule-less nodes that have recordings on
     * SD but are not configured for another recording yet. */

    if (getBackupFlag(BACKUP_READY_TO_MAKE_RECORDING) == false) {

        if ((switchPosition == AM_SWITCH_CUSTOM || switchPosition == AM_SWITCH_DEFAULT) &&
            ESPBridge_isHardwareRequestActive()) {

            ESPBridge_setBusy(false);
            ESPBridge_setUploadAllowed(true);
            ESPBridge_serviceUntil(UINT32_MAX - 1);
            ESPBridge_setUploadAllowed(false);
            ESPBridge_setBusy(true);

        }

        FLASH_LED(Both, SHORT_LED_FLASH_DURATION)

        SAVE_SWITCH_POSITION_AND_POWER_DOWN(DEFAULT_WAIT_INTERVAL);

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


def apply_time_gate_patch(text: str) -> tuple[str, int]:
    if NEW_TIME_GATE_EXPR in text:
        return text, 0
    count = text.count(OLD_TIME_GATE_EXPR)
    if count < 4:
        raise SystemExit(f"Could not find ESP time gate expressions to patch: found {count}")
    return text.replace(OLD_TIME_GATE_EXPR, NEW_TIME_GATE_EXPR, 4), 4


def apply_not_ready_upload_patch(text: str) -> tuple[str, bool]:
    if NEW_NOT_READY_BLOCK in text:
        return text, False
    if OLD_NOT_READY_BLOCK not in text:
        raise SystemExit("Could not find not-ready blink/sleep block to patch")
    return text.replace(OLD_NOT_READY_BLOCK, NEW_NOT_READY_BLOCK, 1), True


def main() -> None:
    text = MAIN_C.read_text(encoding="utf-8")
    text, startup_changed = apply_startup_patch(text)
    text, scheduler_changed = apply_scheduler_patch(text)
    text, time_gate_count = apply_time_gate_patch(text)
    text, not_ready_changed = apply_not_ready_upload_patch(text)
    MAIN_C.write_text(text, encoding="utf-8")

    if startup_changed:
        print("Applied ESP guarded startup service patch")
    else:
        print("ESP guarded startup service patch already applied")

    if scheduler_changed:
        print("Applied ESP scheduler request service patch")
    else:
        print("ESP scheduler request service patch already applied")

    if time_gate_count:
        print(f"Applied ESP accepted-time gate patch to {time_gate_count} scheduler checks")
    else:
        print("ESP accepted-time gate patch already applied")

    if not_ready_changed:
        print("Applied ESP not-ready upload service patch")
    else:
        print("ESP not-ready upload service patch already applied")


if __name__ == "__main__":
    main()

/****************************************************************************
 * espbridge.h
 * AudioMoth Dev <-> ESP32 bridge for SD-owned-by-AudioMoth uploads
 * Drop-in bridge for AudioMoth-Firmware-Basic / AudioMoth-Project.
 *
 * AudioMoth owns the microSD at all times. The ESP32 requests file chunks
 * over UART only while AudioMoth is outside its recording/preparation window.
 *****************************************************************************/

#ifndef __ESPBRIDGE_H
#define __ESPBRIDGE_H

#include <stdint.h>
#include <stdbool.h>

#define ESPBRIDGE_DEFAULT_BAUD              115200
#define ESPBRIDGE_MAX_LINE                  160
#define ESPBRIDGE_MAX_PATH                  96
#define ESPBRIDGE_CHUNK_BYTES               4096
#define ESPBRIDGE_UPLOAD_GUARD_SECONDS      300

void ESPBridge_init(void);

/* High means AudioMoth is recording/preparing/doing protected SD work. */
void ESPBridge_setBusy(bool busy);

/* True only when the main scheduler has enough time before the next recording. */
void ESPBridge_setUploadAllowed(bool allowed);

/* Reads the ESP request pin. */
bool ESPBridge_isRequestActive(void);

/* Services UART commands until deadlineUnixSeconds, request pin release, or idle timeout. */
void ESPBridge_serviceUntil(uint32_t deadlineUnixSeconds);

#endif /* __ESPBRIDGE_H */

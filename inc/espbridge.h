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
#define ESPBRIDGE_PROTOCOL_VERSION          4
#define ESPBRIDGE_CONTROL_BAUD_STREAM       1
#define ESPBRIDGE_PIPE_STREAM               1
#define ESPBRIDGE_PIPE_BAUD                 230400
#define ESPBRIDGE_PIPE_BLOCK_BYTES          131072
#define ESPBRIDGE_PIPE_FRAME_BYTES          2048
#define ESPBRIDGE_PIPE_NEXT_TIMEOUT_MS      120000
#define ESPBRIDGE_PIPE_ACK_TIMEOUT_MS       750
#define ESPBRIDGE_PIPE_FRAME_RETRIES        3
#define ESPBRIDGE_FAST_BAUD                 921600
#define ESPBRIDGE_TRAINING_BYTES            1024
#define ESPBRIDGE_FAST_PAYLOAD_TRAINING_BYTES 1024
#define ESPBRIDGE_SLOW_PAYLOAD_TRAINING_BYTES 128
#define ESPBRIDGE_QUICK_BAUD_THRESHOLD      921600
#define ESPBRIDGE_FAST_SWITCH_GUARD_MS      20
#define ESPBRIDGE_MAX_LINE                  160
#define ESPBRIDGE_MAX_PATH                  96
#define ESPBRIDGE_RX_BUFFER_BYTES           256
#define ESPBRIDGE_CHUNK_BYTES               2048
#define ESPBRIDGE_STREAM_BYTES              65536
#define ESPBRIDGE_TEST_STREAM_BYTES         1048576
#define ESPBRIDGE_UPLOAD_GUARD_SECONDS      300

void ESPBridge_init(void);

/* Called by the existing UART1 RX interrupt callback in gps.c. */
void ESPBridge_handleReceivedByte(uint8_t byte);

/* High means AudioMoth is recording/preparing/doing protected SD work. */
void ESPBridge_setBusy(bool busy);

/* True only when the main scheduler has enough time before the next recording. */
void ESPBridge_setUploadAllowed(bool allowed);

/* Reads the ESP request pin. */
bool ESPBridge_isRequestActive(void);

/* Services UART commands until deadlineUnixSeconds, request pin release, or idle timeout. */
void ESPBridge_serviceUntil(uint32_t deadlineUnixSeconds);

#endif /* __ESPBRIDGE_H */

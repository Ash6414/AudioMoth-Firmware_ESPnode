/****************************************************************************
 * espbridge.c
 * AudioMoth Dev <-> ESP32 upload bridge.
 *
 * Protocol is ASCII commands plus binary DATA payloads.
 * Commands from ESP32:
 *   STATUS\n
 *   TIME <unix_seconds> <milliseconds>\n
 *   LIST\n
 *   GET <path> <offset> <max_bytes>\n
 *   DELETE <path>\n            // ESP32 should send only after server-confirmed upload
 *   DONE\n             // ESP32 releases service window
 *   PING\n
 *
 * Responses from AudioMoth:
 *   OK ...\n
 *   ERR <code> <detail>\n
 *   FILE <path> <size_bytes>\n
 *   END\n
 *   DATA <path> <offset> <n_bytes> <crc32_hex>\n<raw bytes>
 *
 * Notes:
 *   - This file assumes the standard AudioMoth-Project build exposes EMLIB,
 *     FatFS, and audiomoth.h.
 *   - UART route uses USART1 location 2 because AudioMoth Dev labels b9/b10
 *     as U1_TX#2 / U1_RX#2.
 *****************************************************************************/

#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <stdio.h>
#include <stdarg.h>
#include <ctype.h>

#include "em_cmu.h"
#include "em_gpio.h"
#include "em_usart.h"
#include "em_wdog.h"

#include "ff.h"
#include "audiomoth.h"
#include "espbridge.h"

/* AudioMoth Dev left JST header */
#define BRIDGE_UART                         USART1
#define BRIDGE_UART_CLOCK                   cmuClock_USART1
#define BRIDGE_UART_LOCATION                USART_ROUTE_LOCATION_LOC2

#define BRIDGE_TX_PORT                      gpioPortB    /* b9, AudioMoth TX -> ESP RX */
#define BRIDGE_TX_PIN                       9
#define BRIDGE_RX_PORT                      gpioPortB    /* b10, AudioMoth RX <- ESP TX */
#define BRIDGE_RX_PIN                       10

#define BRIDGE_BUSY_PORT                    gpioPortA    /* a8, AudioMoth output */
#define BRIDGE_BUSY_PIN                     8
#define BRIDGE_REQ_PORT                     gpioPortA    /* a7, ESP output to AudioMoth */
#define BRIDGE_REQ_PIN                      7

#define RX_LINE_TIMEOUT_MS                  250
#define SERVICE_IDLE_TIMEOUT_MS             3000

static volatile bool bridgeBusy = true;
static volatile bool uploadAllowed = false;
static bool filesystemEnabled = false;

static char lineBuffer[ESPBRIDGE_MAX_LINE];
static uint8_t chunkBuffer[ESPBRIDGE_CHUNK_BYTES];

/* ---------------- UART primitives ---------------- */

static inline void gpioWrite(GPIO_Port_TypeDef port, unsigned int pin, bool value) {
    if (value) {
        GPIO_PinOutSet(port, pin);
    } else {
        GPIO_PinOutClear(port, pin);
    }
}

static inline bool uartRxAvailable(void) {
    return (USART_StatusGet(BRIDGE_UART) & USART_STATUS_RXDATAV) != 0;
}

static uint8_t uartReadByte(void) {
    return (uint8_t)USART_Rx(BRIDGE_UART);
}

static void uartWriteByte(uint8_t byte) {
    USART_Tx(BRIDGE_UART, byte);
}

static void uartWrite(const void *data, uint32_t length) {
    const uint8_t *p = (const uint8_t*)data;
    for (uint32_t i = 0; i < length; i += 1) uartWriteByte(p[i]);
}

static void sendLine(const char *fmt, ...) {
    char out[192];
    va_list args;
    va_start(args, fmt);
    int n = vsnprintf(out, sizeof(out), fmt, args);
    va_end(args);
    if (n < 0) return;
    if ((uint32_t)n >= sizeof(out)) n = sizeof(out) - 1;
    uartWrite(out, (uint32_t)n);
    uartWrite("\n", 1);
}

/* Returns true when a complete line was read. CR is ignored. */
static bool readLine(uint32_t timeoutMs) {
    uint32_t index = 0;
    uint32_t elapsed = 0;

    while (elapsed < timeoutMs && index < ESPBRIDGE_MAX_LINE - 1) {
        WDOG_Feed();

        if (uartRxAvailable()) {
            char c = (char)uartReadByte();
            if (c == '\r') continue;
            if (c == '\n') {
                lineBuffer[index] = 0;
                return index > 0;
            }
            lineBuffer[index++] = c;
        } else {
            AudioMoth_delay(1);
            elapsed += 1;
        }
    }

    lineBuffer[index] = 0;
    return false;
}

/* ---------------- CRC and validation ---------------- */

static uint32_t crc32Update(uint32_t crc, const uint8_t *data, uint32_t length) {
    crc = ~crc;
    for (uint32_t i = 0; i < length; i += 1) {
        crc ^= data[i];
        for (uint32_t j = 0; j < 8; j += 1) {
            uint32_t mask = -(crc & 1U);
            crc = (crc >> 1) ^ (0xEDB88320UL & mask);
        }
    }
    return ~crc;
}

static bool endsWithWav(const char *path) {
    const char *dot = strrchr(path, '.');
    if (dot == NULL) return false;
    return (dot[1] == 'W' || dot[1] == 'w') &&
           (dot[2] == 'A' || dot[2] == 'a') &&
           (dot[3] == 'V' || dot[3] == 'v') &&
           dot[4] == 0;
}

static bool validPath(const char *path, bool requireWav) {
    uint32_t len = strlen(path);
    if (len == 0 || len >= ESPBRIDGE_MAX_PATH) return false;
    if (path[0] == '/' || path[0] == '\\') return false;
    if (strstr(path, "..") != NULL) return false;

    for (uint32_t i = 0; i < len; i += 1) {
        char c = path[i];
        bool ok = isalnum((unsigned char)c) || c == '_' || c == '-' || c == '.' || c == '/';
        if (!ok) return false;
    }

    if (requireWav && !endsWithWav(path)) return false;
    return true;
}

static bool ensureFilesystem(void) {
    if (filesystemEnabled) return true;
    filesystemEnabled = AudioMoth_enableFileSystem(AM_SD_CARD_NORMAL_SPEED);
    return filesystemEnabled;
}

static bool deadlineReached(uint32_t deadlineUnixSeconds) {
    uint32_t now, ms;
    AudioMoth_getTime(&now, &ms);
    return now >= deadlineUnixSeconds;
}

/* ---------------- File operations ---------------- */

static void listOneDirectory(const char *prefix) {
    DIR dir;
    FILINFO fno;

    FRESULT res = f_opendir(&dir, prefix[0] ? prefix : "");
    if (res != FR_OK) return;

    while (true) {
        WDOG_Feed();
        res = f_readdir(&dir, &fno);
        if (res != FR_OK || fno.fname[0] == 0) break;

        if (fno.fattrib & AM_DIR) {
            /* Support one level of AudioMoth daily folders, e.g. 20260531/file.WAV. */
            char nested[ESPBRIDGE_MAX_PATH];
            if (prefix[0]) continue;
            snprintf(nested, sizeof(nested), "%s", fno.fname);

            DIR subdir;
            FILINFO subfno;
            if (f_opendir(&subdir, nested) == FR_OK) {
                while (true) {
                    FRESULT subres = f_readdir(&subdir, &subfno);
                    if (subres != FR_OK || subfno.fname[0] == 0) break;
                    if ((subfno.fattrib & AM_DIR) == 0 && endsWithWav(subfno.fname)) {
                        char full[ESPBRIDGE_MAX_PATH];
                        snprintf(full, sizeof(full), "%s/%s", nested, subfno.fname);
                        sendLine("FILE %s %lu", full, (unsigned long)subfno.fsize);
                    }
                }
                f_closedir(&subdir);
            }
        } else if (endsWithWav(fno.fname)) {
            sendLine("FILE %s %lu", fno.fname, (unsigned long)fno.fsize);
        }
    }

    f_closedir(&dir);
}

static void commandList(void) {
    if (bridgeBusy || !uploadAllowed) {
        sendLine("ERR BUSY upload_not_allowed");
        return;
    }
    if (!ensureFilesystem()) {
        sendLine("ERR SD filesystem_enable_failed");
        return;
    }

    listOneDirectory("");
    sendLine("END");
}

static void commandGet(char *args) {
    char path[ESPBRIDGE_MAX_PATH];
    unsigned long offset = 0;
    unsigned long requested = ESPBRIDGE_CHUNK_BYTES;

    if (bridgeBusy || !uploadAllowed) {
        sendLine("ERR BUSY upload_not_allowed");
        return;
    }
    if (sscanf(args, "%95s %lu %lu", path, &offset, &requested) < 2) {
        sendLine("ERR ARG usage_GET_path_offset_maxbytes");
        return;
    }
    if (!validPath(path, true)) {
        sendLine("ERR PATH invalid_path");
        return;
    }
    if (requested == 0 || requested > ESPBRIDGE_CHUNK_BYTES) requested = ESPBRIDGE_CHUNK_BYTES;
    if (!ensureFilesystem()) {
        sendLine("ERR SD filesystem_enable_failed");
        return;
    }

    FIL file;
    FRESULT res = f_open(&file, path, FA_READ);
    if (res != FR_OK) {
        sendLine("ERR OPEN %u", (unsigned int)res);
        return;
    }

    FSIZE_t size = f_size(&file);
    if ((FSIZE_t)offset > size) {
        f_close(&file);
        sendLine("ERR RANGE offset_past_eof");
        return;
    }

    res = f_lseek(&file, (FSIZE_t)offset);
    if (res != FR_OK) {
        f_close(&file);
        sendLine("ERR SEEK %u", (unsigned int)res);
        return;
    }

    UINT bytesRead = 0;
    res = f_read(&file, chunkBuffer, (UINT)requested, &bytesRead);
    f_close(&file);

    if (res != FR_OK) {
        sendLine("ERR READ %u", (unsigned int)res);
        return;
    }

    uint32_t crc = crc32Update(0, chunkBuffer, bytesRead);
    sendLine("DATA %s %lu %u %08lX", path, offset, (unsigned int)bytesRead, (unsigned long)crc);
    uartWrite(chunkBuffer, bytesRead);
}

static void commandDelete(char *args) {
    char path[ESPBRIDGE_MAX_PATH];

    if (bridgeBusy || !uploadAllowed) {
        sendLine("ERR BUSY upload_not_allowed");
        return;
    }

    if (sscanf(args, "%95s", path) != 1) {
        sendLine("ERR ARG usage_DELETE_path");
        return;
    }
    if (!validPath(path, true)) {
        sendLine("ERR PATH invalid_path");
        return;
    }
    if (!ensureFilesystem()) {
        sendLine("ERR SD filesystem_enable_failed");
        return;
    }

    FRESULT res = f_unlink(path);
    if (res == FR_OK) sendLine("OK DELETE %s", path);
    else sendLine("ERR DELETE %u", (unsigned int)res);
}

static void commandTime(char *args) {
    unsigned long seconds = 0;
    unsigned long milliseconds = 0;
    if (sscanf(args, "%lu %lu", &seconds, &milliseconds) < 1) {
        sendLine("ERR ARG usage_TIME_unix_seconds_milliseconds");
        return;
    }
    if (milliseconds > 999) milliseconds = 999;
    AudioMoth_setTime((uint32_t)seconds, (uint32_t)milliseconds);
    sendLine("OK TIME %lu %lu", seconds, milliseconds);
}

static void commandStatus(uint32_t deadlineUnixSeconds) {
    uint32_t now, ms;
    AudioMoth_getTime(&now, &ms);
    sendLine("OK STATUS busy=%u allowed=%u req=%u now=%lu ms=%lu deadline=%lu",
             bridgeBusy ? 1 : 0,
             uploadAllowed ? 1 : 0,
             ESPBridge_isRequestActive() ? 1 : 0,
             (unsigned long)now,
             (unsigned long)ms,
             (unsigned long)deadlineUnixSeconds);
}

static void handleCommand(uint32_t deadlineUnixSeconds) {
    if (strcmp(lineBuffer, "PING") == 0) {
        sendLine("OK PONG");
    } else if (strcmp(lineBuffer, "STATUS") == 0) {
        commandStatus(deadlineUnixSeconds);
    } else if (strncmp(lineBuffer, "TIME ", 5) == 0) {
        commandTime(lineBuffer + 5);
    } else if (strcmp(lineBuffer, "LIST") == 0) {
        commandList();
    } else if (strncmp(lineBuffer, "GET ", 4) == 0) {
        commandGet(lineBuffer + 4);
    } else if (strncmp(lineBuffer, "DELETE ", 7) == 0) {
        commandDelete(lineBuffer + 7);
    } else if (strcmp(lineBuffer, "DONE") == 0) {
        sendLine("OK DONE");
    } else {
        sendLine("ERR CMD unknown_command");
    }
}

/* ---------------- Public API ---------------- */

void ESPBridge_init(void) {
    CMU_ClockEnable(cmuClock_GPIO, true);
    CMU_ClockEnable(BRIDGE_UART_CLOCK, true);

    GPIO_PinModeSet(BRIDGE_TX_PORT, BRIDGE_TX_PIN, gpioModePushPull, 1);
    GPIO_PinModeSet(BRIDGE_RX_PORT, BRIDGE_RX_PIN, gpioModeInput, 0);
    GPIO_PinModeSet(BRIDGE_BUSY_PORT, BRIDGE_BUSY_PIN, gpioModePushPull, 1);
    GPIO_PinModeSet(BRIDGE_REQ_PORT, BRIDGE_REQ_PIN, gpioModeInputPull, 0);

    USART_InitAsync_TypeDef init = USART_INITASYNC_DEFAULT;
    init.baudrate = ESPBRIDGE_DEFAULT_BAUD;
    init.oversampling = usartOVS4;
    USART_InitAsync(BRIDGE_UART, &init);

    BRIDGE_UART->ROUTE = USART_ROUTE_TXPEN |
                         USART_ROUTE_RXPEN |
                         BRIDGE_UART_LOCATION;

    bridgeBusy = true;
    uploadAllowed = false;
    filesystemEnabled = false;
}

void ESPBridge_setBusy(bool busy) {
    bridgeBusy = busy;
    gpioWrite(BRIDGE_BUSY_PORT, BRIDGE_BUSY_PIN, busy);
}

void ESPBridge_setUploadAllowed(bool allowed) {
    uploadAllowed = allowed;
}

bool ESPBridge_isRequestActive(void) {
    return GPIO_PinInGet(BRIDGE_REQ_PORT, BRIDGE_REQ_PIN) != 0;
}

void ESPBridge_serviceUntil(uint32_t deadlineUnixSeconds) {
    if (bridgeBusy) return;
    if (deadlineReached(deadlineUnixSeconds)) return;

    uint32_t idleMs = 0;
    sendLine("OK BRIDGE_READY");

    while (!deadlineReached(deadlineUnixSeconds)) {
        WDOG_Feed();

        if (!ESPBridge_isRequestActive() && !uartRxAvailable()) {
            if (idleMs >= SERVICE_IDLE_TIMEOUT_MS) break;
            AudioMoth_delay(10);
            idleMs += 10;
            continue;
        }

        if (readLine(RX_LINE_TIMEOUT_MS)) {
            idleMs = 0;
            handleCommand(deadlineUnixSeconds);
        } else {
            idleMs += RX_LINE_TIMEOUT_MS;
        }
    }

    sendLine("OK BRIDGE_SLEEP");
}

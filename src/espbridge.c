/****************************************************************************
 * espbridge.c
 * AudioMoth Dev <-> ESP32 upload bridge.
 *
 * Protocol is ASCII commands plus binary DATA payloads. AudioMoth owns the
 * microSD at all times; the ESP32 requests files over UART only while the
 * scheduler has opened a safe bridge window.
 *****************************************************************************/

#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <stdio.h>
#include <stdarg.h>
#include <ctype.h>

#include "em_cmu.h"
#include "em_gpio.h"
#include "em_timer.h"
#include "em_usart.h"
#include "em_wdog.h"

#include "ff.h"
#include "audiomoth.h"
#include "espbridge.h"

/* AudioMoth Dev left JST header. UART1 LOC2 is PB9 TX and PB10 RX. */
#define BRIDGE_UART                         UART1
#define BRIDGE_UART_CLOCK                   cmuClock_UART1
#define BRIDGE_UART_LOCATION                UART_ROUTE_LOCATION_LOC2

#define BRIDGE_TX_PORT                      gpioPortB    /* b9, AudioMoth TX -> ESP RX */
#define BRIDGE_TX_PIN                       9
#define BRIDGE_RX_PORT                      gpioPortB    /* b10, AudioMoth RX <- ESP TX */
#define BRIDGE_RX_PIN                       10

#define BRIDGE_BUSY_PORT                    gpioPortA    /* a8, AudioMoth output */
#define BRIDGE_BUSY_PIN                     8
#define BRIDGE_REQ_PORT                     gpioPortA    /* a7, ESP output to AudioMoth */
#define BRIDGE_REQ_PIN                      7

/* Prototype mode: do not let a PA7 read issue prevent UART service from running. */
#define BRIDGE_REQUIRE_REQ_PIN              0

#define RX_LINE_TIMEOUT_MS                  250
#define SERVICE_IDLE_TIMEOUT_MS             3000
#define SERVICE_MAX_WINDOW_MS               30000
#define SERVICE_DEBUG_PULSE_MS              150
#define MILLISECONDS_PER_SECOND             1000
#define MICROSECONDS_PER_SECOND             1000000

static volatile bool bridgeBusy = true;
static volatile bool uploadAllowed = false;
static bool filesystemEnabled = false;
static bool serviceActive = false;
static uint32_t softUartTicksPerBit = 0;
static uint32_t softUartTicksPerMillisecond = 0;

static char lineBuffer[ESPBRIDGE_MAX_LINE];
static uint8_t chunkBuffer[ESPBRIDGE_CHUNK_BYTES];

/* ---------------- UART primitives ---------------- */

static void configureBridgePins(void) {
    CMU_ClockEnable(cmuClock_GPIO, true);
    CMU_ClockEnable(BRIDGE_UART_CLOCK, true);

    USART_Reset(BRIDGE_UART);
    CMU_ClockEnable(BRIDGE_UART_CLOCK, false);

    GPIO_PinModeSet(BRIDGE_TX_PORT, BRIDGE_TX_PIN, gpioModePushPull, 1);
    GPIO_PinModeSet(BRIDGE_RX_PORT, BRIDGE_RX_PIN, gpioModeInputPull, 1);
}

static void startSoftUartTimer(void) {
    CMU_ClockEnable(cmuClock_TIMER1, true);
    TIMER_Reset(TIMER1);

    TIMER_Init_TypeDef timerInit = TIMER_INIT_DEFAULT;
    timerInit.enable = false;
    timerInit.prescale = timerPrescale1;

    TIMER_Init(TIMER1, &timerInit);
    TIMER_TopSet(TIMER1, UINT16_MAX);
    TIMER_CounterSet(TIMER1, 0);
    TIMER_Enable(TIMER1, true);

    uint32_t timerFrequency = CMU_ClockFreqGet(cmuClock_TIMER1);
    softUartTicksPerBit = (timerFrequency + ESPBRIDGE_DEFAULT_BAUD / 2) / ESPBRIDGE_DEFAULT_BAUD;
    softUartTicksPerMillisecond = (timerFrequency + 500) / 1000;
}

static void stopSoftUartTimer(void) {
    TIMER_Enable(TIMER1, false);
    TIMER_Reset(TIMER1);
    CMU_ClockEnable(cmuClock_TIMER1, false);
    softUartTicksPerBit = 0;
    softUartTicksPerMillisecond = 0;
}

static void configureBridgeUart(void) {
    configureBridgePins();
    startSoftUartTimer();
}

static void softUartDelayTicks(uint32_t ticks) {
    uint16_t start = (uint16_t)TIMER_CounterGet(TIMER1);
    while ((uint16_t)(TIMER_CounterGet(TIMER1) - start) < ticks) {
    }
}

static void bridgeDelayMilliseconds(uint32_t milliseconds) {
    for (uint32_t i = 0; i < milliseconds; i += 1) {
        softUartDelayTicks(softUartTicksPerMillisecond);
        WDOG_Feed();
    }
}

static inline void gpioWrite(GPIO_Port_TypeDef port, unsigned int pin, bool value) {
    if (value) {
        GPIO_PinOutSet(port, pin);
    } else {
        GPIO_PinOutClear(port, pin);
    }
}

static void pulsePin(GPIO_Port_TypeDef port, unsigned int pin, bool idleHigh, uint32_t count) {
    GPIO_PinModeSet(port, pin, gpioModePushPull, idleHigh ? 1 : 0);

    for (uint32_t i = 0; i < count; i += 1) {
        gpioWrite(port, pin, !idleHigh);
        AudioMoth_delay(SERVICE_DEBUG_PULSE_MS);
        gpioWrite(port, pin, idleHigh);
        AudioMoth_delay(SERVICE_DEBUG_PULSE_MS);
    }
}

static void pulseBusyDebug(void) {
    /* Visible on ESP GPIO26; proves ESPBridge_serviceUntil() was entered. */
    pulsePin(BRIDGE_BUSY_PORT, BRIDGE_BUSY_PIN, false, 3);
    gpioWrite(BRIDGE_BUSY_PORT, BRIDGE_BUSY_PIN, false);
}

static void pulseTxPinDebug(void) {
    /* Visible on ESP GPIO16 if b9 really reaches the ESP RX input. */
    pulsePin(BRIDGE_TX_PORT, BRIDGE_TX_PIN, true, 3);
    GPIO_PinModeSet(BRIDGE_TX_PORT, BRIDGE_TX_PIN, gpioModePushPull, 1);
}

static inline bool rawRequestPinActive(void) {
    return GPIO_PinInGet(BRIDGE_REQ_PORT, BRIDGE_REQ_PIN) != 0;
}

static inline bool uartRxAvailable(void) {
    return GPIO_PinInGet(BRIDGE_RX_PORT, BRIDGE_RX_PIN) == 0;
}

static bool uartReadByte(uint8_t *byte) {
    if (GPIO_PinInGet(BRIDGE_RX_PORT, BRIDGE_RX_PIN) != 0) return false;

    softUartDelayTicks(softUartTicksPerBit + softUartTicksPerBit / 2);

    uint8_t value = 0;
    for (uint32_t bit = 0; bit < 8; bit += 1) {
        if (GPIO_PinInGet(BRIDGE_RX_PORT, BRIDGE_RX_PIN)) value |= (1U << bit);
        softUartDelayTicks(softUartTicksPerBit);
    }

    *byte = value;
    return true;
}

static void uartWriteByte(uint8_t byte) {
    gpioWrite(BRIDGE_TX_PORT, BRIDGE_TX_PIN, false);
    softUartDelayTicks(softUartTicksPerBit);

    for (uint32_t bit = 0; bit < 8; bit += 1) {
        gpioWrite(BRIDGE_TX_PORT, BRIDGE_TX_PIN, (byte & (1U << bit)) != 0);
        softUartDelayTicks(softUartTicksPerBit);
    }

    gpioWrite(BRIDGE_TX_PORT, BRIDGE_TX_PIN, true);
    softUartDelayTicks(softUartTicksPerBit);
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
    uint32_t elapsedTicks = 0;
    uint32_t timeoutTicks = timeoutMs * softUartTicksPerMillisecond;
    uint32_t pollTicks = softUartTicksPerBit / 4;
    if (pollTicks == 0) pollTicks = 1;

    while (elapsedTicks < timeoutTicks && index < ESPBRIDGE_MAX_LINE - 1) {
        WDOG_Feed();

        uint8_t byte;
        if (uartReadByte(&byte)) {
            char c = (char)byte;
            if (c == '\r') continue;
            if (c == '\n') {
                lineBuffer[index] = 0;
                return index > 0;
            }
            lineBuffer[index++] = c;
        } else {
            softUartDelayTicks(pollTicks);
            elapsedTicks += pollTicks;
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

static uint32_t elapsedServiceMilliseconds(uint32_t startSeconds, uint32_t startMilliseconds) {
    uint32_t now, milliseconds;
    AudioMoth_getTime(&now, &milliseconds);

    if (now < startSeconds) return SERVICE_MAX_WINDOW_MS;

    uint32_t elapsedSeconds = now - startSeconds;
    int32_t elapsedMilliseconds = (int32_t)milliseconds - (int32_t)startMilliseconds;

    if (elapsedMilliseconds < 0) {
        if (elapsedSeconds == 0) return 0;
        elapsedSeconds -= 1;
        elapsedMilliseconds += MILLISECONDS_PER_SECOND;
    }

    if (elapsedSeconds >= SERVICE_MAX_WINDOW_MS / MILLISECONDS_PER_SECOND + 1) return SERVICE_MAX_WINDOW_MS;

    uint32_t elapsed = elapsedSeconds * MILLISECONDS_PER_SECOND + (uint32_t)elapsedMilliseconds;
    return elapsed > SERVICE_MAX_WINDOW_MS ? SERVICE_MAX_WINDOW_MS : elapsed;
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
    sendLine("OK STATUS busy=%u allowed=%u req=%u req_pin=%u now=%lu ms=%lu deadline=%lu",
             bridgeBusy ? 1 : 0,
             uploadAllowed ? 1 : 0,
             ESPBridge_isRequestActive() ? 1 : 0,
             rawRequestPinActive() ? 1 : 0,
             (unsigned long)now,
             (unsigned long)ms,
             (unsigned long)deadlineUnixSeconds);
}

static bool handleCommand(uint32_t deadlineUnixSeconds) {
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
        return true;
    } else {
        sendLine("ERR CMD unknown_command");
    }

    return false;
}

/* ---------------- Public API ---------------- */

void ESPBridge_init(void) {
    CMU_ClockEnable(cmuClock_GPIO, true);
    GPIO_PinModeSet(BRIDGE_BUSY_PORT, BRIDGE_BUSY_PIN, gpioModePushPull, 1);
    GPIO_PinModeSet(BRIDGE_REQ_PORT, BRIDGE_REQ_PIN, gpioModeInputPull, 0);

    configureBridgePins();

    bridgeBusy = true;
    uploadAllowed = false;
    filesystemEnabled = false;
    serviceActive = false;
}

void ESPBridge_setBusy(bool busy) {
    bridgeBusy = busy;
    gpioWrite(BRIDGE_BUSY_PORT, BRIDGE_BUSY_PIN, busy);
}

void ESPBridge_setUploadAllowed(bool allowed) {
    uploadAllowed = allowed;
}

bool ESPBridge_isRequestActive(void) {
#if BRIDGE_REQUIRE_REQ_PIN
    return rawRequestPinActive();
#else
    return true;
#endif
}

void ESPBridge_serviceUntil(uint32_t deadlineUnixSeconds) {
    if (bridgeBusy || serviceActive) return;

    serviceActive = true;

    uint32_t idleMs = 0;
    uint32_t serviceStartSeconds, serviceStartMilliseconds;
    AudioMoth_getTime(&serviceStartSeconds, &serviceStartMilliseconds);

    pulseBusyDebug();
    pulseTxPinDebug();
    configureBridgeUart();

    sendLine("OK BRIDGE_READY");

    while (elapsedServiceMilliseconds(serviceStartSeconds, serviceStartMilliseconds) < SERVICE_MAX_WINDOW_MS) {
        WDOG_Feed();

        bool deadlineDone = deadlineReached(deadlineUnixSeconds);
        bool requestActive = rawRequestPinActive();
        bool rxAvailable = uartRxAvailable();

        if (deadlineDone && !requestActive && !rxAvailable) break;

        if (!requestActive && !rxAvailable) {
            if (idleMs >= SERVICE_IDLE_TIMEOUT_MS) break;
            bridgeDelayMilliseconds(10);
            idleMs += 10;
            continue;
        }

        if (readLine(RX_LINE_TIMEOUT_MS)) {
            idleMs = 0;
            if (handleCommand(deadlineUnixSeconds)) break;
        } else {
            idleMs += RX_LINE_TIMEOUT_MS;
        }
    }

    sendLine("OK BRIDGE_SLEEP");
    stopSoftUartTimer();
    serviceActive = false;
}

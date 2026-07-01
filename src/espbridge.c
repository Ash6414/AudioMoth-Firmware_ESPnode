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

#define BRIDGE_REQUIRE_REQ_PIN              0

#define RX_LINE_TIMEOUT_MS                  250
#define SERVICE_IDLE_TIMEOUT_MS             30000
#define SERVICE_MAX_WINDOW_MS               7200000
#define SERVICE_READY_BEACON_MS             1000
#define MILLISECONDS_PER_SECOND             1000

static volatile bool bridgeBusy = true;
static volatile bool uploadAllowed = false;
static bool filesystemEnabled = false;
static bool serviceActive = false;
static uint32_t fastPayloadBaud = ESPBRIDGE_FAST_BAUD;
static uint32_t softUartTicksPerBit = 0;
static uint32_t softUartTicksPerMillisecond = 0;

static char lineBuffer[ESPBRIDGE_MAX_LINE];
static uint8_t chunkBuffer[ESPBRIDGE_CHUNK_BYTES];

static volatile uint8_t bridgeRxBuffer[ESPBRIDGE_RX_BUFFER_BYTES];
static volatile uint8_t bridgeRxHead = 0;
static volatile uint8_t bridgeRxTail = 0;
static volatile bool bridgeRxOverflow = false;

void ESPBridge_handleReceivedByte(uint8_t byte) {
    uint8_t next = (uint8_t)(bridgeRxHead + 1U);
    if (next == bridgeRxTail) {
        bridgeRxOverflow = true;
        return;
    }

    bridgeRxBuffer[bridgeRxHead] = byte;
    bridgeRxHead = next;
}

static void resetBridgeRxBuffer(void) {
    bridgeRxHead = 0;
    bridgeRxTail = 0;
    bridgeRxOverflow = false;
}

static bool bufferedRxAvailable(void) {
    return bridgeRxHead != bridgeRxTail;
}

static bool bufferedRxRead(uint8_t *byte) {
    uint8_t tail = bridgeRxTail;
    if (bridgeRxHead == tail) return false;

    *byte = bridgeRxBuffer[tail];
    bridgeRxTail = (uint8_t)(tail + 1U);
    return true;
}

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

static void uartWriteUInt16LE(uint16_t value) {
    uartWriteByte((uint8_t)(value & 0xFF));
    uartWriteByte((uint8_t)((value >> 8) & 0xFF));
}

static void uartWriteUInt32LE(uint32_t value) {
    uartWriteByte((uint8_t)(value & 0xFF));
    uartWriteByte((uint8_t)((value >> 8) & 0xFF));
    uartWriteByte((uint8_t)((value >> 16) & 0xFF));
    uartWriteByte((uint8_t)((value >> 24) & 0xFF));
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
            if (c == '\n') {
                lineBuffer[index] = 0;
                return index > 0;
            }
            if (c != '\r') lineBuffer[index++] = c;
        } else {
            softUartDelayTicks(pollTicks);
        }

        elapsedTicks += pollTicks;
    }

    lineBuffer[index] = 0;
    return false;
}

static void bridgeSetBaud(uint32_t baud) {
    uint32_t timerFrequency = CMU_ClockFreqGet(cmuClock_TIMER1);
    softUartTicksPerBit = (timerFrequency + baud / 2) / baud;
    if (softUartTicksPerBit == 0) softUartTicksPerBit = 1;
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

static void sendFastTrainingPreamble(void) {
    bridgeDelayMilliseconds(40);
    for (uint32_t i = 0; i < ESPBRIDGE_TRAINING_BYTES; i += 1) {
        uartWriteByte(0x55);
    }
    uartWrite("\n", 1);
    for (uint32_t i = 0; i < 3; i += 1) {
        sendLine("OK FAST_READY");
        bridgeDelayMilliseconds(5);
    }
}

static bool endsWithWav(const char *path) {
    const char *dot = strrchr(path, '.');
    if (dot == NULL) return false;
    return (dot[1] == 'W' || dot[1] == 'w') &&
           (dot[2] == 'A' || dot[2] == 'a') &&
           (dot[3] == 'V' || dot[3] == 'v') &&
           dot[4] == 0;
}

static bool basenameHasExtension(const char *path) {
    const char *slash = strrchr(path, '/');
    const char *name = slash == NULL ? path : slash + 1;
    return strchr(name, '.') != NULL;
}

static bool pathMayBeAudioFile(const char *path) {
    return endsWithWav(path) || !basenameHasExtension(path);
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

    if (requireWav && !pathMayBeAudioFile(path)) return false;
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

static bool fileLooksLikeWav(const char *path) {
    if (endsWithWav(path)) return true;
    if (basenameHasExtension(path)) return false;

    FIL file;
    FRESULT res = f_open(&file, path, FA_READ);
    if (res != FR_OK) return false;

    uint8_t header[12];
    UINT bytesRead = 0;
    res = f_read(&file, header, sizeof(header), &bytesRead);
    f_close(&file);

    if (res != FR_OK || bytesRead != sizeof(header)) return false;

    return header[0] == 'R' && header[1] == 'I' && header[2] == 'F' && header[3] == 'F' &&
           header[8] == 'W' && header[9] == 'A' && header[10] == 'V' && header[11] == 'E';
}

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
                    if ((subfno.fattrib & AM_DIR) == 0) {
                        char full[ESPBRIDGE_MAX_PATH];
                        snprintf(full, sizeof(full), "%s/%s", nested, subfno.fname);
                        if (fileLooksLikeWav(full)) {
                            sendLine("FILE %s %lu", full, (unsigned long)subfno.fsize);
                        }
                    }
                }
                f_closedir(&subdir);
            }
        } else if (fileLooksLikeWav(fno.fname)) {
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

static bool supportedBaud(uint32_t baud) {
    return baud == ESPBRIDGE_DEFAULT_BAUD ||
           baud == 230400UL ||
           baud == 460800UL ||
           baud == 921600UL ||
           baud == 1000000UL;
}

static void commandFastCapability(char *args) {
    unsigned long baud = 0;
    if (sscanf(args, "%lu", &baud) != 1 || !supportedBaud((uint32_t)baud)) {
        sendLine("ERR ARG unsupported_baud");
        return;
    }

    fastPayloadBaud = (uint32_t)baud;
    sendLine("OK FASTCAP %lu %u", baud, ESPBRIDGE_CHUNK_BYTES);
}

static void commandGet(char *args, bool fastPayload) {
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
    uint32_t readStartSeconds, readStartMilliseconds;
    AudioMoth_getTime(&readStartSeconds, &readStartMilliseconds);
    res = f_read(&file, chunkBuffer, (UINT)requested, &bytesRead);
    uint32_t sdReadMilliseconds = elapsedServiceMilliseconds(readStartSeconds, readStartMilliseconds);
    f_close(&file);

    if (res != FR_OK) {
        sendLine("ERR READ %u", (unsigned int)res);
        return;
    }

    uint32_t crc = crc32Update(0, chunkBuffer, bytesRead);
    if (!fastPayload) {
        sendLine("DATA %s %lu %u %08lX %lu", path, offset, (unsigned int)bytesRead,
                 (unsigned long)crc, (unsigned long)sdReadMilliseconds);
        uartWrite(chunkBuffer, bytesRead);
        return;
    }

    sendLine("FASTDATA %s %lu %u %08lX %lu %lu", path, offset, (unsigned int)bytesRead,
             (unsigned long)crc, (unsigned long)fastPayloadBaud, (unsigned long)sdReadMilliseconds);
    bridgeDelayMilliseconds(ESPBRIDGE_FAST_SWITCH_GUARD_MS);
    bridgeSetBaud(fastPayloadBaud);

    uint32_t trainingBytes = fastPayloadBaud >= ESPBRIDGE_QUICK_BAUD_THRESHOLD
        ? ESPBRIDGE_FAST_PAYLOAD_TRAINING_BYTES
        : ESPBRIDGE_SLOW_PAYLOAD_TRAINING_BYTES;
    for (uint32_t i = 0; i < trainingBytes; i += 1) {
        uartWriteByte(0x55);
    }
    static const uint8_t magic[] = {0xA5, 0x5A, 0xC3, 0x3C};
    uartWrite(magic, sizeof(magic));
    uartWrite(chunkBuffer, bytesRead);

    bridgeSetBaud(ESPBRIDGE_DEFAULT_BAUD);
    bridgeDelayMilliseconds(5);
    sendLine("OK FASTDATA %lu %u", offset, (unsigned int)bytesRead);
}

static void commandGetStream(char *args) {
    char path[ESPBRIDGE_MAX_PATH];
    unsigned long offset = 0;
    unsigned long requested = ESPBRIDGE_STREAM_BYTES;
    unsigned long baud = ESPBRIDGE_FAST_BAUD;

    if (bridgeBusy || !uploadAllowed) {
        sendLine("ERR BUSY upload_not_allowed");
        return;
    }
    if (sscanf(args, "%95s %lu %lu %lu", path, &offset, &requested, &baud) < 3) {
        sendLine("ERR ARG usage_GETSTREAM_path_offset_bytes_baud");
        return;
    }
    if (!validPath(path, true)) {
        sendLine("ERR PATH invalid_path");
        return;
    }
    if (!supportedBaud((uint32_t)baud)) {
        sendLine("ERR ARG unsupported_baud");
        return;
    }
    if (requested == 0 || requested > ESPBRIDGE_STREAM_BYTES) requested = ESPBRIDGE_STREAM_BYTES;
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

    uint32_t available = (uint32_t)(size - (FSIZE_t)offset);
    uint32_t totalBytes = (uint32_t)requested > available ? available : (uint32_t)requested;

    res = f_lseek(&file, (FSIZE_t)offset);
    if (res != FR_OK) {
        f_close(&file);
        sendLine("ERR SEEK %u", (unsigned int)res);
        return;
    }

    sendLine("STREAM %s %lu %lu %u %lu", path, offset, (unsigned long)totalBytes,
             ESPBRIDGE_CHUNK_BYTES, baud);
    if (baud != ESPBRIDGE_DEFAULT_BAUD) {
        bridgeDelayMilliseconds(ESPBRIDGE_FAST_SWITCH_GUARD_MS);
        bridgeSetBaud((uint32_t)baud);

        uint32_t trainingBytes = baud >= ESPBRIDGE_QUICK_BAUD_THRESHOLD
            ? ESPBRIDGE_FAST_PAYLOAD_TRAINING_BYTES
            : ESPBRIDGE_SLOW_PAYLOAD_TRAINING_BYTES;
        for (uint32_t i = 0; i < trainingBytes; i += 1) {
            uartWriteByte(0x55);
        }
    }

    static const uint8_t streamMagic[] = {0xA5, 0x5A, 0xD7, 0x7D};
    uint32_t sent = 0;
    bool readError = false;

    while (sent < totalBytes) {
        WDOG_Feed();
        uint32_t remaining = totalBytes - sent;
        UINT toRead = remaining > ESPBRIDGE_CHUNK_BYTES ? ESPBRIDGE_CHUNK_BYTES : (UINT)remaining;
        UINT bytesRead = 0;

        uint32_t readStartSeconds, readStartMilliseconds;
        AudioMoth_getTime(&readStartSeconds, &readStartMilliseconds);
        res = f_read(&file, chunkBuffer, toRead, &bytesRead);
        uint32_t sdReadMilliseconds = elapsedServiceMilliseconds(readStartSeconds, readStartMilliseconds);

        if (res != FR_OK || bytesRead == 0) {
            readError = true;
            break;
        }

        uint32_t frameOffset = (uint32_t)offset + sent;
        uint32_t crc = crc32Update(0, chunkBuffer, bytesRead);
        uartWrite(streamMagic, sizeof(streamMagic));
        uartWriteUInt32LE(frameOffset);
        uartWriteUInt16LE((uint16_t)bytesRead);
        uartWriteUInt32LE(crc);
        uartWriteUInt32LE(sdReadMilliseconds);
        uartWrite(chunkBuffer, bytesRead);

        sent += bytesRead;
    }

    f_close(&file);
    if (baud != ESPBRIDGE_DEFAULT_BAUD) {
        bridgeSetBaud(ESPBRIDGE_DEFAULT_BAUD);
        bridgeDelayMilliseconds(5);
    }

    if (readError || sent != totalBytes) {
        sendLine("ERR STREAM read_failed %lu %lu", (unsigned long)sent, (unsigned long)totalBytes);
        return;
    }

    for (uint32_t i = 0; i < 4; i += 1) {
        sendLine("OK STREAM %s %lu %lu", path, offset, (unsigned long)sent);
        bridgeDelayMilliseconds(15);
    }
}

static void fillTestStreamPayload(uint32_t offset, uint8_t *buffer, uint32_t length) {
    for (uint32_t i = 0; i < length; i += 1) {
        buffer[i] = (uint8_t)((offset + i) & 0xFFU);
    }
}

static void commandTestStream(char *args) {
    unsigned long requested = ESPBRIDGE_TEST_STREAM_BYTES;
    unsigned long baud = ESPBRIDGE_FAST_BAUD;

    if (sscanf(args, "%lu %lu", &requested, &baud) < 1) {
        sendLine("ERR ARG usage_TESTSTREAM_bytes_baud");
        return;
    }
    if (!supportedBaud((uint32_t)baud) || baud == ESPBRIDGE_DEFAULT_BAUD) {
        sendLine("ERR ARG unsupported_baud");
        return;
    }
    if (requested == 0 || requested > ESPBRIDGE_TEST_STREAM_BYTES) requested = ESPBRIDGE_TEST_STREAM_BYTES;

    sendLine("TESTSTREAM %lu %u %lu", requested, ESPBRIDGE_CHUNK_BYTES, baud);
    bridgeDelayMilliseconds(ESPBRIDGE_FAST_SWITCH_GUARD_MS);
    bridgeSetBaud((uint32_t)baud);

    uint32_t trainingBytes = baud >= ESPBRIDGE_QUICK_BAUD_THRESHOLD
        ? ESPBRIDGE_FAST_PAYLOAD_TRAINING_BYTES
        : ESPBRIDGE_SLOW_PAYLOAD_TRAINING_BYTES;
    for (uint32_t i = 0; i < trainingBytes; i += 1) {
        uartWriteByte(0x55);
    }

    static const uint8_t streamMagic[] = {0xA5, 0x5A, 0xD7, 0x7D};
    uint32_t sent = 0;
    while (sent < (uint32_t)requested) {
        WDOG_Feed();
        uint32_t remaining = (uint32_t)requested - sent;
        uint32_t frameBytes = remaining > ESPBRIDGE_CHUNK_BYTES ? ESPBRIDGE_CHUNK_BYTES : remaining;
        fillTestStreamPayload(sent, chunkBuffer, frameBytes);

        uint32_t crc = crc32Update(0, chunkBuffer, frameBytes);
        uartWrite(streamMagic, sizeof(streamMagic));
        uartWriteUInt32LE(sent);
        uartWriteUInt16LE((uint16_t)frameBytes);
        uartWriteUInt32LE(crc);
        uartWriteUInt32LE(0);
        uartWrite(chunkBuffer, frameBytes);

        sent += frameBytes;
    }

    bridgeSetBaud(ESPBRIDGE_DEFAULT_BAUD);
    bridgeDelayMilliseconds(5);
    for (uint32_t i = 0; i < 4; i += 1) {
        sendLine("OK TESTSTREAM %lu", (unsigned long)sent);
        bridgeDelayMilliseconds(15);
    }
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

static void commandBaud(char *args) {
    unsigned long baud = 0;
    if (sscanf(args, "%lu", &baud) != 1) {
        sendLine("ERR ARG unsupported_baud");
        return;
    }

    if (!supportedBaud((uint32_t)baud)) {
        sendLine("ERR ARG unsupported_baud");
        return;
    }

    sendLine("OK BAUD %lu", baud);
    bridgeSetBaud((uint32_t)baud);
    if (baud != ESPBRIDGE_DEFAULT_BAUD) {
        sendFastTrainingPreamble();
    }
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
    } else if (strncmp(lineBuffer, "FASTCAP ", 8) == 0) {
        commandFastCapability(lineBuffer + 8);
    } else if (strncmp(lineBuffer, "BAUD ", 5) == 0) {
        commandBaud(lineBuffer + 5);
    } else if (strcmp(lineBuffer, "STATUS") == 0) {
        commandStatus(deadlineUnixSeconds);
    } else if (strncmp(lineBuffer, "TIME ", 5) == 0) {
        commandTime(lineBuffer + 5);
    } else if (strcmp(lineBuffer, "LIST") == 0) {
        commandList();
    } else if (strncmp(lineBuffer, "GET ", 4) == 0) {
        commandGet(lineBuffer + 4, false);
    } else if (strncmp(lineBuffer, "GETFAST ", 8) == 0) {
        commandGet(lineBuffer + 8, true);
    } else if (strncmp(lineBuffer, "GETSTREAM ", 10) == 0) {
        commandGetStream(lineBuffer + 10);
    } else if (strncmp(lineBuffer, "TESTSTREAM ", 11) == 0) {
        commandTestStream(lineBuffer + 11);
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
    uint32_t readyBeaconMs = 0;
    uint32_t serviceStartSeconds, serviceStartMilliseconds;
    AudioMoth_getTime(&serviceStartSeconds, &serviceStartMilliseconds);

    configureBridgeUart();
    sendLine("OK BRIDGE_READY");

    while (elapsedServiceMilliseconds(serviceStartSeconds, serviceStartMilliseconds) < SERVICE_MAX_WINDOW_MS) {
        WDOG_Feed();

        bool deadlineDone = deadlineReached(deadlineUnixSeconds);
        bool requestActive = ESPBridge_isRequestActive();
        bool rxAvailable = uartRxAvailable();

        if (deadlineDone && !requestActive && !rxAvailable && idleMs >= SERVICE_IDLE_TIMEOUT_MS) break;

        if (!requestActive && !rxAvailable) {
            if (idleMs >= SERVICE_IDLE_TIMEOUT_MS) break;
            bridgeDelayMilliseconds(10);
            idleMs += 10;
            continue;
        }

        if (readLine(RX_LINE_TIMEOUT_MS)) {
            idleMs = 0;
            readyBeaconMs = 0;
            if (handleCommand(deadlineUnixSeconds)) break;
        } else {
            idleMs += RX_LINE_TIMEOUT_MS;
            readyBeaconMs += RX_LINE_TIMEOUT_MS;
            if (readyBeaconMs >= SERVICE_READY_BEACON_MS) {
                sendLine("OK BRIDGE_READY");
                readyBeaconMs = 0;
            }
        }
    }

    sendLine("OK BRIDGE_SLEEP");
    stopSoftUartTimer();
    serviceActive = false;
}

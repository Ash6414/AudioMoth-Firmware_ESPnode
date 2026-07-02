#!/usr/bin/env python3
"""Patch AudioMoth ESP bridge for robust ESP32 service sessions.

The ESP32 still drives ESP_REQ, but this build must also keep the UART service
alive when PA7 is not being sampled correctly. For the transfer prototype, file
commands are allowed whenever the AudioMoth bridge service is active, the
firmware is not busy recording, and the scheduler has marked uploads safe.
LIST walks a few folder levels, emits INFO ENTRY lines for
visible protocol-safe entries, skips CONFIG.TXT, and skips hidden/system or
path-unsafe SD entries such as Windows' System Volume Information. Raw FatFs
LIST/GET/DELETE operations keep the SD card clock powered while touching the
card, matching the stock AudioMoth filesystem helpers.
"""

from __future__ import annotations

from pathlib import Path

BRIDGE_C = Path("project/src/espbridge.c")

OLD_REQUIRE = "#define BRIDGE_REQUIRE_REQ_PIN              1"
NEW_REQUIRE = "#define BRIDGE_REQUIRE_REQ_PIN              0"

OLD_SERVICE_READ = "        bool requestActive = rawRequestPinActive();"
NEW_SERVICE_READ = "        bool requestActive = ESPBridge_isRequestActive();"

OLD_STATE_FLAGS = """static volatile bool uploadAllowed = false;
static bool filesystemEnabled = false;
static bool serviceActive = false;
static uint32_t fastPayloadBaud = ESPBRIDGE_FAST_BAUD;
"""

NEW_STATE_FLAGS = """static volatile bool uploadAllowed = false;
static bool filesystemEnabled = false;
static bool serviceActive = false;
static volatile bool espTimeAccepted = false;
static uint32_t fastPayloadBaud = ESPBRIDGE_FAST_BAUD;
"""

OLD_TIME_ACCEPT = """    if (milliseconds > 999) milliseconds = 999;
    AudioMoth_setTime((uint32_t)seconds, (uint32_t)milliseconds);
    sendLine("OK TIME %lu %lu", seconds, milliseconds);
}
"""

NEW_TIME_ACCEPT = """    if (milliseconds > 999) milliseconds = 999;
    AudioMoth_setTime((uint32_t)seconds, (uint32_t)milliseconds);
    espTimeAccepted = true;
    sendLine("OK TIME %lu %lu", seconds, milliseconds);
}
"""

OLD_INIT_FLAGS = """    uploadAllowed = false;
    filesystemEnabled = false;
    serviceActive = false;
}
"""

NEW_INIT_FLAGS = """    uploadAllowed = false;
    filesystemEnabled = false;
    serviceActive = false;
    espTimeAccepted = false;
}
"""

OLD_TIME_ACCEPT_API = """bool ESPBridge_isHardwareRequestActive(void) {
    return rawRequestPinActive();
}

void ESPBridge_serviceUntil(uint32_t deadlineUnixSeconds) {
"""

NEW_TIME_ACCEPT_API = """bool ESPBridge_isHardwareRequestActive(void) {
    return rawRequestPinActive();
}

bool ESPBridge_hasAcceptedTime(void) {
    return espTimeAccepted;
}

void ESPBridge_serviceUntil(uint32_t deadlineUnixSeconds) {
"""

OLD_PATH_MAY = """static bool pathMayBeAudioFile(const char *path) {
    return endsWithWav(path) || !basenameHasExtension(path);
}
"""

NEW_PATH_MAY = """static bool pathMayBeAudioFile(const char *path) {
    return endsWithWav(path) || !basenameHasExtension(path);
}

static bool charEqualsIgnoreCase(char a, char b) {
    return tolower((unsigned char)a) == tolower((unsigned char)b);
}

static bool isConfigTxtPath(const char *path) {
    const char *slash = strrchr(path, '/');
    const char *name = slash == NULL ? path : slash + 1;
    const char *config = "config.txt";

    for (uint32_t i = 0; config[i] != 0; i += 1) {
        if (name[i] == 0 || !charEqualsIgnoreCase(name[i], config[i])) return false;
    }

    return name[10] == 0;
}
"""

OLD_PATH_REQUIRE = "    if (requireWav && !pathMayBeAudioFile(path)) return false;"
NEW_PATH_REQUIRE = "    if (requireWav && isConfigTxtPath(path)) return false;"

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
    return !bridgeBusy && uploadAllowed && serviceActive;
}

static void rawFilesystemBegin(void) {
    AudioMoth_restartSDCardClock();
}

static void rawFilesystemEnd(void) {
    AudioMoth_pauseSDCardClock();
}
"""

OLD_FILE_GATE = "    if (bridgeBusy || !uploadAllowed) {"
NEW_FILE_GATE = "    if (!fileCommandsAllowed()) {"

OLD_STATUS_ALLOWED = "             uploadAllowed ? 1 : 0,"
NEW_STATUS_ALLOWED = "             fileCommandsAllowed() ? 1 : 0,"

LIST_FUNCTION_START = "static void listOneDirectory(const char *prefix) {"
LIST_FUNCTION_END = "static void commandList(void) {"
COMMAND_GET_START = "static void commandGet(char *args, bool fastPayload) {"
COMMAND_DELETE_START = "static void commandDelete(char *args) {"
COMMAND_BAUD_START = "static void commandBaud(char *args) {"

NEW_LIST_FUNCTION = r'''#define LIST_MAX_DEPTH 4
#define LIST_SKIP_ATTRS 0x06

static bool isDotDirectory(const char *name) {
    return strcmp(name, ".") == 0 || strcmp(name, "..") == 0;
}

static void buildChildPath(char *out, uint32_t outSize, const char *prefix, const char *name) {
    if (prefix[0]) snprintf(out, outSize, "%s/%s", prefix, name);
    else snprintf(out, outSize, "%s", name);
}

static bool isBridgeSafePath(const char *path) {
    return validPath(path, false);
}

static void listDirectoryRecursive(const char *prefix, uint32_t depth) {
    DIR dir;
    FILINFO fno;

    FRESULT res = f_opendir(&dir, prefix[0] ? prefix : "");
    if (res != FR_OK) {
        sendLine("INFO OPENDIR %s %u", prefix[0] ? prefix : "/", (unsigned int)res);
        return;
    }

    while (true) {
        WDOG_Feed();
        res = f_readdir(&dir, &fno);
        if (res != FR_OK) {
            sendLine("INFO READDIR %s %u", prefix[0] ? prefix : "/", (unsigned int)res);
            break;
        }
        if (fno.fname[0] == 0) break;
        if (isDotDirectory(fno.fname)) continue;

        char full[ESPBRIDGE_MAX_PATH];
        buildChildPath(full, sizeof(full), prefix, fno.fname);

        if (fno.fattrib & LIST_SKIP_ATTRS) {
            sendLine("INFO SKIP_ATTR %s %u", full, (unsigned int)fno.fattrib);
            continue;
        }

        if (!isBridgeSafePath(full)) {
            sendLine("INFO SKIP_PATH %s", full);
            continue;
        }

        sendLine("INFO ENTRY %s %lu %u", full, (unsigned long)fno.fsize, (unsigned int)fno.fattrib);

        if (fno.fattrib & AM_DIR) {
            if (depth < LIST_MAX_DEPTH) {
                listDirectoryRecursive(full, depth + 1);
            } else {
                sendLine("INFO SKIP_DEPTH %s", full);
            }
        } else if (!isConfigTxtPath(full)) {
            sendLine("FILE %s %lu", full, (unsigned long)fno.fsize);
        } else {
            sendLine("INFO SKIP_CONFIG %s", full);
        }
    }

    f_closedir(&dir);
}

static void listOneDirectory(const char *prefix) {
    rawFilesystemBegin();
    listDirectoryRecursive(prefix, 0);
    rawFilesystemEnd();
}

'''

NEW_COMMAND_GET = r'''static void commandGet(char *args, bool fastPayload) {
    char path[ESPBRIDGE_MAX_PATH];
    unsigned long offset = 0;
    unsigned long requested = ESPBRIDGE_CHUNK_BYTES;

    if (!fileCommandsAllowed()) {
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

    rawFilesystemBegin();

    FIL file;
    FRESULT res = f_open(&file, path, FA_READ);
    if (res != FR_OK) {
        rawFilesystemEnd();
        sendLine("ERR OPEN %u", (unsigned int)res);
        return;
    }

    FSIZE_t size = f_size(&file);
    if ((FSIZE_t)offset > size) {
        f_close(&file);
        rawFilesystemEnd();
        sendLine("ERR RANGE offset_past_eof");
        return;
    }

    res = f_lseek(&file, (FSIZE_t)offset);
    if (res != FR_OK) {
        f_close(&file);
        rawFilesystemEnd();
        sendLine("ERR SEEK %u", (unsigned int)res);
        return;
    }

    UINT bytesRead = 0;
    uint32_t readStartSeconds, readStartMilliseconds;
    AudioMoth_getTime(&readStartSeconds, &readStartMilliseconds);
    res = f_read(&file, chunkBuffer, (UINT)requested, &bytesRead);
    uint32_t sdReadMilliseconds = elapsedServiceMilliseconds(readStartSeconds, readStartMilliseconds);
    FRESULT closeRes = f_close(&file);
    rawFilesystemEnd();

    if (res != FR_OK) {
        sendLine("ERR READ %u", (unsigned int)res);
        return;
    }
    if (closeRes != FR_OK) {
        sendLine("ERR CLOSE %u", (unsigned int)closeRes);
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

    if (!fileCommandsAllowed()) {
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

    rawFilesystemBegin();

    FIL file;
    FRESULT res = f_open(&file, path, FA_READ);
    if (res != FR_OK) {
        rawFilesystemEnd();
        sendLine("ERR OPEN %u", (unsigned int)res);
        return;
    }

    FSIZE_t size = f_size(&file);
    if ((FSIZE_t)offset > size) {
        f_close(&file);
        rawFilesystemEnd();
        sendLine("ERR RANGE offset_past_eof");
        return;
    }

    uint32_t available = (uint32_t)(size - (FSIZE_t)offset);
    uint32_t totalBytes = (uint32_t)requested > available ? available : (uint32_t)requested;

    res = f_lseek(&file, (FSIZE_t)offset);
    if (res != FR_OK) {
        f_close(&file);
        rawFilesystemEnd();
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

    FRESULT closeRes = f_close(&file);
    rawFilesystemEnd();
    if (baud != ESPBRIDGE_DEFAULT_BAUD) {
        bridgeSetBaud(ESPBRIDGE_DEFAULT_BAUD);
        bridgeDelayMilliseconds(5);
    }

    if (readError || sent != totalBytes) {
        sendLine("ERR STREAM read_failed %lu %lu", (unsigned long)sent, (unsigned long)totalBytes);
        return;
    }
    if (closeRes != FR_OK) {
        sendLine("ERR CLOSE %u", (unsigned int)closeRes);
        return;
    }

    for (uint32_t i = 0; i < 4; i += 1) {
        sendLine("OK STREAM %s %lu %lu", path, offset, (unsigned long)sent);
        bridgeDelayMilliseconds(15);
    }
}

static void commandGetPipe(char *args) {
    char path[ESPBRIDGE_MAX_PATH];
    unsigned long offset = 0;
    unsigned long requested = 0;
    unsigned long baud = ESPBRIDGE_PIPE_BAUD;

    if (!fileCommandsAllowed()) {
        sendLine("ERR BUSY upload_not_allowed");
        return;
    }
    if (sscanf(args, "%95s %lu %lu %lu", path, &offset, &requested, &baud) < 3) {
        sendLine("ERR ARG usage_GETPIPE_path_offset_bytes_baud");
        return;
    }
    if (!validPath(path, true)) {
        sendLine("ERR PATH invalid_path");
        return;
    }
    if (!supportedBaud((uint32_t)baud) || baud == ESPBRIDGE_DEFAULT_BAUD) {
        sendLine("ERR ARG unsupported_baud");
        return;
    }
    if (!ensureFilesystem()) {
        sendLine("ERR SD filesystem_enable_failed");
        return;
    }

    rawFilesystemBegin();

    FIL file;
    FRESULT res = f_open(&file, path, FA_READ);
    if (res != FR_OK) {
        rawFilesystemEnd();
        sendLine("ERR OPEN %u", (unsigned int)res);
        return;
    }

    FSIZE_t size = f_size(&file);
    if ((FSIZE_t)offset > size) {
        f_close(&file);
        rawFilesystemEnd();
        sendLine("ERR RANGE offset_past_eof");
        return;
    }

    uint32_t available = (uint32_t)(size - (FSIZE_t)offset);
    uint32_t totalBytes = (requested == 0 || requested > available) ? available : (uint32_t)requested;

    res = f_lseek(&file, (FSIZE_t)offset);
    if (res != FR_OK) {
        f_close(&file);
        rawFilesystemEnd();
        sendLine("ERR SEEK %u", (unsigned int)res);
        return;
    }

    sendLine("PIPE %s %lu %lu %lu %u %lu",
             path,
             offset,
             (unsigned long)totalBytes,
             (unsigned long)ESPBRIDGE_PIPE_BLOCK_BYTES,
             ESPBRIDGE_CHUNK_BYTES,
             baud);

    if (totalBytes == 0) {
        f_close(&file);
        rawFilesystemEnd();
        sendLine("OK PIPEDONE %s %lu 0", path, offset);
        return;
    }

    static const uint8_t streamMagic[] = {0xA5, 0x5A, 0xD7, 0x7D};
    uint32_t sentTotal = 0;
    bool readError = false;

    while (sentTotal < totalBytes) {
        uint32_t blockOffset = (uint32_t)offset + sentTotal;
        uint32_t blockRemaining = totalBytes - sentTotal;
        uint32_t blockTarget = blockRemaining > ESPBRIDGE_PIPE_BLOCK_BYTES
            ? ESPBRIDGE_PIPE_BLOCK_BYTES
            : blockRemaining;
        uint32_t blockSent = 0;

        bridgeDelayMilliseconds(ESPBRIDGE_FAST_SWITCH_GUARD_MS);
        bridgeSetBaud((uint32_t)baud);

        uint32_t trainingBytes = baud >= ESPBRIDGE_QUICK_BAUD_THRESHOLD
            ? ESPBRIDGE_FAST_PAYLOAD_TRAINING_BYTES
            : ESPBRIDGE_SLOW_PAYLOAD_TRAINING_BYTES;
        for (uint32_t i = 0; i < trainingBytes; i += 1) {
            uartWriteByte(0x55);
        }

        while (blockSent < blockTarget) {
            WDOG_Feed();
            uint32_t remaining = blockTarget - blockSent;
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

            uint32_t frameOffset = blockOffset + blockSent;
            uint32_t crc = crc32Update(0, chunkBuffer, bytesRead);
            uartWrite(streamMagic, sizeof(streamMagic));
            uartWriteUInt32LE(frameOffset);
            uartWriteUInt16LE((uint16_t)bytesRead);
            uartWriteUInt32LE(crc);
            uartWriteUInt32LE(sdReadMilliseconds);
            uartWrite(chunkBuffer, bytesRead);

            blockSent += bytesRead;
        }

        bridgeSetBaud(ESPBRIDGE_DEFAULT_BAUD);
        bridgeDelayMilliseconds(5);

        if (readError || blockSent != blockTarget) {
            f_close(&file);
            rawFilesystemEnd();
            sendLine("ERR PIPE read_failed %lu %lu",
                     (unsigned long)(sentTotal + blockSent),
                     (unsigned long)totalBytes);
            return;
        }

        sentTotal += blockSent;
        sendLine("OK PIPEBLOCK %s %lu %lu", path, (unsigned long)blockOffset, (unsigned long)blockSent);

        if (sentTotal >= totalBytes) break;

        if (!readLine(ESPBRIDGE_PIPE_NEXT_TIMEOUT_MS)) {
            f_close(&file);
            rawFilesystemEnd();
            sendLine("ERR PIPE next_timeout %lu %lu",
                     (unsigned long)sentTotal,
                     (unsigned long)totalBytes);
            return;
        }

        if (strcmp(lineBuffer, "STOP") == 0) {
            f_close(&file);
            rawFilesystemEnd();
            sendLine("OK PIPESTOP %s %lu %lu", path, offset, (unsigned long)sentTotal);
            return;
        }

        unsigned long nextOffset = 0;
        if (strcmp(lineBuffer, "NEXT") != 0 &&
            (sscanf(lineBuffer, "NEXT %lu", &nextOffset) != 1 ||
             nextOffset != (unsigned long)((uint32_t)offset + sentTotal))) {
            f_close(&file);
            rawFilesystemEnd();
            sendLine("ERR PIPE expected_NEXT");
            return;
        }
    }

    FRESULT closeRes = f_close(&file);
    rawFilesystemEnd();
    if (closeRes != FR_OK) {
        sendLine("ERR CLOSE %u", (unsigned int)closeRes);
        return;
    }

    sendLine("OK PIPEDONE %s %lu %lu", path, offset, (unsigned long)sentTotal);
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

'''

NEW_COMMAND_DELETE = r'''static void commandDelete(char *args) {
    char path[ESPBRIDGE_MAX_PATH];

    if (!fileCommandsAllowed()) {
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

    rawFilesystemBegin();
    FRESULT res = f_unlink(path);
    rawFilesystemEnd();

    if (res == FR_OK) sendLine("OK DELETE %s", path);
    else sendLine("ERR DELETE %u", (unsigned int)res);
}

'''


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


def replace_between(text: str, start: str, end: str, new: str, label: str) -> tuple[str, bool]:
    if "static void listDirectoryRecursive" in text and "SKIP_ATTR" in text and "rawFilesystemBegin();" in text:
        return text, False
    start_index = text.find(start)
    if start_index < 0:
        raise SystemExit(f"Could not find ESP bridge {label} start text to patch")
    end_index = text.find(end, start_index)
    if end_index < 0:
        raise SystemExit(f"Could not find ESP bridge {label} end text to patch")
    return text[:start_index] + new + text[end_index:], True


def replace_function(text: str, start: str, end: str, new: str, label: str, marker: str) -> tuple[str, bool]:
    start_index = text.find(start)
    if start_index < 0:
        raise SystemExit(f"Could not find ESP bridge {label} start text to patch")
    end_index = text.find(end, start_index)
    if end_index < 0:
        raise SystemExit(f"Could not find ESP bridge {label} end text to patch")
    block = text[start_index:end_index]
    if marker in block:
        return text, False
    return text[:start_index] + new + text[end_index:], True


def main() -> None:
    text = BRIDGE_C.read_text(encoding="utf-8")
    text, require_changed = replace_once(text, OLD_REQUIRE, NEW_REQUIRE, "request-gate define")
    text, service_changed = replace_once(text, OLD_SERVICE_READ, NEW_SERVICE_READ, "service request check")
    text, state_changed = replace_once(text, OLD_STATE_FLAGS, NEW_STATE_FLAGS, "accepted-time state")
    text, time_changed = replace_once(text, OLD_TIME_ACCEPT, NEW_TIME_ACCEPT, "accepted-time latch")
    text, init_changed = replace_once(text, OLD_INIT_FLAGS, NEW_INIT_FLAGS, "accepted-time init")
    text, api_changed = replace_once(text, OLD_TIME_ACCEPT_API, NEW_TIME_ACCEPT_API, "accepted-time API")
    text, path_helper_changed = replace_once(text, OLD_PATH_MAY, NEW_PATH_MAY, "config path helper")
    text, path_require_changed = replace_once(text, OLD_PATH_REQUIRE, NEW_PATH_REQUIRE, "GET/DELETE config path guard")
    text, helper_changed = replace_once(text, OLD_ENSURE_FILESYSTEM, NEW_ENSURE_FILESYSTEM, "idle file-command helper")
    text, gate_count = replace_all(text, OLD_FILE_GATE, NEW_FILE_GATE, "file-command gate", 3)
    text, status_changed = replace_once(text, OLD_STATUS_ALLOWED, NEW_STATUS_ALLOWED, "STATUS allowed flag")
    text, list_changed = replace_between(text, LIST_FUNCTION_START, LIST_FUNCTION_END, NEW_LIST_FUNCTION, "recursive LIST")
    text, get_changed = replace_function(text, COMMAND_GET_START, COMMAND_DELETE_START, NEW_COMMAND_GET, "powered GET", "rawFilesystemBegin();")
    text, delete_changed = replace_function(text, COMMAND_DELETE_START, COMMAND_BAUD_START, NEW_COMMAND_DELETE, "powered DELETE", "rawFilesystemBegin();")
    BRIDGE_C.write_text(text, encoding="utf-8")

    if require_changed:
        print("Applied ESP optional request-gate patch")
    else:
        print("ESP optional request-gate patch already applied")

    if service_changed:
        print("Applied ESP optional service-loop patch")
    else:
        print("ESP optional service-loop patch already applied")

    if state_changed:
        print("Applied ESP accepted-time state patch")
    else:
        print("ESP accepted-time state patch already applied")

    if time_changed:
        print("Applied ESP accepted-time latch patch")
    else:
        print("ESP accepted-time latch patch already applied")

    if init_changed:
        print("Applied ESP accepted-time init patch")
    else:
        print("ESP accepted-time init patch already applied")

    if api_changed:
        print("Applied ESP accepted-time API patch")
    else:
        print("ESP accepted-time API patch already applied")

    if path_helper_changed:
        print("Applied ESP config path helper patch")
    else:
        print("ESP config path helper patch already applied")

    if path_require_changed:
        print("Applied ESP GET/DELETE config path guard patch")
    else:
        print("ESP GET/DELETE config path guard patch already applied")

    if helper_changed:
        print("Applied ESP idle file-command helper and SD power patch")
    else:
        print("ESP idle file-command helper and SD power patch already applied")

    if gate_count:
        print(f"Applied ESP idle file-command gate patch to {gate_count} command handlers")
    else:
        print("ESP idle file-command gate patch already applied")

    if status_changed:
        print("Applied ESP STATUS allowed flag patch")
    else:
        print("ESP STATUS allowed flag patch already applied")

    if list_changed:
        print("Applied ESP recursive safe any-file SD LIST diagnostics patch")
    else:
        print("ESP recursive safe any-file SD LIST diagnostics patch already applied")

    if get_changed:
        print("Applied ESP powered SD GET patch")
    else:
        print("ESP powered SD GET patch already applied")

    if delete_changed:
        print("Applied ESP powered SD DELETE patch")
    else:
        print("ESP powered SD DELETE patch already applied")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Patch AudioMoth ESP bridge for robust ESP32 service sessions.

The ESP32 still drives ESP_REQ, but this build must also keep the UART service
alive when PA7 is not being sampled correctly. For the transfer prototype, file
commands are allowed whenever the AudioMoth bridge service is active and the
firmware is not busy recording, instead of waiting for the scheduler's separate
uploadAllowed flag. LIST walks a few folder levels, emits INFO ENTRY lines for
all visible entries, and exposes any regular file except CONFIG.TXT. Raw FatFs
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
    return !bridgeBusy && serviceActive;
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
COMMAND_GET_START = "static void commandGet(char *args) {"
COMMAND_DELETE_START = "static void commandDelete(char *args) {"
COMMAND_TIME_START = "static void commandTime(char *args) {"

NEW_LIST_FUNCTION = r'''#define LIST_MAX_DEPTH 4

static bool isDotDirectory(const char *name) {
    return strcmp(name, ".") == 0 || strcmp(name, "..") == 0;
}

static void buildChildPath(char *out, uint32_t outSize, const char *prefix, const char *name) {
    if (prefix[0]) snprintf(out, outSize, "%s/%s", prefix, name);
    else snprintf(out, outSize, "%s", name);
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

NEW_COMMAND_GET = r'''static void commandGet(char *args) {
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
    res = f_read(&file, chunkBuffer, (UINT)requested, &bytesRead);
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
    sendLine("DATA %s %lu %u %08lX", path, offset, (unsigned int)bytesRead, (unsigned long)crc);
    uartWrite(chunkBuffer, bytesRead);
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
    if "static void listDirectoryRecursive" in text and "SKIP_CONFIG" in text and "rawFilesystemBegin();" in text:
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
    text, path_helper_changed = replace_once(text, OLD_PATH_MAY, NEW_PATH_MAY, "config path helper")
    text, path_require_changed = replace_once(text, OLD_PATH_REQUIRE, NEW_PATH_REQUIRE, "GET/DELETE config path guard")
    text, helper_changed = replace_once(text, OLD_ENSURE_FILESYSTEM, NEW_ENSURE_FILESYSTEM, "idle file-command helper")
    text, gate_count = replace_all(text, OLD_FILE_GATE, NEW_FILE_GATE, "file-command gate", 3)
    text, status_changed = replace_once(text, OLD_STATUS_ALLOWED, NEW_STATUS_ALLOWED, "STATUS allowed flag")
    text, list_changed = replace_between(text, LIST_FUNCTION_START, LIST_FUNCTION_END, NEW_LIST_FUNCTION, "recursive LIST")
    text, get_changed = replace_function(text, COMMAND_GET_START, COMMAND_DELETE_START, NEW_COMMAND_GET, "powered GET", "rawFilesystemBegin();")
    text, delete_changed = replace_function(text, COMMAND_DELETE_START, COMMAND_TIME_START, NEW_COMMAND_DELETE, "powered DELETE", "rawFilesystemBegin();")
    BRIDGE_C.write_text(text, encoding="utf-8")

    if require_changed:
        print("Applied ESP optional request-gate patch")
    else:
        print("ESP optional request-gate patch already applied")

    if service_changed:
        print("Applied ESP optional service-loop patch")
    else:
        print("ESP optional service-loop patch already applied")

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
        print("Applied ESP recursive any-file SD LIST diagnostics patch")
    else:
        print("ESP recursive any-file SD LIST diagnostics patch already applied")

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
#!/usr/bin/env python3
"""Add SD capacity reporting to the ESP bridge LIST command.

The optional request patch owns the final safe recursive LIST implementation.
This patch runs after it and injects a compact `SD total_kb=... free_kb=...`
protocol line before the FILE entries, so the ESP32 can include card free space
in the manifest and heartbeat.
"""

from __future__ import annotations

from pathlib import Path

BRIDGE_C = Path("project/src/espbridge.c")

INSERT_AFTER = """static bool isBridgeSafePath(const char *path) {
    return validPath(path, false);
}

"""

SD_HELPER = r'''static uint32_t clampUint64ToUint32(uint64_t value) {
    return value > 0xFFFFFFFFULL ? 0xFFFFFFFFUL : (uint32_t)value;
}

static void sendSdInfo(void) {
    FATFS *fs = NULL;
    DWORD freeClusters = 0;

    FRESULT res = f_getfree("", &freeClusters, &fs);
    if (res != FR_OK || fs == NULL) {
        sendLine("INFO SD_FREE %u", (unsigned int)res);
        return;
    }

    uint64_t totalClusters = fs->n_fatent > 2U ? (uint64_t)(fs->n_fatent - 2U) : 0ULL;
    uint64_t totalKb = (totalClusters * (uint64_t)fs->csize * 512ULL) / 1024ULL;
    uint64_t freeKb = ((uint64_t)freeClusters * (uint64_t)fs->csize * 512ULL) / 1024ULL;

    sendLine("SD total_kb=%lu free_kb=%lu",
             (unsigned long)clampUint64ToUint32(totalKb),
             (unsigned long)clampUint64ToUint32(freeKb));
}

'''

OLD_LIST_START = """static void listOneDirectory(const char *prefix) {
    rawFilesystemBegin();
    listDirectoryRecursive(prefix, 0);
"""

NEW_LIST_START = """static void listOneDirectory(const char *prefix) {
    rawFilesystemBegin();
    sendSdInfo();
    listDirectoryRecursive(prefix, 0);
"""


def replace_once(text: str, old: str, new: str, label: str) -> tuple[str, bool]:
    if new in text:
        return text, False
    if old not in text:
        raise SystemExit(f"Could not find ESP bridge {label} text to patch")
    return text.replace(old, new, 1), True


def main() -> None:
    text = BRIDGE_C.read_text(encoding="utf-8")
    text, helper_changed = replace_once(text, INSERT_AFTER, INSERT_AFTER + SD_HELPER, "SD info helper insertion point")
    text, list_changed = replace_once(text, OLD_LIST_START, NEW_LIST_START, "LIST SD info call")
    BRIDGE_C.write_text(text, encoding="utf-8")

    if helper_changed:
        print("Applied ESP bridge SD info helper patch")
    else:
        print("ESP bridge SD info helper patch already applied")

    if list_changed:
        print("Applied ESP bridge LIST SD info call patch")
    else:
        print("ESP bridge LIST SD info call patch already applied")


if __name__ == "__main__":
    main()

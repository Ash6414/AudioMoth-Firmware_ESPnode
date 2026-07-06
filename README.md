# AudioMoth-Firmware-Basic

Custom AudioMoth Dev firmware for the ESP32 AudioMoth bridge node.

This fork intentionally keeps the USB firmware name `AudioMoth-Firmware-Basic`
so the AudioMoth Configuration App treats it like standard Basic firmware. Do
not rename the firmware description in `src/main.c` unless Configurator
compatibility is intentionally being removed.

## Current ESP bridge behavior

- Firmware name: `AudioMoth-Firmware-Basic`
- Control UART: `115200`
- Current production upload path: protocol v4 ACKed `GETPIPE` at stable `115200` AudioMoth-to-ESP payload baud
- UART pipe frame: 2048 bytes with CRC32 and ESP ACK/NAK retry
- Pipe payload: up to 65536 bytes per server block, sent as CRC32-checked 2048-byte frames
- Each data header reports SD read milliseconds for end-to-end bottleneck measurement
- Bridge transport: hardware UART1 LOC2 on PB9/PB10, restored to the older
  functioning bridge path for stable ESP-to-AudioMoth command receive
- RX handling: all ESP-to-AudioMoth commands remain at 115200 baud on UART1
- UART1 RX interrupt bytes are buffered before the line parser reads them, so
  bursty ESP control writes do not lose characters while the bridge is waiting
  on timeouts or SD/server handoff commands
- Command line reads use a bounded wall-time timeout even when noisy bytes are
  arriving, so Wi-Fi-side UART noise cannot trap the bridge inside one partial
  command forever
- ESP request pin: PA7. The production bridge keeps the old working logical
  request fallback because successful field logs show `req=1 req_pin=0`.
- AudioMoth busy pin: PA8
- GPS support: disabled so the bridge owns PA7, PA8, PB9, and PB10
- AudioMoth opens a guarded upload-capable bridge window on each CUSTOM/DEFAULT
  wake, then idles out if no raw ESP request or UART traffic is present
- `OK BRIDGE_READY` repeats while the bridge service is idle, including during
  the guarded no-request grace window
- `PING`, `STATUS`, `TIME`, and `DONE` work in the bridge window
- `LIST`, `GET`, `GETPIPE`, and `DELETE` work while
  bridge service is active and the scheduler has marked file upload safe
- Newly flashed or schedule-less nodes still open a safe file-upload bridge
  on wake, so existing SD files can be recovered before the next recording
  schedule is configured
- `LIST` recursively walks SD card folders up to 4 levels deep
- `LIST` includes any regular SD file except `CONFIG.TXT` / `config.txt`
- File discovery does not require a `.WAV` suffix
- `LIST` emits `SD total_kb=... free_kb=...` before file entries

The 115200 bridge rate makes startup tolerant of resets and avoids losing time
to failed baud switches. A matching ESP uses `GETPIPE` to send one 115200-baud
command, then AudioMoth keeps the SD file open and streams repeated 115200-baud
payload blocks. Each block is split into framed
2048-byte chunks with offset, length, CRC32, and SD-read milliseconds. The ESP
ACKs each good frame and NAKs bad frames so AudioMoth can resend before moving
on. AudioMoth waits inside the same command for `NEXT <offset>` after each
64 KiB block, so the ESP only advances after the server accepts the previous
block. `GET` remains available as a compatibility command, but the matching
production ESP32 upload path uses the ACKed `GETPIPE` pipe. The dashboard and
production ESP command handler expose only the stable 115200-baud transfer
path.

The bridge keeps a small UART1 RX ring buffer fed by the stock GPS-interface RX
interrupt path. This is important because the original polling-only reader could
sleep for 1 ms just as the ESP sent a command line, causing chopped commands
like `PIG`, `ST`, or `TI17`.

The bridge keeps the raw PA7 request-pin state separate from logical UART
service availability. Startup uses a guarded upload window to avoid ESP32 and
AudioMoth reset-order races, while the long-running service still uses raw PA7
and UART traffic to decide whether it should stay awake. This intentionally
matches the older functioning bridge behavior where the ESP could talk even
when PA7 read low on the AudioMoth side.

## ESP32 wiring

```text
ESP32-WROOM-U                 AudioMoth Dev
GPIO32 RX2  <---------------- PB9 UART TX
GPIO33 TX2  ----------------> PB10 UART RX
GPIO25 OUT  ----------------> PA7 ESP_REQ
GPIO26 IN   <---------------- PA8 MOTH_BUSY
GND         ----------------- GND
```

## Flash notes

Download the latest successful `audiomoth-firmware-bin` GitHub Actions
artifact and flash `audiomoth.bin` with the AudioMoth Flash App. Then put the
AudioMoth switch back into CUSTOM/run mode before testing with the ESP32.

The matching ESP32 sketch is in `Ash6414/Espmoth` under
`ESPBridge-MothNode1`.

## Build notes

GitHub Actions overlays this repository onto AudioMoth-Project, applies the
safe SD-service patches, builds `audiomoth.bin`, verifies
the bridge strings, and publishes the binary, hex, map, listing, and build
metadata as one artifact.

Compatible with the [AudioMoth Configuration App](https://github.com/OpenAcousticDevices/AudioMoth-Configuration-App).
For standard AudioMoth usage instructions, visit
[Open Acoustic Devices](https://www.openacousticdevices.info/getting-started).

## License

Copyright 2017 [Open Acoustic Devices](http://www.openacousticdevices.info/).

[MIT license](http://www.openacousticdevices.info/license).

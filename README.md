# AudioMoth-Firmware-Basic

Custom AudioMoth Dev firmware for the ESP32 AudioMoth bridge node.

This fork intentionally keeps the USB firmware name `AudioMoth-Firmware-Basic`
so the AudioMoth Configuration App treats it like standard Basic firmware. Do
not rename the firmware description in `src/main.c` unless Configurator
compatibility is intentionally being removed.

## Current ESP bridge behavior

- Firmware name: `AudioMoth-Firmware-Basic`
- Control UART: `115200`
- Fast one-way stream UARTs supported: `230400`, `460800`, `921600`
- Current production upload path: protocol v4 `GETPIPE` at `921600` AudioMoth-to-ESP payload baud
- Fast stream startup: 20 ms guard, then `0x55` training bytes and framed binary data
- Lower payload rates use a shorter 128-byte training preamble
- UART pipe frame: 2048 bytes with CRC32 and ESP ACK/NAK retry
- Pipe payload: up to 131072 bytes per server block, sent as CRC32-checked 2048-byte frames
- Each data header reports SD read milliseconds for end-to-end bottleneck measurement
- Bridge transport: EFM32 `UART1` LOC2 hardware route on PB9/PB10
- RX handling: all ESP-to-AudioMoth commands remain at 115200 baud; UART1 is
  polled directly with the interrupt buffer as a backup so SD-backed streams
  can keep accepting control commands after each chunk
- Command line reads use a bounded wall-time timeout even when noisy bytes are
  arriving, so Wi-Fi-side UART noise cannot trap the bridge inside one partial
  command forever
- ESP request pin: PA7
- AudioMoth busy pin: PA8
- GPS support: disabled so the bridge owns PA7, PA8, PB9, PB10, and UART1
- `OK BRIDGE_READY` repeats while the bridge service is idle
- `PING`, `STATUS`, `TIME`, `FASTCAP`, and `DONE` work in the bridge window
- `LIST`, `GET`, `GETFAST`, `GETSTREAM`, `GETPIPE`, and `DELETE` work while
  bridge service is active and the AudioMoth is not busy recording
- `TESTSTREAM` sends a deterministic 1 MiB max framed stream without touching SD, for UART speed checks
- `LIST` recursively walks SD card folders up to 4 levels deep
- `LIST` includes any regular SD file except `CONFIG.TXT` / `config.txt`
- File discovery does not require a `.WAV` suffix
- `LIST` emits `SD total_kb=... free_kb=...` before file entries

The 115200 control rate makes startup tolerant of resets and avoids the weak
high-speed ESP-to-AudioMoth receive direction. A matching ESP uses `GETPIPE`
to send one 115200-baud command, then AudioMoth keeps the SD file open and
streams repeated 921600-baud payload blocks. Each block is split into framed
2048-byte chunks with offset, length, CRC32, and SD-read milliseconds. The ESP
ACKs each good frame and NAKs bad frames so AudioMoth can resend before moving
on. AudioMoth returns to 115200 after each 128 KiB block and waits inside the
same command for `NEXT <offset>`, so the ESP only advances after the server
accepts the previous block. `GET` remains as the compatibility fallback.

For no-SD-card throughput diagnostics, the matching ESP can issue
`TESTSTREAM <bytes> <baud>`. AudioMoth sends the same framed format as
`GETSTREAM`, with predictable byte values and CRC32 per frame, so the ESP can
measure the UART bottleneck before a real recording is available. The current
production upload path uses ACKed 921600-baud frames so isolated byte errors are
retried instead of failing the whole file.

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
hardware UART and safe SD-service patches, builds `audiomoth.bin`, verifies
the bridge strings, and publishes the binary, hex, map, listing, and build
metadata as one artifact.

Compatible with the [AudioMoth Configuration App](https://github.com/OpenAcousticDevices/AudioMoth-Configuration-App).
For standard AudioMoth usage instructions, visit
[Open Acoustic Devices](https://www.openacousticdevices.info/getting-started).

## License

Copyright 2017 [Open Acoustic Devices](http://www.openacousticdevices.info/).

[MIT license](http://www.openacousticdevices.info/license).

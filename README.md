# AudioMoth-Firmware-Basic

Custom AudioMoth Dev firmware for the ESP32 AudioMoth bridge node.

This fork intentionally keeps the USB firmware name `AudioMoth-Firmware-Basic`
so the AudioMoth Configuration App treats it like standard Basic firmware. Do
not rename the firmware description in `src/main.c` unless Configurator
compatibility is intentionally being removed.

## Current ESP bridge behavior

- Firmware name: `AudioMoth-Firmware-Basic`
- Control UART: `115200`
- Fast payload UART: `921600`
- Fast payload startup: 20 ms guard, 128 bytes of `0x55`, then a binary marker
- UART file payload: 8192 bytes with CRC32
- Bridge transport: EFM32 `UART1` LOC2 hardware route on PB9/PB10
- RX handling: all ESP-to-AudioMoth commands remain at 115200 baud
- ESP request pin: PA7
- AudioMoth busy pin: PA8
- GPS support: disabled so the bridge owns PA7, PA8, PB9, PB10, and UART1
- `OK BRIDGE_READY` repeats while the bridge service is idle
- `PING`, `STATUS`, `TIME`, `FASTCAP`, and `DONE` work in the bridge window
- `LIST`, `GET`, `GETFAST`, and `DELETE` work while bridge service is active and the
  AudioMoth is not busy recording
- `LIST` recursively walks SD card folders up to 4 levels deep
- `LIST` includes any regular SD file except `CONFIG.TXT` / `config.txt`
- File discovery does not require a `.WAV` suffix
- `LIST` emits `SD total_kb=... free_kb=...` before file entries

The 115200 control rate makes startup tolerant of resets and avoids the weak
high-speed ESP-to-AudioMoth receive direction. A matching ESP arms `GETFAST`;
AudioMoth switches only each 8192-byte payload to 921600 baud, sends it to the ESP,
and automatically returns to 115200 before accepting the next command.

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

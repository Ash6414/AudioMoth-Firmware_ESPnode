# AudioMoth-Firmware-Basic

Custom AudioMoth Dev firmware for the ESP32 AudioMoth bridge node.

This fork intentionally keeps the USB firmware name `AudioMoth-Firmware-Basic`
so the AudioMoth Configuration App treats it like the standard Basic firmware.
Do not rename the firmware description in `src/main.c` unless you also want to
break Configurator compatibility.

## Current ESP bridge behavior

- Firmware name: `AudioMoth-Firmware-Basic`
- Bridge UART: `115200`
- Bridge transport: EFM32 `UART1` hardware route on PB9/PB10
- ESP request pin: PA7
- AudioMoth busy pin: PA8
- GPS support: disabled so the ESP bridge owns PA7, PA8, PB9, PB10, and UART1
- `OK BRIDGE_READY` repeats while the bridge service is idle
- `PING`, `STATUS`, `TIME`, and `DONE` work in the early bridge window
- `LIST`, `GET`, and `DELETE` work while the bridge service is active and the
  AudioMoth is not busy recording
- `LIST` recursively walks SD card folders up to 4 levels deep
- `LIST` includes any regular SD file except `CONFIG.TXT` / `config.txt`
- File discovery does not require a `.WAV` suffix
- `LIST` emits `SD total_kb=... free_kb=...` before file entries so the ESP32
  can send free SD space in its manifest and heartbeat

## ESP32 wiring

```text
ESP32-WROOM-U                 AudioMoth Dev
GPIO16 RX2  <---------------- PB9 UART TX
GPIO17 TX2  ----------------> PB10 UART RX
GPIO25 OUT  ----------------> PA7 ESP_REQ
GPIO26 IN   <---------------- PA8 MOTH_BUSY
GND         ----------------- GND
```

## Flash notes

Flash the generated `audiomoth.bin` with the AudioMoth Flash App, then put the
AudioMoth switch back into CUSTOM/run mode before testing with the ESP32.

The matching ESP32 sketch is in the `Ash6414/Espmoth` repository under
`ESPBridge-MothNode1`.

## Build notes

The repo is based on the Open Acoustic Devices Basic firmware layout. Build it
inside the AudioMoth project toolchain and keep the output firmware named like
Basic so the Configurator remains compatible.

Compatible with the [AudioMoth Configuration App](https://github.com/OpenAcousticDevices/AudioMoth-Configuration-App).
For standard AudioMoth usage instructions, visit
[Open Acoustic Devices](https://www.openacousticdevices.info/getting-started).

## License

Copyright 2017 [Open Acoustic Devices](http://www.openacousticdevices.info/).

[MIT license](http://www.openacousticdevices.info/license).

/****************************************************************************
 * gps.c
 * Bridge-safe GPS stubs for the ESP32 AudioMoth prototype.
 *
 * The stock AudioMoth GPS driver uses the same expansion header resources that
 * the ESP bridge needs:
 *   - PA7: GPS enable in stock firmware, ESP_REQ in the bridge
 *   - PA8/TIMER2: GPS PPS in stock firmware, MOTH_BUSY in the bridge
 *   - PB9/PB10/UART1: GPS serial path in stock firmware, bridge UART here
 *
 * For this prototype, GPS and magnetic-switch support are intentionally disabled
 * so ESPBridge owns those pins and UART1 for the full runtime.
 *****************************************************************************/

#include <stdint.h>
#include <stdbool.h>

#include "gps.h"
#include "espbridge.h"

/* Interrupt entry points kept as no-ops so linked vector names remain defined. */

void GPIO_ODD_IRQHandler(void) {
}

void GPSInterface_handleReceivedByte(uint8_t byte) {
    ESPBridge_handleReceivedByte(byte);
}

void GPSInterface_handlePulsePerSecond(uint32_t counter, uint32_t counterPeriod, uint32_t counterFrequency) {
    (void)counter;
    (void)counterPeriod;
    (void)counterFrequency;
}

void GPSInterface_handleTick(void) {
}

/* Public functions */

void GPS_powerUpGPS(void) {
}

void GPS_powerDownGPS(void) {
}

void GPS_enableGPSInterface(void) {
}

void GPS_disableGPSInterface(void) {
}

void GPS_enableMagneticSwitch(void) {
}

void GPS_disableMagneticSwitch(void) {
}

bool GPS_isMagneticSwitchClosed(void) {
    return false;
}

GPS_fixResult_t GPS_setTimeFromGPS(uint32_t timeout) {
    (void)timeout;
    return GPS_TIMEOUT;
}

void GPS_cancelTimeSetting(GPS_fixCancellationReason_t reason) {
    (void)reason;
}

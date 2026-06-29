#!/usr/bin/env python3
"""Patch ESPBridge to use AudioMoth UART1 hardware on PB9/PB10.

The first bridge prototype bit-banged UART with TIMER1. That works only at very
low baud rates and cannot sustain the negotiated 1 Mbaud transfer mode. AudioMoth-Project
already routes the GPS UART through UART1 LOC2 with RX on PB10; LOC2 also gives
TX on PB9. This patch replaces the software-UART primitives with polled UART1
hardware while leaving the higher-level bridge protocol untouched.
"""

from __future__ import annotations

from pathlib import Path

BRIDGE_C = Path("project/src/espbridge.c")

BLOCK_START = "static void configureBridgePins(void) {"
BLOCK_END = "\n/* ---------------- CRC and validation ---------------- */"

NEW_UART_BLOCK = r'''#define UART_RX_POLL_US                    50

static uint32_t uartPollTicksPerMicrosecond = 1;

static void configureBridgePins(void) {
    CMU_ClockEnable(cmuClock_GPIO, true);
    GPIO_PinModeSet(BRIDGE_TX_PORT, BRIDGE_TX_PIN, gpioModePushPull, 1);
    GPIO_PinModeSet(BRIDGE_RX_PORT, BRIDGE_RX_PIN, gpioModeInputPull, 1);
}

static void startUartPollTimer(void) {
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
    uartPollTicksPerMicrosecond = (timerFrequency + 500000UL) / 1000000UL;
    if (uartPollTicksPerMicrosecond == 0) uartPollTicksPerMicrosecond = 1;
}

static void stopUartPollTimer(void) {
    TIMER_Enable(TIMER1, false);
    TIMER_Reset(TIMER1);
    CMU_ClockEnable(cmuClock_TIMER1, false);
    uartPollTicksPerMicrosecond = 1;
}

static void bridgeDelayMicroseconds(uint32_t microseconds) {
    uint32_t ticks = microseconds * uartPollTicksPerMicrosecond;
    uint16_t start = (uint16_t)TIMER_CounterGet(TIMER1);
    while ((uint16_t)(TIMER_CounterGet(TIMER1) - start) < ticks) {
    }
}

static void applyBridgeUartBaud(uint32_t baud) {
    NVIC_DisableIRQ(UART1_RX_IRQn);
    USART_IntDisable(BRIDGE_UART, UART_IF_RXDATAV);
    USART_IntClear(BRIDGE_UART, UART_IF_RXDATAV);
    NVIC_ClearPendingIRQ(UART1_RX_IRQn);

    CMU_ClockEnable(BRIDGE_UART_CLOCK, true);
    USART_Reset(BRIDGE_UART);

    USART_InitAsync_TypeDef uartInit = USART_INITASYNC_DEFAULT;
    uartInit.enable = usartDisable;
    uartInit.baudrate = baud;
    uartInit.oversampling = usartOVS16;

    USART_InitAsync(BRIDGE_UART, &uartInit);

    BRIDGE_UART->ROUTE = UART_ROUTE_TXPEN | UART_ROUTE_RXPEN | BRIDGE_UART_LOCATION;
    resetBridgeRxBuffer();
    USART_Enable(BRIDGE_UART, usartEnable);

    USART_IntClear(BRIDGE_UART, UART_IF_RXDATAV);
    USART_IntEnable(BRIDGE_UART, UART_IF_RXDATAV);
    NVIC_ClearPendingIRQ(UART1_RX_IRQn);
    NVIC_EnableIRQ(UART1_RX_IRQn);
}

static void configureBridgeUart(void) {
    configureBridgePins();
    startUartPollTimer();
    applyBridgeUartBaud(ESPBRIDGE_DEFAULT_BAUD);
}

static void bridgeSetBaud(uint32_t baud) {
    while ((BRIDGE_UART->STATUS & UART_STATUS_TXC) == 0) {
        WDOG_Feed();
    }
    applyBridgeUartBaud(baud);
}

static void stopBridgeUart(void) {
    NVIC_DisableIRQ(UART1_RX_IRQn);
    USART_IntDisable(BRIDGE_UART, UART_IF_RXDATAV);
    USART_IntClear(BRIDGE_UART, UART_IF_RXDATAV);
    NVIC_ClearPendingIRQ(UART1_RX_IRQn);
    USART_Reset(BRIDGE_UART);
    CMU_ClockEnable(BRIDGE_UART_CLOCK, false);
    resetBridgeRxBuffer();
    GPIO_PinModeSet(BRIDGE_TX_PORT, BRIDGE_TX_PIN, gpioModePushPull, 1);
    GPIO_PinModeSet(BRIDGE_RX_PORT, BRIDGE_RX_PIN, gpioModeInputPull, 1);
    stopUartPollTimer();
}

static void bridgeDelayMilliseconds(uint32_t milliseconds) {
    for (uint32_t i = 0; i < milliseconds; i += 1) {
        AudioMoth_delay(1);
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
    return bufferedRxAvailable();
}

static bool uartReadByte(uint8_t *byte) {
    return bufferedRxRead(byte);
}

static void uartWriteByte(uint8_t byte) {
    USART_Tx(BRIDGE_UART, byte);
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
    uint32_t elapsedUs = 0;
    uint32_t timeoutUs = timeoutMs * 1000UL;

    while (elapsedUs < timeoutUs && index < ESPBRIDGE_MAX_LINE - 1) {
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
            bridgeDelayMicroseconds(UART_RX_POLL_US);
            elapsedUs += UART_RX_POLL_US;
        }
    }

    lineBuffer[index] = 0;
    return false;
}
'''


def replace_once(text: str, old: str, new: str, label: str) -> tuple[str, bool]:
    if new in text:
        return text, False
    if old not in text:
        raise SystemExit(f"Could not find ESP bridge {label} text to patch")
    return text.replace(old, new, 1), True


def replace_uart_block(text: str) -> tuple[str, bool]:
    if "USART_InitAsync_TypeDef uartInit" in text and "stopBridgeUart" in text:
        return text, False

    start = text.find(BLOCK_START)
    if start < 0:
        raise SystemExit("Could not find ESP bridge UART block start")

    end = text.find(BLOCK_END, start)
    if end < 0:
        raise SystemExit("Could not find ESP bridge UART block end")

    return text[:start] + NEW_UART_BLOCK + text[end:], True


def main() -> None:
    text = BRIDGE_C.read_text(encoding="utf-8")
    text, block_changed = replace_uart_block(text)
    text, stop_changed = replace_once(text, "    stopSoftUartTimer();", "    stopBridgeUart();", "UART stop call")
    BRIDGE_C.write_text(text, encoding="utf-8")

    changed = []
    if block_changed:
        changed.append("hardware UART block")
    if stop_changed:
        changed.append("UART stop call")

    if changed:
        print("Applied ESP bridge hardware UART patch: " + ", ".join(changed))
    else:
        print("ESP bridge hardware UART patch already applied")


if __name__ == "__main__":
    main()

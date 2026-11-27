/*
 * File:    nrf24.h
 */

#ifndef NRF24_H
#define NRF24_H

#include <stdint.h>
#include "driver/gpio.h"
#include "driver/spi_master.h"

// [CONFIG] Hardware Pin Definitions
// Adjust these to match your ESP32 wiring
#define PIN_NUM_MISO 13
#define PIN_NUM_MOSI 11
#define PIN_NUM_CLK  12
#define PIN_NUM_CSN  10
#define PIN_NUM_CE   9

// --- Register Definitions (Standard, no need to change) ---
#define R_REGISTER    0x00
#define W_REGISTER    0x20
#define R_RX_PAYLOAD  0x61
#define W_TX_PAYLOAD  0xA0
#define FLUSH_TX      0xE1
#define FLUSH_RX      0xE2
#define NOP           0xFF

#define CONFIG      0x00
#define EN_AA       0x01
#define EN_RXADDR   0x02
#define SETUP_AW    0x03
#define SETUP_RETR  0x04
#define RF_CH       0x05
#define RF_SETUP    0x06
#define STATUS      0x07
#define TX_ADDR     0x10
#define RX_ADDR_P0  0x0A
#define FIFO_STATUS 0x17

// [CONFIG] Payload Size
// Must be the same on both TX and RX (Max 32)
#define PAYLOAD_WIDTH 32

void nrf24_init(void);
void nrf24_tx_mode(uint8_t *address, uint8_t channel);
uint8_t nrf24_transmit(uint8_t *data);
uint8_t nrf24_get_status(void);

#endif

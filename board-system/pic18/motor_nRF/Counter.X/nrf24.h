/*
 * File:    nrf24.h
 */

#ifndef NRF24_LIB_H
#define	NRF24_LIB_H

#include "mcc_generated_files/system/system.h"
#include "mcc_generated_files/spi/spi1.h"
#include "mcc_generated_files/system/pins.h"

// ---------------------------------------------------
// SPI Macros (MCC Melody)
// ---------------------------------------------------
#define SPI_WRITE_BYTE(dt)          SPI1_ByteExchange(dt)
#define SPI_READ_BYTE(dt)           SPI1_ByteExchange(dt)
#define SPI_INIT()                  SPI1_Open(HOST_CONFIG)

// ---------------------------------------------------
// [CONFIG] Hardware Pin Mapping
// Modify here if you change PCB layout
// ---------------------------------------------------
// CSN (Chip Select) -> RC0
#define NRF24L01_CSN_H()            IO_RC0_SetHigh()
#define NRF24L01_CSN_L()            IO_RC0_SetLow()
#define NRF24L01_CSN_SetOutput()    IO_RC0_SetDigitalOutput()

// CE (Chip Enable) -> RC1
#define NRF24L01_CE_H()             IO_RC1_SetHigh()
#define NRF24L01_CE_L()             IO_RC1_SetLow()
#define NRF24L01_CE_SetOutput()     IO_RC1_SetDigitalOutput()

// ---------------------------------------------------
// [CONFIG] Data Settings
// ---------------------------------------------------
#define PAYLOAD_BYTES 32 // Must match ESP32

// [CONFIG] Address (5 bytes). 
// Must match ESP32 TX Address perfectly.
// Note: If data is not received, try reversing the byte order on ESP32 side.
unsigned char ADDRESS_DATA_RXPIPE0[5] = {0xE8, 0xE8, 0xF0, 0xF0, 0xE1};
unsigned char ADDRESS_DATA_TXPIPE0[5] = {0xE8, 0xE8, 0xF0, 0xF0, 0xE1};

typedef enum{
    NRF24_INIT_OK = 0,
    NRF24_INIT_FAILED = 1
} NRF24_INIT_STATUS;

typedef enum {
    RX_MODE = 1,
    TX_MODE = 2
} NRF24_OPERATION_MODE;

// ---------------------------------------------------
// Registers (Standard)
// ---------------------------------------------------
#define R_REGISTER          0x00  
#define W_REGISTER          0x20  
#define R_RX_PAYLOAD        0x61  
#define W_TX_PAYLOAD        0xA0  
#define FLUSH_TX            0xE1  
#define FLUSH_RX            0xE2  
#define REUSE_TX_PL         0xE3  

#define NRF24_MEM_CONFIG              0x00  
#define NRF24_MEM_EN_AA               0x01  
#define NRF24_MEM_EN_RXADDR           0x02  
#define NRF24_MEM_SETUP_AW            0x03  
#define NRF24_MEM_SETUP_RETR          0x04  
#define NRF24_MEM_RF_CH               0x05  
#define NRF24_MEM_RF_SETUP            0x06  
#define NRF24_MEM_STATUSS             0x07  
#define NRF24_MEM_OBSERVE_TX          0x08  
#define NRF24_MEM_CD                  0x09  
#define NRF24_MEM_RX_ADDR_P0          0x0A  
#define NRF24_MEM_RX_ADDR_P1          0x0B  
#define NRF24_MEM_RX_ADDR_P2          0x0C  
#define NRF24_MEM_RX_ADDR_P3          0x0D  
#define NRF24_MEM_RX_ADDR_P4          0x0E  
#define NRF24_MEM_RX_ADDR_P5          0x0F  
#define NRF24_MEM_TX_ADDR             0x10  
#define NRF24_MEM_RX_PW_P0            0x11  
#define NRF24_MEM_RX_PW_P1            0x12  
#define NRF24_MEM_RX_PW_P2            0x13  
#define NRF24_MEM_RX_PW_P3            0x14  
#define NRF24_MEM_RX_PW_P4            0x15  
#define NRF24_MEM_RX_PW_P5            0x16  
#define NRF24_MEM_FIFO_STATUS         0x17
#define NRF24_MEM_CMD_NOP             0xFF

#define NRF24_MEM_REGISTER_MASK       0x1F

// ---------------------------------------------------
// Functions
// ---------------------------------------------------
// Low-level SPI register access
void nrf24_write_register(unsigned char mnemonic_addr, unsigned char value);
unsigned char nrf24_read_register(unsigned char mnemonic_addr);
void nrf24_write_buff(unsigned char mnemonic_addr, unsigned char *buffer, unsigned char bytes);
void nrf24_read_buff(unsigned char mnemonic_addr, unsigned char *buffer, unsigned char bytes);

// Initialization and mode control
NRF24_INIT_STATUS nrf24_rf_init(unsigned char mode, unsigned char rf_channel);
void nrf24_set_rf_mode(NRF24_OPERATION_MODE mode);

// Data transfer
void nrf24_send_rf_data(unsigned char *buffer);
unsigned char nrf24_is_rf_data_available(void); // 1 = data ready
void nrf24_read_rf_data(unsigned char *buffer);

// Channel and utility
void nrf24_set_channel_frq(unsigned char rf_channel); // 2400+channel MHz
unsigned char nrf24_get_channel_frq(void);
void nrf24_flush_tx_rx(void);

#endif
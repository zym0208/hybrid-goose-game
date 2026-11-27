/*
 * File:    nrf24.c
 */
#include "nrf24.h"

// ... (Standard register write/read functions omitted for brevity, no changes needed there) ...

void nrf24_write_register(unsigned char mnemonic_addr, unsigned char value) {
    NRF24L01_CSN_L();
    if (mnemonic_addr < W_REGISTER) {
        SPI_WRITE_BYTE(W_REGISTER | (mnemonic_addr & NRF24_MEM_REGISTER_MASK));
        SPI_WRITE_BYTE(value);
    } else {
        SPI_WRITE_BYTE(mnemonic_addr);
        if ((mnemonic_addr != FLUSH_TX) && (mnemonic_addr != FLUSH_RX) && \
            (mnemonic_addr != REUSE_TX_PL) && (mnemonic_addr != NRF24_MEM_CMD_NOP)) {
            SPI_WRITE_BYTE(value);
        }
    }
    NRF24L01_CSN_H();
    __delay_us(10); 
}

unsigned char nrf24_read_register(unsigned char mnemonic_addr) {
    unsigned char byte0;
    NRF24L01_CSN_L();
    SPI_WRITE_BYTE(R_REGISTER | (mnemonic_addr & NRF24_MEM_REGISTER_MASK) );
    byte0 = SPI_READ_BYTE(NRF24_MEM_CMD_NOP);
    NRF24L01_CSN_H();
    return byte0;
}

void nrf24_write_buff(unsigned char mnemonic_addr, unsigned char *buffer, unsigned char bytes) {
    unsigned char i;
    NRF24L01_CSN_L();
    SPI_WRITE_BYTE(W_REGISTER | mnemonic_addr);
    for (i = 0; i < bytes; i++) {
        SPI_WRITE_BYTE(*buffer);
        buffer++;
        __delay_us(10);
    }
    NRF24L01_CSN_H();
}

void nrf24_read_buff(unsigned char mnemonic_addr, unsigned char *buffer, unsigned char bytes) {
    unsigned char i;
    NRF24L01_CSN_L();
    SPI_WRITE_BYTE(R_REGISTER | mnemonic_addr);
    for (i = 0; i < bytes; i++) {
        *buffer = SPI_READ_BYTE(NRF24_MEM_CMD_NOP);
        buffer++;
    }
    *buffer = (unsigned char )0; // Fixed NULL warning
    NRF24L01_CSN_H();
}

NRF24_INIT_STATUS nrf24_rf_init(unsigned char mode, unsigned char rf_channel) {
    NRF24_INIT_STATUS stat = NRF24_INIT_FAILED;
    SPI_INIT();
    
    // Init Pins
    NRF24L01_CSN_SetOutput();
    NRF24L01_CE_SetOutput();
    NRF24L01_CSN_L();
    NRF24L01_CE_L();
    __delay_us(10);
    NRF24L01_CE_L();
    
    // [CONFIG] CRC Setting: 0x0B = 1 byte CRC. 
    // Note: ESP32 usually uses 2 bytes (0x0E/0x0F). If unstable, try changing 0x0B to 0x0F.
    nrf24_write_register(NRF24_MEM_CONFIG, 0x0B);
    
    // [CONFIG] Auto-Ack and Pipes
    nrf24_write_register(NRF24_MEM_EN_AA, 0x01);      // Enable Auto-Ack pipe 0
    nrf24_write_register(NRF24_MEM_EN_RXADDR, 0x01);  // Enable RX pipe 0
    nrf24_write_register(NRF24_MEM_SETUP_AW, 0x03);   // Address width: 5 bytes
    
    // Set Addresses
    nrf24_write_buff(NRF24_MEM_RX_ADDR_P0 , ADDRESS_DATA_RXPIPE0, 5);
    nrf24_write_buff(NRF24_MEM_TX_ADDR , ADDRESS_DATA_TXPIPE0, 5);
    
    // [CONFIG] Auto Retransmit: 500us delay, 10 retries
    nrf24_write_register(NRF24_MEM_SETUP_RETR, 0x1A); 
    
    nrf24_set_channel_frq(rf_channel);
    
    // [CONFIG] Data Rate & Power
    // 0x0F = 2Mbps, 0dBm (Current setting, matches ESP32)
    // 0x06 = 1Mbps, 0dBm 
    nrf24_write_register(NRF24_MEM_RF_SETUP, 0x0F); 
    
    nrf24_write_register(NRF24_MEM_RX_PW_P0, PAYLOAD_BYTES);
        
    __delay_us(10);
    nrf24_set_rf_mode(mode);
    __delay_us(10);
    
    if ((nrf24_read_register(NRF24_MEM_CONFIG) & 0x08) != 0){
      stat = NRF24_INIT_OK;
    }
    __delay_us(100);
    return stat;
}

void nrf24_set_rf_mode(NRF24_OPERATION_MODE mode) {
    switch (mode) {
        case RX_MODE:
            // [CONFIG] RX Mode + Power Up + 2-byte CRC (0x0F). 
            // Ensure this CRC config matches 'nrf24_rf_init' logic if changed.
            nrf24_write_register(NRF24_MEM_CONFIG, 0x0F); 
            NRF24L01_CE_H(); // CE High = Listening
            break;
        case TX_MODE:
            nrf24_write_register(NRF24_MEM_CONFIG, 0x0E); // TX Mode
            NRF24L01_CE_L();
            break;
    }
}

void nrf24_send_rf_data(unsigned char *buffer) {
    nrf24_set_rf_mode(TX_MODE);
    nrf24_write_buff(W_TX_PAYLOAD, buffer, PAYLOAD_BYTES);
    NRF24L01_CE_H();
    __delay_ms(1);
    NRF24L01_CE_L();
}

unsigned char nrf24_is_rf_data_available(void) {
    // Check Bit 6 (RX_DR) of STATUS register
    if ((nrf24_read_register(NRF24_MEM_STATUSS) & 0x40) == 0x40) {
        return 1; // Data Ready
    }
    return 0; // No Data
}

void nrf24_read_rf_data(unsigned char *buffer) {
    nrf24_read_buff(R_RX_PAYLOAD, buffer, PAYLOAD_BYTES);
    nrf24_write_register(NRF24_MEM_STATUSS, 0x70); // Clear interrupts
    nrf24_flush_tx_rx();
}

void nrf24_set_channel_frq(unsigned char rf_channel) {
    nrf24_write_register(NRF24_MEM_RF_CH, rf_channel);
}

unsigned char nrf24_get_channel_frq(void) {
    return nrf24_read_register(NRF24_MEM_RF_CH);
}

void nrf24_flush_tx_rx(void) {
    NRF24L01_CSN_L();
    nrf24_write_register(NRF24_MEM_STATUSS, 0x70);
    __delay_ms(10);
    NRF24L01_CSN_H();
    
    NRF24L01_CSN_L();
    SPI_WRITE_BYTE(FLUSH_TX);
    __delay_ms(10);
    NRF24L01_CSN_H();
    
    NRF24L01_CSN_L();
    SPI_WRITE_BYTE(FLUSH_RX);
    __delay_ms(10);    
    NRF24L01_CSN_H();
}
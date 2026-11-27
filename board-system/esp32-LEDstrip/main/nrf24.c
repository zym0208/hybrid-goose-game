/*
 * File:    nrf24.c
 */

#include "nrf24.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <string.h>

static const char *TAG = "NRF24";
static spi_device_handle_t spi;

// ... (GPIO and SPI helper functions remain unchanged) ...
static void cs_select(void) { gpio_set_level(PIN_NUM_CSN, 0); }
static void cs_deselect(void) { gpio_set_level(PIN_NUM_CSN, 1); }
static void ce_enable(void) { gpio_set_level(PIN_NUM_CE, 1); }
static void ce_disable(void) { gpio_set_level(PIN_NUM_CE, 0); }

static uint8_t spi_transfer_byte(uint8_t data) {
    uint8_t rx_data = 0;
    spi_transaction_t t;
    memset(&t, 0, sizeof(t));
    t.length = 8;
    t.tx_buffer = &data;
    t.rx_buffer = &rx_data;
    spi_device_transmit(spi, &t);
    return rx_data;
}
// ... (Register R/W helpers remain unchanged) ...
static void nrf_write_reg(uint8_t reg, uint8_t value) {
    cs_select();
    spi_transfer_byte(W_REGISTER | (reg & 0x1F));
    spi_transfer_byte(value);
    cs_deselect();
}
static uint8_t nrf_read_reg(uint8_t reg) {
    uint8_t value;
    cs_select();
    spi_transfer_byte(R_REGISTER | (reg & 0x1F));
    value = spi_transfer_byte(NOP);
    cs_deselect();
    return value;
}
static void nrf_write_buf(uint8_t reg, uint8_t *buf, uint8_t len) {
    cs_select();
    spi_transfer_byte(W_REGISTER | (reg & 0x1F));
    for (int i = 0; i < len; i++) {
        spi_transfer_byte(buf[i]);
    }
    cs_deselect();
}
static void nrf_flush_tx(void) {
    cs_select();
    spi_transfer_byte(FLUSH_TX);
    cs_deselect();
}
static void nrf_flush_rx(void) {
    cs_select();
    spi_transfer_byte(FLUSH_RX);
    cs_deselect();
}

void nrf24_init(void) {
    esp_err_t ret;

    gpio_reset_pin(PIN_NUM_CSN);
    gpio_set_direction(PIN_NUM_CSN, GPIO_MODE_OUTPUT);
    gpio_set_level(PIN_NUM_CSN, 1);

    gpio_reset_pin(PIN_NUM_CE);
    gpio_set_direction(PIN_NUM_CE, GPIO_MODE_OUTPUT);
    gpio_set_level(PIN_NUM_CE, 0);

    spi_bus_config_t buscfg = {
        .miso_io_num = PIN_NUM_MISO,
        .mosi_io_num = PIN_NUM_MOSI,
        .sclk_io_num = PIN_NUM_CLK,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = 64,
    };

    spi_device_interface_config_t devcfg = {
        .clock_speed_hz = 1000000, // [CONFIG] SPI Clock: 1MHz (Safe for wiring), Max 10MHz.
        .mode = 0,                 
        .spics_io_num = -1,        
        .queue_size = 7,
    };

    ret = spi_bus_initialize(SPI2_HOST, &buscfg, SPI_DMA_CH_AUTO);
    ret = spi_bus_add_device(SPI2_HOST, &devcfg, &spi);
    ESP_ERROR_CHECK(ret);
    
    vTaskDelay(pdMS_TO_TICKS(100)); // Wait for nRF power-up

    ESP_LOGI(TAG, "NRF24 Hardware Initialized (1MHz)");
}

// config
void nrf24_tx_mode(uint8_t *address, uint8_t channel) {
    ce_disable();
    
    // [CONFIG] Address Width: 3 = 5 bytes (Default).
    nrf_write_reg(SETUP_AW, 0x03);
    
    nrf_write_buf(TX_ADDR, address, 5);    
    nrf_write_buf(RX_ADDR_P0, address, 5); // Required for Auto-ACK
    
    nrf_write_reg(RF_CH, channel);         // Set Channel
    
    // [CONFIG] RF Data Rate & Power
    // 0x0F = 2Mbps, 0dBm (Max Power)
    // 0x06 = 1Mbps, 0dBm
    // MUST match the Receiver setting!
    nrf_write_reg(RF_SETUP, 0x0f);         
    
    nrf_write_reg(EN_AA, 0x01);            // Enable Auto-Ack on Pipe 0
    nrf_write_reg(EN_RXADDR, 0x01);        // Enable RX on Pipe 0
    
    // [CONFIG] Auto Retransmit: 0x1A = 500us delay, 10 retries
    nrf_write_reg(SETUP_RETR, 0x1A);       
    
    nrf_flush_tx();
    nrf_flush_rx();
    
    // [CONFIG] 0x0E = CRC Enabled (2 bytes), Power Up, TX Mode
    nrf_write_reg(CONFIG, 0x0E);           
    
    vTaskDelay(pdMS_TO_TICKS(2)); // Wait for oscillator (1.5ms required)
}

uint8_t nrf24_transmit(uint8_t *data) {
    nrf_write_reg(STATUS, 0x70); // Clear interrupts

    ce_disable();
    cs_select();
    spi_transfer_byte(W_TX_PAYLOAD);
    for (int i = 0; i < PAYLOAD_WIDTH; i++) {
        spi_transfer_byte(data[i]);
    }
    cs_deselect();

    ce_enable(); // Start Transmission
    for(volatile int i=0; i<1000; i++); // Short delay pulse
    
    uint8_t status = 0;
    int timeout = 200; // [CONFIG] Timeout 200ms
    
    while (timeout > 0) {
        status = nrf_read_reg(STATUS);
        
        // Check if TX_DS (Success) or MAX_RT (Max Retries)
        if (status & 0x30) {
            break; 
        }
        
        vTaskDelay(pdMS_TO_TICKS(1));
        timeout--;
    }
    
    ce_disable();

    nrf_write_reg(STATUS, 0x70); // Clear flags
    
    if (status & 0x10) {
        nrf_flush_tx(); // Flush if max retries reached
    }

    if (status & 0x20) return 1; // Success (ACK)
    if (status & 0x10) return 0; // Failed (No ACK)
    return 2; // Timeout
}

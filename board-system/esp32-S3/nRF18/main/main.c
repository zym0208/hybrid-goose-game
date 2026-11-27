/*
 * main.c — ESP32-S3 TX node: sends motor commands to PIC via nRF24L01
 */

#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "nrf24.h"

static const char *TAG = "MAIN";

// TX address — must match the address configured on the PIC receiver exactly
uint8_t tx_address[5] = {0xE8, 0xE8, 0xF0, 0xF0, 0xE1};

void app_main(void)
{
    nrf24_init();
    nrf24_tx_mode(tx_address, 76); // Channel 76 = 2476 MHz

    ESP_LOGI(TAG, "System initialized. Preparing to send command 0x4A40...");

    uint8_t data[32];

    // Motor command encoded as 16-bit value (bit fields interpreted by PIC firmware)
    // uint16_t command_val = 0x4800; // 0100/1 000/00 -> 9,0
    uint16_t command_val = 0x4A40; // 0100/1 010/01 -> 9,9
    // uint16_t command_val = 0x0240; // 0000/0 010/01  -> 0,9

    while (1) {
        memset(data, 0, 32);

        // PIC reads: info = (bufferRX[0] << 8) | bufferRX[1] — send MSB first
        data[0] = (uint8_t)((command_val >> 8) & 0xFF);
        data[1] = (uint8_t)(command_val & 0xFF);

        ESP_LOGI(TAG, "Sending Command: 0x%04X (Waiting for ACK...)", command_val);

        // Blocks until hardware ACK from PIC or timeout
        uint8_t result = nrf24_transmit(data);

        if (result == 1) {
            ESP_LOGI(TAG, ">> TX Success! ACK received.");
            // Long delay prevents re-sending before the motor finishes moving
            vTaskDelay(pdMS_TO_TICKS(10000));

        } else if (result == 0) {
            // PIC may be busy executing the previous command — keep retrying
            ESP_LOGW(TAG, ">> TX Failed (No ACK). Retrying in 1s...");
            vTaskDelay(pdMS_TO_TICKS(1000));

        } else {
            ESP_LOGE(TAG, ">> Hardware Error / Timeout.");
            vTaskDelay(pdMS_TO_TICKS(1000));
        }
    }
}
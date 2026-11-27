#include <stdio.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "driver/gpio.h"
#include "esp_log.h"
/* 
#define MOTOR_GPIO GPIO_NUM_18

static const char *TAG = "DC_MOTOR";

void app_main(void)
{
    gpio_config_t io_conf = {
        .pin_bit_mask = (1ULL << MOTOR_GPIO),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE
    };
    gpio_config(&io_conf);

    ESP_LOGI(TAG, "Motor test started");

    while (1)
    {
        ESP_LOGI(TAG, "Motor ON");
        gpio_set_level(MOTOR_GPIO, 1);  
        vTaskDelay(pdMS_TO_TICKS(3000));
        ESP_LOGI(TAG, "Motor OFF");
        gpio_set_level(MOTOR_GPIO, 0);
        vTaskDelay(pdMS_TO_TICKS(8000));  
    }
   while(1){

   };
} */

#include <string.h>
#include "driver/uart.h"

#define UART_NUM UART_NUM_0
#define BUF_SIZE 128
#define MOTOR_GPIO GPIO_NUM_18
static const char *TAG = "DC_MOTOR";

void dice_motor_spin(void)
{
    ESP_LOGI(TAG, "Dice motor ON");
    gpio_set_level(MOTOR_GPIO_18, 1);
    vTaskDelay(pdMS_TO_TICKS(3000));
    gpio_set_level(MOTOR_GPIO_18, 0);
    ESP_LOGI(TAG, "Dice motor OFF");
}


void app_main(void)
{
    gpio_config_t io_conf = {
        .pin_bit_mask = (1ULL << MOTOR_GPIO),
        .mode = GPIO_MODE_OUTPUT
    };
    gpio_config(&io_conf);

    uart_driver_install(UART_NUM, BUF_SIZE * 2, 0, 0, NULL, 0);
    xTaskCreate(uart_task, "uart_task", 4096, NULL, 10, NULL);

    ESP_LOGI(TAG, "ESP32 ready for SPIN commands");
}

/*
 * File:    main.c
 */

#include "mcc_generated_files/system/system.h"
#include "mcc_generated_files/spi/spi1.h"
#include "nrf24.h"

// [CONFIG] Received data is stored here. Max size defined by PAYLOAD_BYTES.
unsigned char bufferRX[32];

void main(void)
{
    SYSTEM_Initialize();
    
    // Blink LED to indicate boot
    IO_RC2_Toggle(); __delay_ms(1000); IO_RC2_Toggle(); 

    SPI1_Open(0);

    // [CONFIG] Init RX Mode. 
    // Parameter 2 is RF Channel: 76 = 2476 MHz. (Range 0-125).
    // Must match ESP32 setting.
    if (nrf24_rf_init(RX_MODE, 76) != NRF24_INIT_OK)
    {
        // Init failed: Slow blink
        while (1) {
            IO_RC2_Toggle();
            __delay_ms(500);
        }
    }
    
    IO_RC2_SetLow();

    while (1)
    {
        // Check if data received
        if (nrf24_is_rf_data_available())
        {
            // Read data into bufferRX
            nrf24_read_rf_data(bufferRX);
            
            // [DEBUG] Toggle LED to indicate successful packet reception
            IO_RC2_Toggle(); 
            
            // You can process 'bufferRX' here (e.g., parse "MSG-1")
            
            __delay_ms(10);
        }
    }
}
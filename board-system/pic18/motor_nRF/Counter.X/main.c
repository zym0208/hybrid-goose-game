// PIC18 motor controller — receives (x,y) target via nRF24L01+,
// drives a stepper along a perimeter path (bottom->right->top->left).
#include "mcc_generated_files/system/system.h"
#include "mcc_generated_files/spi/spi1.h"
#include "nrf24.h"
#include <xc.h>
#include <stdbool.h>

// Pin assignments
#define MAGNET     LATBbits.LATB0
#define STEP_LAT   LATBbits.LATB5
#define DIR_LAT_Y  LATBbits.LATB4
#define EN_LAT     LATBbits.LATB3
#define DIR_LAT_X  LATBbits.LATB6

// Global variables
volatile uint8_t count = 0;
volatile uint8_t zeroing = 0;
volatile uint16_t info_ack = 0;
//volatile uint16_t info = 0x4A40;
volatile uint16_t info = 0xFFFF;
volatile uint8_t new_data = 0;
volatile uint8_t x_place = 0;    // Initial X position
volatile uint8_t y_place = 0;    // Initial Y position
bool back_x = false;
bool back_y = false;
volatile bool nRF_time_flag = false;

// Movement variables
volatile int16_t x_move = 0;
volatile int16_t y_move = 0;
volatile uint8_t motor_active = 0;

// Toggle flags for direction changes (corner logic)
volatile bool twist_to_y = false;
volatile bool twist_to_x = false;
volatile bool moving_x = false;
volatile bool moving_y = false;
volatile bool end_game_done = false;

// nRF24 RX buffer
unsigned char bufferRX[32];

typedef struct {
    uint8_t x_cord;
    uint8_t y_cord;
//    uint8_t place;
//    uint8_t ID;
//    uint8_t request;
//    uint8_t ack;
} DataPacket;

DataPacket packet;

// Check for incoming nRF24 data
bool get_nRF_data(void)
{
    if (nrf24_is_rf_data_available()) {
        nrf24_read_rf_data(bufferRX);
        info = ((uint16_t)bufferRX[0] << 8) | bufferRX[1];
        return true;
    }
    return false;
}

// Parse the received 16-bit integer into the packet struct
void read_data(volatile uint16_t *info) 
{
    uint16_t data = *info;
    packet.x_cord = (data >> 11) & 0x1F;   // 5 bits
    packet.y_cord = (data >> 6)  & 0x1F;   // 5 bits
//    packet.place  = (data >> 5) & 0x01;
//    packet.ID     = (data >> 4) & 0x01;
//    packet.request= (data >> 3) & 0x01;
//    packet.ack    = (data >> 2) & 0x01;
}

// Stop all motion: halt timers, disable driver and magnet.
void Terminate(void)
{
    TMR0_Stop();
    EN_LAT = 1;  // Disable driver
    MAGNET = 0;
    x_move = 0;
    y_move = 0;
}

// Logic to calculate movement delta and direction
void move(void)
{
    back_x = false;
    back_y = false;
    
    // Reset toggle flags
    twist_to_y = false;
    twist_to_x = false;
    moving_x = false;
    moving_y = false;
    
    // Calculate difference in X direction
    if(packet.x_cord != x_place) {
        moving_x = true;
        if((int16_t)packet.x_cord > (int16_t)x_place) {
            x_move = (int16_t)packet.x_cord - (int16_t)x_place; 
            back_x = false;
        } else {
            x_move = (int16_t)x_place - (int16_t)packet.x_cord; 
            back_x = true;
        }
    } else {
        x_move = 0;
    }

    // Calculate difference in Y direction
    if(packet.y_cord != y_place) {
        moving_y = true;
        if((int16_t)packet.y_cord > (int16_t)y_place) {
            y_move = (int16_t)packet.y_cord - (int16_t)y_place; 
            back_y = false;
        } else {
            y_move = (int16_t)y_place - (int16_t)packet.y_cord; 
            back_y = true;
        }
    } else {
        y_move = 0;
    }

    // Determine corner transition logic based on current position
    twist_to_y = false;
    twist_to_x = false;

    // Bottom row (y=0, x: 0-9) - FIRST X, THEN Y
    if(y_place == 0 && x_place < 10) {
        if(!back_x) {
            // Move right to x=9, then up (y)
            if(x_move >= (9 - x_place)) {
                twist_to_y = true;
                y_move += x_move - (9 - x_place);
                x_move = 9 - x_place;
                back_y = false;  // Remainder goes up
            }
        } else {
            // Move left to x=0, ignore remainder
            if(x_move > x_place) {
                x_move = x_place;
            }
            y_move = 0;  // Ignore remainder
        }
    }
    // Right column (x=9, y: 0-9) - FIRST Y, THEN X
    else if(x_place == 9 && y_place < 10) {
        if(!back_y) {
            // Move up to y=9, remainder left (x)
            if(y_move >= (9 - y_place)) {
                twist_to_x = true;
                x_move += y_move - (9 - y_place);
                y_move = 9 - y_place;
                back_x = true;  // Remainder goes left
            }
        } else {
            // Move down to y=0, remainder left (x)
            if(y_move >= y_place) {
                twist_to_x = true;
                x_move += y_move - y_place;
                y_move = y_place;
                back_x = true;  // Remainder goes left
            }
        }
    }
    // Top row (y=9, x: 0-9) - FIRST X, THEN Y
    else if(y_place == 9 && x_place < 10) {
        if(back_x) {
            // Move left to x=0, remainder down (y)
            if(x_move >= x_place) {
                twist_to_y = true;
                y_move += x_move - x_place;
                x_move = x_place;
                back_y = true;  // Remainder goes down
            }
        } else {
            // Move right to x=9, remainder down (y)
            if(x_move >= (9 - x_place)) {
                twist_to_y = true;
                y_move += x_move - (9 - x_place);
                x_move = 9 - x_place;
                back_y = true;  // Remainder goes down
            }
        }
    }
    // Left column (x=0, y: 1-8) - FIRST Y, THEN X
    else if(x_place == 0 && y_place > 0 && y_place < 9) {
        if(!back_y) {
            // Move up to y=9, remainder right (x)
            if(y_move >= (9 - y_place)) {
                twist_to_x = true;
                x_move += y_move - (9 - y_place);
                y_move = 9 - y_place;
                back_x = false;  // Remainder goes right
            }
        } else {
            // Move down to y=0, remainder right (x)
            if(y_move >= y_place) {
                twist_to_x = true;
                x_move += y_move - y_place;
                y_move = y_place;
                back_x = false;  // Remainder goes right
            }
        }
    }

    // Set initial direction based on CURRENT position and required movement
    if (y_place == 0 && x_place < 10) {
        // Bottom row - Start with X movement
        if(!back_x) {
            DIR_LAT_X = 1;
            DIR_LAT_Y = 1;
        } else {
            DIR_LAT_X = 0;
            DIR_LAT_Y = 0;
        }
    }
    else if (x_place == 9 && y_place < 10) {
        // Right column - Start with Y movement
        if(!back_y) {
            DIR_LAT_X = 1;
            DIR_LAT_Y = 0;
        } else {
            DIR_LAT_X = 0;
            DIR_LAT_Y = 1;
        }
    }
    else if (y_place == 9 && x_place < 10) {
        // Top row - Start with X movement
        if(back_x) {
            DIR_LAT_X = 0;
            DIR_LAT_Y = 0;
        } else {
            DIR_LAT_X = 1;
            DIR_LAT_Y = 1;
        }
    }
    else if (x_place == 0 && y_place > 0 && y_place < 9) {
        // Left column - Start with Y movement
        if(!back_y) {
            DIR_LAT_X = 1;
            DIR_LAT_Y = 0;
        } else {
            DIR_LAT_X = 0;
            DIR_LAT_Y = 1;
        }
    }

    // Start movement if there are steps to perform
    if((x_move != 0 || y_move != 0) && motor_active != 1) {
        motor_active = 1;
        EN_LAT = 0;
        MAGNET = 1;
        TMR0_Start();
        TMR1_Start();
        TMR2_Stop();
    }
}

// TMR0 callback: counts steps and handles corner turning logic
void calculating_step(void)
{
    // BOTTOM ROW or TOP ROW: First X, then Y
    if((y_place == 0 || y_place == 9) && x_place < 10) {
        // Perform X movement first
        if(x_move > 0) {
            x_move--;
            
            // Update x_place based on direction
            if(back_x) {
                x_place--;
            } else {
                x_place++;
            }
            
            // Check if X is done and we need to twist to Y
            if(x_move == 0 && y_move > 0 && twist_to_y) {
                // Adjust direction for Y movement based on corner
                if(x_place == 9 && y_place == 0) {
                    // Bottom row to Right column (Up)    THIS IS ALREADY RIGHT THE REST IS WRONG OF THE FINITE STATE MACHINE
                    DIR_LAT_X = 1;
                    DIR_LAT_Y = 0;
                } 
                else if(x_place == 0 && y_place == 9) {
                    // Top row to Left column (Down)
                    DIR_LAT_X = 0;
                    DIR_LAT_Y = 1;
                }
                else if(x_place == 9 && y_place == 9) {
                    // Top row to Right column (Down)
                    DIR_LAT_X = 0;
                    DIR_LAT_Y = 1;
                }
            }
        }
        // Perform Y movement if X is done
        else if(y_move > 0) {
            y_move--;
            
            // Update y_place based on direction
            if(back_y) {
                y_place--;
            } else {
                y_place++;
            }
        }
    }
    // LEFT COLUMN or RIGHT COLUMN: First Y, then X
    else if((x_place == 0 || x_place == 9) && y_place < 10) {
        // Perform Y movement first
        if(y_move > 0) {
            y_move--;
            
            // Update y_place based on direction
            if(back_y) {
                y_place--;
            } else {
                y_place++;
            }
            
            // Check if Y is done and we need to twist to X
            if(y_move == 0 && x_move > 0 && twist_to_x) {
                // Adjust direction for X movement based on corner
                if(y_place == 9 && x_place == 9) {
                    // Right column to Top row (Left)
                    DIR_LAT_X = 0;
                    DIR_LAT_Y = 0;
                }
                else if(y_place == 0 && x_place == 9) {
                    // Right column to Bottom row (Left)
                    DIR_LAT_X = 0;
                    DIR_LAT_Y = 0;
                }
                else if(y_place == 9 && x_place == 0) {
                    // Left column to Top row (Right)
                    DIR_LAT_X = 1;
                    DIR_LAT_Y = 1;
                }
                else if(y_place == 0 && x_place == 0) {
                    // Left column to Bottom row (Right)
                    DIR_LAT_X = 1;
                    DIR_LAT_Y = 1;
                }
            }
        }
        // Perform X movement if Y is done
        else if(x_move > 0) {
            x_move--;
            
            // Update x_place based on direction
            if(back_x) {
                x_place--;
            } else {
                x_place++;
            }
        }
    }
    
    // Stop everything when all moves are done
    if(x_move == 0 && y_move == 0) {
        // Check for "End Game" position: x=0, y=2 (run once)
        if(x_place == 0 && y_place == 2 && !end_game_done) {
            // Auto-move 2 steps to the right
            x_move = 2;
            back_x = false;
            DIR_LAT_X = 1;
            DIR_LAT_Y = 1;
            end_game_done = true;  // Mark done to avoid infinite loop
            // Keep motor active
        } else {
            TMR0_Stop();
            TMR1_Stop();
            TMR2_Start();
            EN_LAT = 1;  // Disable motor driver
            MAGNET = 0;
            motor_active = 0; // Release lock
            
            // Update final position state if needed
            x_place = packet.x_cord;
            y_place = packet.y_cord;
        }
    }
}

// TMR1 callback: Generates physical step pulses
void Step(void)
{
    if(count < 2) {
        STEP_LAT = 0;
        count++;
    } else {
        STEP_LAT = 1;
        count = 0;
    }
}

// Home the stepper to origin using limit switches: RB1 for Y-axis, RB2 for X-axis.
void Zero_motors(void)
{
    motor_active = 1;
    zeroing = 0;
    TMR1_Start();
    EN_LAT = 0;
    MAGNET = 1;
    
    // 0,1 -> down
    DIR_LAT_X = 0;
    DIR_LAT_Y = 1;
    while (PORTBbits.RB1 == 1) {}
    
    // 1,0 -> up
//    DIR_LAT_X = 1;
//    DIR_LAT_Y = 0;
//    while (PORTBbits.RB1 == 0) {}
    
    // 0,0 -> left
    DIR_LAT_X = 0;
    DIR_LAT_Y = 0;      
    while (PORTBbits.RB2 == 1) {}
    
    // 1,1 -> right
//    DIR_LAT_X = 1;
//    DIR_LAT_Y = 1;      
//    while (PORTBbits.RB2 == 0) {}
    
    EN_LAT = 1;
    MAGNET = 0;
    TMR1_Stop();
    TMR2_Start();
    motor_active = 0;
}

// TMR2 callback: signals main loop to poll nRF24 for new data.
void Timer_nRF(void)
{
    nRF_time_flag = true;
}

int main(void)
{    
    SYSTEM_Initialize();
    
    SPI1_Open(0);
    
    // Enable Global Interrupts
    INTERRUPT_GlobalInterruptHighEnable(); 
    INTERRUPT_GlobalInterruptLowEnable();
    
    TMR0_OverflowCallbackRegister(calculating_step);
    TMR1_OverflowCallbackRegister(Step);
    TMR2_OverflowCallbackRegister(Timer_nRF);
    
    TMR0_Stop();
    TMR1_Stop();
    TMR2_Start();
    
    // Setup pins
    ANSELA = 0;
    ANSELB = 0;
    zeroing = 1;
    
    // Initialize nRF24L01+ (RX Mode, Channel 76)
    if(nrf24_rf_init(RX_MODE, 76) != NRF24_INIT_OK) {}
    
    Zero_motors();
    
    while(1)
    {
         // 1. Check for nRF24 data ONLY if motors are NOT moving
        // This prevents data corruption during movement
        if(motor_active == 0 && nRF_time_flag == true) 
        {
            info = 0xFFFF;
            nRF_time_flag = false;
            if(get_nRF_data()) {
                if(info != 0xFFFF) {
                    new_data = 1;
                }
            }
        }
        
        // 2. Process new data if received
        if(new_data == 1)
        {
            new_data = 0;     // Clear flag immediately
            read_data(&info); // Parse raw data into packet struct
            move();           // Start movement logic (sets motor_active = 1)
        }
        
        
//        // Process nieuwe data
//        if (new_data == 0 && motor_active == 0)
//        {
//            Terminate();
//        }
        
//        if(new_data == 0 && motor_active == 0)
//        {
//            Terminate();
//        }
        
//        // 3. Handle Buttons / Limits (Example logic)
//        if (PORTBbits.RB1 == 0)
//        {
//            // Zero_motors(); // Optional homing
//            EN_LAT = 1;       // Emergency Stop / Disable
//        }
    }
}

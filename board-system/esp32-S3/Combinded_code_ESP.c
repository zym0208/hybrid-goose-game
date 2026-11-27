#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "driver/uart.h"
#include "driver/spi_master.h"
#include "esp_log.h"

#define BYTE_TO_BINARY_PATTERN "%c%c%c%c%c%c%c%c"
#define BYTE_TO_BINARY(byte)  \
  (byte & 0x80 ? '1' : '0'), \
  (byte & 0x40 ? '1' : '0'), \
  (byte & 0x20 ? '1' : '0'), \
  (byte & 0x10 ? '1' : '0'), \
  (byte & 0x08 ? '1' : '0'), \
  (byte & 0x04 ? '1' : '0'), \
  (byte & 0x02 ? '1' : '0'), \
  (byte & 0x01 ? '1' : '0')

// CONFIGURATION 
#define MAX_PLAYERS 4
#define MAX_ROBOT_PLAYERS 1  // Easy to change later!
#define MIN_PLAYERS 2
#define TOTAL_TILES 36
#define LED_COUNT 36

// GPIO pins
#define DC_MOTOR_PIN 25
#define BUTTON_PIN 26
#define CAMERA_TRIGGER_PIN 27

// UART for communication with companion app
#define UART_APP 2

// NRF24L01 SPI pins (connected to ESP32 via SPI)
#define NRF_CE_PIN 4
#define NRF_CSN_PIN 5
#define NRF_MOSI_PIN 23
#define NRF_MISO_PIN 19
#define NRF_SCK_PIN 18

// Special tiles
#define BRIDGE_TILE 6
#define INN_TILE 19
#define WELL_TILE 31

static const char *TAG = "BOARDGAME";

//  DATA STRUCTURES 
typedef enum {
    TILE_NORMAL,
    TILE_BRIDGE,
    TILE_INN,
    TILE_WELL
} TileType;

typedef enum {
    PLAYER_TYPE_ROBOT,
    PLAYER_TYPE_PHYSICAL
} PlayerType;

typedef struct {
    int id;
    int position;
    int color_r, color_g, color_b;
    bool is_active;
    bool has_won;
    PlayerType type;
    bool enabled;
} Player;

typedef struct {
    Player players[MAX_PLAYERS];
    int active_player;
    int last_dice_roll;
    int last_player;
    bool game_over;
    bool game_started;
    int winner;
    int num_players;
    int num_robot_players;
    int num_physical_players;
} GameState;

typedef struct {
    int x;
    int y;
} Coordinate;

//  GLOBAL VARIABLES 
GameState game;
bool physical_player_turn = false;
bool button_pressed = false;
spi_device_handle_t nrf_spi;

// TILE COORDINATE CONVERSION
Coordinate tile_to_xy(int tile) {
    Coordinate coord;
    
    if (tile < 1 || tile > 36) {
        ESP_LOGE(TAG, "Invalid tile number: %d", tile);
        coord.x = 0;
        coord.y = 0;
        return coord;
    }
    
    if (tile >= 1 && tile <= 10) {
        coord.x = tile - 1;
        coord.y = 0;
    }
    else if (tile >= 11 && tile <= 19) {
        coord.x = 9;
        coord.y = tile - 10;
    }
    else if (tile >= 20 && tile <= 28) {
        coord.x = 9 - (tile - 20);
        coord.y = 9;
    }
    else {
        coord.x = 0;
        coord.y = 9 - (tile - 28);
    }
    
    return coord;
}

// TILE INFORMATION 
TileType get_tile_type(int tile) {
    switch(tile) {
        case BRIDGE_TILE: return TILE_BRIDGE;
        case INN_TILE: return TILE_INN;
        case WELL_TILE: return TILE_WELL;
        default: return TILE_NORMAL;
    }
}

const char* get_tile_name(TileType type) {
    switch(type) {
        case TILE_BRIDGE: return "Bridge";
        case TILE_INN: return "Inn";
        case TILE_WELL: return "Well";
        default: return "Normal";
    }
}

int apply_tile_rules(int tile, int player_id) {
    TileType type = get_tile_type(tile);
    
    switch(type) {
        case TILE_BRIDGE:
            ESP_LOGI(TAG, "Player %d landed on Bridge - Move forward 2", player_id);
            return tile + 2;
        
        case TILE_INN:
            ESP_LOGI(TAG, "Player %d landed on Inn - Skip next turn", player_id);
            return tile;
        
        case TILE_WELL:
            ESP_LOGI(TAG, "Player %d landed on Well - Go back 3", player_id);
            return (tile - 3 > 0) ? tile - 3 : 1;
        
        default:
            return tile;
    }
}

//  HARDWARE FUNCTIONS 

void spin_dice_motor(void) {
    ESP_LOGI(TAG, "Spinning dice motor...");
    gpio_set_level(DC_MOTOR_PIN, 1);
    vTaskDelay(pdMS_TO_TICKS(500));
    gpio_set_level(DC_MOTOR_PIN, 0);
}

int read_dice_from_camera(void) {
    ESP_LOGI(TAG, "Triggering camera...");
    
    vTaskDelay(pdMS_TO_TICKS(1000));
    
    gpio_set_level(CAMERA_TRIGGER_PIN, 1);
    vTaskDelay(pdMS_TO_TICKS(100));
    gpio_set_level(CAMERA_TRIGGER_PIN, 0);
    
    int dice_value = (rand() % 6) + 1;
    
    ESP_LOGI(TAG, "Camera detected dice: %d", dice_value);
    return dice_value;
}

void IRAM_ATTR button_isr_handler(void* arg) {
    button_pressed = true;
}

// NRF24L01 SPI Communication Functions

// Send data via NRF24L01 to PIC
void nrf_send_data(uint8_t *data, size_t len) {
    spi_transaction_t trans;
    memset(&trans, 0, sizeof(trans));
    
    trans.length = len * 8;  // Length in bits
    trans.tx_buffer = data;
    
    // Pull CE low to start transmission
    gpio_set_level(NRF_CE_PIN, 0);
    vTaskDelay(pdMS_TO_TICKS(1));
    
    // Transmit via SPI
    esp_err_t ret = spi_device_transmit(nrf_spi, &trans);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "NRF SPI transmission failed");
    }
    
    // Pull CE high to complete transmission
    gpio_set_level(NRF_CE_PIN, 1);
    vTaskDelay(pdMS_TO_TICKS(1));
    gpio_set_level(NRF_CE_PIN, 0);
}

// COMMUNICATION FUNCTIONS 

void parse_config_message(const char* message) {
    int total = 0, robots = 0;
    
    if (sscanf(message, "CONFIG:{\"total\":%d,\"robots\":%d}", &total, &robots) == 2) {
        // Validation
        if (total < MIN_PLAYERS || total > MAX_PLAYERS) {
            ESP_LOGE(TAG, "Invalid total players: %d (must be %d-%d)", 
                     total, MIN_PLAYERS, MAX_PLAYERS);
            
            char error[128];
            snprintf(error, sizeof(error), 
                    "ERROR:{\"msg\":\"Total must be %d-%d\"}\n", 
                    MIN_PLAYERS, MAX_PLAYERS);
            uart_write_bytes(UART_APP, error, strlen(error));
            return;
        }
        
        if (robots < 0 || robots > MAX_ROBOT_PLAYERS) {
            ESP_LOGE(TAG, "Invalid robot count: %d (max %d)", robots, MAX_ROBOT_PLAYERS);
            
            char error[128];
            snprintf(error, sizeof(error), 
                    "ERROR:{\"msg\":\"Max %d robot(s)\"}\n", MAX_ROBOT_PLAYERS);
            uart_write_bytes(UART_APP, error, strlen(error));
            return;
        }
        
        if (robots > total) {
            ESP_LOGE(TAG, "Robots (%d) cannot exceed total players (%d)", robots, total);
            
            char error[128];
            snprintf(error, sizeof(error), 
                    "ERROR:{\"msg\":\"Robots > total players\"}\n");
            uart_write_bytes(UART_APP, error, strlen(error));
            return;
        }
        
        // Configuration is valid
        game.num_players = total;
        game.num_robot_players = robots;
        game.num_physical_players = total - robots;
        
        ESP_LOGI(TAG, "=== GAME CONFIGURATION ===");
        ESP_LOGI(TAG, "Total players: %d", total);
        ESP_LOGI(TAG, "Robot players: %d", robots);
        ESP_LOGI(TAG, "Physical players: %d", total - robots);
        
        // Enable and assign player types
        for (int i = 0; i < MAX_PLAYERS; i++) {
            if (i < total) {
                game.players[i].enabled = true;
                
                if (i < robots) {
                    game.players[i].type = PLAYER_TYPE_ROBOT;
                    ESP_LOGI(TAG, "Player %d: ROBOT (ENABLED)", i);
                } else {
                    game.players[i].type = PLAYER_TYPE_PHYSICAL;
                    ESP_LOGI(TAG, "Player %d: PHYSICAL (ENABLED)", i);
                }
            } else {
                game.players[i].enabled = false;
                ESP_LOGI(TAG, "Player %d: DISABLED", i);
            }
        }
        
        game.game_started = true;
        ESP_LOGI(TAG, "=========================");
        
        // Send acknowledgment
        char ack[128];
        snprintf(ack, sizeof(ack), 
                "ACK:{\"total\":%d,\"robots\":%d,\"physical\":%d}\n", 
                total, robots, total - robots);
        uart_write_bytes(UART_APP, ack, strlen(ack));
        
        // Send initial state
        send_state_to_app();
        
    } else {
        ESP_LOGE(TAG, "Failed to parse config message");
    }
}

void send_state_to_app(void) {
    char json_buffer[256];
    snprintf(json_buffer, sizeof(json_buffer),
        "POS:{\"p1\":%d,\"p2\":%d,\"p3\":%d,\"p4\":%d,\"active\":%d,\"last_player\":%d,\"dice\":%d,\"total\":%d}\n",
        game.players[0].enabled ? game.players[0].position : 0,
        game.players[1].enabled ? game.players[1].position : 0,
        game.players[2].enabled ? game.players[2].position : 0,
        game.players[3].enabled ? game.players[3].position : 0,
        game.active_player,
        game.last_player,
        game.last_dice_roll,
        game.num_players
    );
    
    uart_write_bytes(UART_APP, json_buffer, strlen(json_buffer));
    ESP_LOGI(TAG, "Sent to app: %s", json_buffer);
}

void send_win_to_app(int winner_id) {
    char json_buffer[64];
    snprintf(json_buffer, sizeof(json_buffer), "WIN:{\"winner\":%d}\n", winner_id);
    uart_write_bytes(UART_APP, json_buffer, strlen(json_buffer));
    ESP_LOGI(TAG, "Winner: Player %d", winner_id);
}

// Send movement to PIC via NRF24L01 (SPI)
void send_movement_to_pic(int player_id, int from_tile, int to_tile, int plaats, int status_req, int ack, int reset, int reserved) {
    // Check if player is enabled and is a robot
    if (!game.players[player_id].enabled) {
        ESP_LOGI(TAG, "Player %d is DISABLED - skipping transmission", player_id);
        return;
    }
    
    if (game.players[player_id].type != PLAYER_TYPE_ROBOT) {
        ESP_LOGI(TAG, "Player %d is PHYSICAL - skipping NRF transmission", player_id);
        return;
    }
    
    ESP_LOGI(TAG, "Player %d is ROBOT - sending movement to PIC via NRF24L01", player_id);
    
    Coordinate from_coord = tile_to_xy(from_tile);
    Coordinate to_coord = tile_to_xy(to_tile);
    
    ESP_LOGI(TAG, "Player %d: Tile %d (%d,%d) → Tile %d (%d,%d)", 
             player_id, 
             from_tile, from_coord.x, from_coord.y,
             to_tile, to_coord.x, to_coord.y);
    
    uint8_t x_5bit = (uint8_t)(to_coord.x & 0x1F);
    uint8_t y_5bit = (uint8_t)(to_coord.y & 0x1F);
    
    uint8_t id_1bit = (uint8_t)(player_id & 0x01);
    uint8_t plaats_1bit = (uint8_t)(plaats & 0x01);
    uint8_t status_req_1bit = (uint8_t)(status_req & 0x01);
    uint8_t ack_1bit = (uint8_t)(ack & 0x01);
    uint8_t reset_1bit = (uint8_t)(reset & 0x01);
    uint8_t reserved_1bit = (uint8_t)(reserved & 0x01);
    
    uint8_t byte1 = (x_5bit << 3) | ((y_5bit >> 2) & 0x07);
    
    uint8_t byte2 = 0;
    byte2 |= ((y_5bit & 0x03) << 6);
    byte2 |= (plaats_1bit << 5);
    byte2 |= (id_1bit << 4);
    byte2 |= (status_req_1bit << 3);
    byte2 |= (ack_1bit << 2);
    byte2 |= (reset_1bit << 1);
    byte2 |= (reserved_1bit & 0x01);
    
    ESP_LOGI(TAG, "Encoded message for ROBOT player %d:", player_id);
    ESP_LOGI(TAG, "  X=%d (5-bit: 0x%02X)", to_coord.x, x_5bit);
    ESP_LOGI(TAG, "  Y=%d (5-bit: 0x%02X)", to_coord.y, y_5bit);
    ESP_LOGI(TAG, "  Byte 1: 0x%02X (binary: "BYTE_TO_BINARY_PATTERN")", 
             byte1, BYTE_TO_BINARY(byte1));
    ESP_LOGI(TAG, "  Byte 2: 0x%02X (binary: "BYTE_TO_BINARY_PATTERN")", 
             byte2, BYTE_TO_BINARY(byte2));
    
    // Send via NRF24L01 (SPI)
    uint8_t message[2] = {byte1, byte2};
    nrf_send_data(message, 2);
    
    ESP_LOGI(TAG, "Message sent to PIC via NRF24L01: [0x%02X, 0x%02X]", byte1, byte2);
}

// LED FUNCTIONS 

void led_update_all_positions(void) {
    ESP_LOGI(TAG, "Updating LED positions");
}

void led_pulse_active_player(int player_id) {
    ESP_LOGI(TAG, "Pulsing LED for player %d", player_id);
}

void led_special_tile_animation(int tile, TileType type) {
    ESP_LOGI(TAG, "Special tile animation at %d (%s)", tile, get_tile_name(type));
}

void led_win_animation(int winner_id) {
    ESP_LOGI(TAG, "Playing win animation for player %d", winner_id);
    vTaskDelay(pdMS_TO_TICKS(500));
    vTaskDelay(pdMS_TO_TICKS(500));
    vTaskDelay(pdMS_TO_TICKS(500));
}

//  GAME LOGIC 

void init_game(void) {
    ESP_LOGI(TAG, "Initializing game (waiting for configuration)...");
    
    for(int i = 0; i < MAX_PLAYERS; i++) {
        game.players[i].id = i;
        game.players[i].position = 1;
        game.players[i].is_active = false;
        game.players[i].has_won = false;
        game.players[i].type = PLAYER_TYPE_ROBOT;
        game.players[i].enabled = false;
        
        if(i == 0) { game.players[i].color_r = 255; game.players[i].color_g = 0; game.players[i].color_b = 0; }
        else if(i == 1) { game.players[i].color_r = 0; game.players[i].color_g = 255; game.players[i].color_b = 0; }
        else if(i == 2) { game.players[i].color_r = 0; game.players[i].color_g = 0; game.players[i].color_b = 255; }
        else { game.players[i].color_r = 255; game.players[i].color_g = 255; game.players[i].color_b = 0; }
    }
    
    game.active_player = 0;
    game.players[0].is_active = true;
    game.last_dice_roll = 0;
    game.last_player = -1;
    game.game_over = false;
    game.game_started = false;
    game.winner = -1;
    game.num_players = 0;
    game.num_robot_players = 0;
    game.num_physical_players = 0;
    
    led_update_all_positions();
}

bool check_winner(int player_index) {
    if(game.players[player_index].position == TOTAL_TILES) {
        game.game_over = true;
        game.winner = player_index;
        game.players[player_index].has_won = true;
        return true;
    }
    return false;
}

void move_player(int player_index, int dice_roll) {
    Player *p = &game.players[player_index];
    int old_pos = p->position;
    int new_pos = old_pos + dice_roll;
    
    if(new_pos > TOTAL_TILES) {
        ESP_LOGI(TAG, "Player %d rolled too high (%d), stays at %d", 
                 p->id, dice_roll, old_pos);
        return;
    }
    
    if(new_pos == TOTAL_TILES) {
        p->position = new_pos;
        ESP_LOGI(TAG, "Player %d reached tile 36 - WINNER!", p->id);
        check_winner(player_index);
        return;
    }
    
    p->position = new_pos;
    ESP_LOGI(TAG, "Player %d moved from %d to %d", p->id, old_pos, new_pos);
    
    int adjusted_pos = apply_tile_rules(new_pos, p->id);
    if(adjusted_pos != new_pos) {
        p->position = adjusted_pos;
        ESP_LOGI(TAG, "Position adjusted to %d after tile rules", adjusted_pos);
    }
    
    led_update_all_positions();
    
    TileType tile_type = get_tile_type(p->position);
    if(tile_type != TILE_NORMAL) {
        led_special_tile_animation(p->position, tile_type);
    }
    
    send_movement_to_pic(
        player_index,
        old_pos,
        p->position,
        0, 0, 0, 0, 0
    );
}

void next_player(void) {
    game.players[game.active_player].is_active = false;
    game.last_player = game.active_player;
    
    // Find next enabled player
    int attempts = 0;
    do {
        game.active_player = (game.active_player + 1) % MAX_PLAYERS;
        attempts++;
        
        if (attempts > MAX_PLAYERS) {
            ESP_LOGE(TAG, "ERROR: No enabled players found!");
            return;
        }
    } while (!game.players[game.active_player].enabled);
    
    game.players[game.active_player].is_active = true;
    
    ESP_LOGI(TAG, "Next player: %d (%s)", 
             game.active_player, 
             game.players[game.active_player].type == PLAYER_TYPE_ROBOT ? "ROBOT" : "PHYSICAL");
    
    led_pulse_active_player(game.active_player);
    
    if(game.players[game.active_player].type == PLAYER_TYPE_PHYSICAL) {
        physical_player_turn = true;
        ESP_LOGI(TAG, "Waiting for physical player to press button...");
    }
}

void execute_turn(int player_idx) {
    if(game.game_over) return;
    
    ESP_LOGI(TAG, "=== Player %d (%s) turn ===", 
             player_idx,
             game.players[player_idx].type == PLAYER_TYPE_ROBOT ? "ROBOT" : "PHYSICAL");
    
    spin_dice_motor();
    
    int dice = read_dice_from_camera();
    game.last_dice_roll = dice;
    
    move_player(player_idx, dice);
    
    send_state_to_app();
    
    if(check_winner(player_idx)) {
        send_win_to_app(game.players[player_idx].id);
        led_win_animation(player_idx);
        return;
    }
    
    next_player();
}

void uart_receive_task(void *pvParameters) {
    uint8_t data[128];
    
    while(1) {
        int len = uart_read_bytes(UART_APP, data, sizeof(data) - 1, pdMS_TO_TICKS(100));
        if(len > 0) {
            data[len] = '\0';
            ESP_LOGI(TAG, "Received from app: %s", (char*)data);
            
            if(strncmp((char*)data, "CONFIG:", 7) == 0) {
                parse_config_message((char*)data);
            }
        }
        vTaskDelay(pdMS_TO_TICKS(50));
    }
}

// MAIN GAME LOOP 

void game_task(void *pvParameters) {
    init_game();
    
    ESP_LOGI(TAG, "=== WAITING FOR CONFIGURATION ===");
    ESP_LOGI(TAG, "Send: CONFIG:{\"total\":X,\"robots\":Y}");
    ESP_LOGI(TAG, "  - total: %d-%d players", MIN_PLAYERS, MAX_PLAYERS);
    ESP_LOGI(TAG, "  - robots: 0-%d robot(s)", MAX_ROBOT_PLAYERS);
    ESP_LOGI(TAG, "Example: CONFIG:{\"total\":2,\"robots\":1}");
    ESP_LOGI(TAG, "=================================");
    
    while(1) {
        if(!game.game_started) {
            vTaskDelay(pdMS_TO_TICKS(500));
            continue;
        }
        
        if(game.game_over) {
            ESP_LOGI(TAG, "Game over! Winner: Player %d", game.winner);
            vTaskDelay(pdMS_TO_TICKS(5000));
            continue;
        }
        
        if(physical_player_turn) {
            if(button_pressed) {
                button_pressed = false;
                physical_player_turn = false;
                
                execute_turn(game.active_player);
            }
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }
        
        execute_turn(game.active_player);
        
        vTaskDelay(pdMS_TO_TICKS(2000));
    }
}

// INITIALIZATION

void init_nrf24l01_spi(void) {
    ESP_LOGI(TAG, "Initializing NRF24L01 SPI...");
    
    // SPI bus configuration
    spi_bus_config_t buscfg = {
        .mosi_io_num = NRF_MOSI_PIN,
        .miso_io_num = NRF_MISO_PIN,
        .sclk_io_num = NRF_SCK_PIN,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = 32
    };
    
    // Initialize SPI bus (using VSPI/SPI3_HOST)
    esp_err_t ret = spi_bus_initialize(SPI3_HOST, &buscfg, SPI_DMA_CH_AUTO);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to initialize SPI bus");
        return;
    }
    
    // NRF24L01 device configuration
    spi_device_interface_config_t devcfg = {
        .clock_speed_hz = 4 * 1000 * 1000,  // 4 MHz (NRF max is 8MHz)
        .mode = 0,                           // SPI mode 0
        .spics_io_num = NRF_CSN_PIN,        // CS pin
        .queue_size = 7,
        .flags = 0,
    };
    
    // Add device to SPI bus
    ret = spi_bus_add_device(SPI3_HOST, &devcfg, &nrf_spi);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to add NRF device to SPI bus");
        return;
    }
    
    // Configure CE pin as output
    gpio_config_t io_conf = {
        .pin_bit_mask = (1ULL << NRF_CE_PIN),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE
    };
    gpio_config(&io_conf);
    gpio_set_level(NRF_CE_PIN, 0);
    
    ESP_LOGI(TAG, "NRF24L01 SPI initialized");
    ESP_LOGI(TAG, "  MOSI: GPIO%d", NRF_MOSI_PIN);
    ESP_LOGI(TAG, "  MISO: GPIO%d", NRF_MISO_PIN);
    ESP_LOGI(TAG, "  SCK:  GPIO%d", NRF_SCK_PIN);
    ESP_LOGI(TAG, "  CSN:  GPIO%d", NRF_CSN_PIN);
    ESP_LOGI(TAG, "  CE:   GPIO%d", NRF_CE_PIN);
}

void init_hardware(void) {
    // GPIO init for motor and camera
    gpio_config_t io_conf = {
        .pin_bit_mask = (1ULL << DC_MOTOR_PIN) | (1ULL << CAMERA_TRIGGER_PIN),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE
    };
    gpio_config(&io_conf);
    
    // Button init with interrupt
    gpio_config_t btn_conf = {
        .pin_bit_mask = (1ULL << BUTTON_PIN),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .intr_type = GPIO_INTR_NEGEDGE
    };
    gpio_config(&btn_conf);
    gpio_install_isr_service(0);
    gpio_isr_handler_add(BUTTON_PIN, button_isr_handler, NULL);
    
    // UART config for companion app
    uart_config_t uart_config = {
        .baud_rate = 115200,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE
    };
    uart_param_config(UART_APP, &uart_config);
    uart_driver_install(UART_APP, 1024, 1024, 0, NULL, 0);
    
    // Initialize NRF24L01 SPI
    init_nrf24l01_spi();
    
    ESP_LOGI(TAG, "Hardware initialized");
}

void app_main(void) {
    ESP_LOGI(TAG, "Starting ESP32 Board Game");
    ESP_LOGI(TAG, "Communication:
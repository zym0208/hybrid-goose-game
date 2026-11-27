#include <stdio.h>
#include <inttypes.h>
#include <stdbool.h>
#include <string.h>
#include <stdlib.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "esp_system.h"
#include "esp_timer.h"
#include "esp_log.h"

#include "driver/gpio.h"
#include "driver/uart.h"

#include "led_strip.h"

#include "nrf24.h"

static const char *TAG = "NRF_SENDER";

// Bridge to the C++ MCP layer in main/mcp_server.cc
extern void game_notify_quiz_pending_mcp(int player, int timeout_s);
extern void game_notify_state_mcp(int p1, int p2, int p3, int p4, int total_players, int active, int last_player, int dice);

// ==== Hardware-config ====
#define LED_PIN       21
#define NUM_LEDS      88      // LED strip
#define NUM_TILES     36       // tiles 1..36
#define MAX_HUMAN_PLAYERS 3
#define MAX_PLAYERS   (1 + MAX_HUMAN_PLAYERS)
#define AI_PLAYER     1

#define MOTOR_GPIO GPIO_NUM_18
#define MOTOR_SPIN_TIME_MS 3000

// ==== UART / Serial input (DICE:n) ====
#define UART_PORT      UART_NUM_0
#define UART_RX_BUF    1024

static volatile int  pending_dice   = 0;
static volatile bool dice_available = false;
static int total_players = 2; // AI + 1 human by default

// ==== Quiz gate (tile 5) ====
#define QUIZ_TILE                5
#define QUIZ_PENALTY_BACK_STEPS  3

static volatile bool quiz_pending = false;
static volatile int  quiz_player = 0;
// 0: pending, 1: correct, -1: wrong/timeout
static volatile int  quiz_result = 0;
static volatile bool request_app_camera_confirm = false;

// ==== player positions (tile 1..36) ====
static int player_pos[MAX_PLAYERS + 1] = {0, 1, 1, 1, 1};

// 1..MAX_PLAYERS: who is currently active 
static int active_player = 1;

// Last "action" that went to host
static int last_dice_roll = 0;      // SKIP send 0
static int last_moved_player = 0;

// Multi-occupancy animation
static uint32_t multi_cycle_index = 0;
static int64_t  last_multi_switch_us = 0;

// For special tiles 
// Stores how many turns a player must skip
static int turns_to_skip[MAX_PLAYERS + 1] = {0, 0, 0, 0, 0};
// Stores if the next move must be backwards or not
static bool move_backwards_flag[MAX_PLAYERS + 1] = {false, false, false, false, false};

// LED-strip handle
static led_strip_handle_t strip = NULL;

// Forward declarations used by quiz flow.
static inline int next_player(int p);
static void auto_skip_if_needed(void);
static void send_position_to_pic(int linear_pos);
void dice_motor_spin(void);
static inline bool is_valid_player(int player);
static inline bool is_human_player(int player);
static inline void set_total_humans(int humans);

// =====================================================
// TILE -> LED MAPPING (1..36 -> physical LED 1..150)
// =====================================================
static const uint16_t tile_to_led[NUM_TILES + 1] = {
    0,    // index 0 unused (tiles zijn 1-based)

    // tile:  1   2   3   4   5   6   7   8   9  10  11  12
             6,  9,  11, 13, 14, 16, 18, 20, 22, 28, 30, 32,

    // tile: 13  14  15  16  17  18  19  20  21  22  23  24
            34, 36, 38, 39, 41, 43, 49, 52, 53, 55, 57, 59,

    // tile: 25  26  27  28  29  30  31  32  33  34  35  36
             61, 62, 64, 70, 73, 75, 77, 78, 80,82,84,87
};

// tile (1..36) -> led index (0..149)
static inline int tile_to_led_index(int tile_1_based) {
    if (tile_1_based < 1 || tile_1_based > NUM_TILES) return -1;

    int led_1_based = (int)tile_to_led[tile_1_based];
    if (led_1_based < 1 || led_1_based > NUM_LEDS) return -1;

    return led_1_based - 1; // to 0-based for led_strip_set_pixel
}


static void clear_strip_buffer(void) {
    if (!strip) return;
    for (int i = 0; i < NUM_LEDS; i++) {
        ESP_ERROR_CHECK(led_strip_set_pixel(strip, i, 0, 0, 0));
    }
}

// ==== Init LED-strip ====
static void init_ledstrip(void) {
    led_strip_config_t strip_config = {
        .strip_gpio_num = LED_PIN,
        .max_leds = NUM_LEDS,
        .led_model = LED_MODEL_WS2812,
        .color_component_format = LED_STRIP_COLOR_COMPONENT_FMT_GRB,
        .flags = { .invert_out = false },
    };

    led_strip_rmt_config_t rmt_config = {
        .clk_src = RMT_CLK_SRC_DEFAULT,
        .resolution_hz = 10 * 1000 * 1000, // 10 MHz
        .mem_block_symbols = 256,
        .flags = { .with_dma = false },
    };

    ESP_ERROR_CHECK(led_strip_new_rmt_device(&strip_config, &rmt_config, &strip));

    clear_strip_buffer();
    ESP_ERROR_CHECK(led_strip_refresh(strip));
}

// Helper: draw a TILE on the correct physical LED via mapping
static void set_tile_led(int tile_1_based, uint8_t r, uint8_t g, uint8_t b) {
    // standard LED
    int idx = tile_to_led_index(tile_1_based);
    if (idx >= 0) {
        ESP_ERROR_CHECK(led_strip_set_pixel(strip, idx, r, g, b));
    }

    // extra LEDs for corners
    switch (tile_1_based) {
        case 10:
            ESP_ERROR_CHECK(led_strip_set_pixel(strip, 25 - 1, r, g, b)); // LED25
            break;

        case 19:
            ESP_ERROR_CHECK(led_strip_set_pixel(strip, 46 - 1, r, g, b)); // LED46
            break;

        case 28:
            ESP_ERROR_CHECK(led_strip_set_pixel(strip, 67 - 1, r, g, b)); // LED67
            break;

        default:
            break;
    }
}

// ==== Helpers: color and position of a player ====
static void get_player_color(int player, uint8_t *r, uint8_t *g, uint8_t *b) {
    switch (player) {
        case 1: *r = 255; *g = 0;   *b = 0;   break; // red
        case 2: *r = 255; *g = 255; *b = 0;   break; // yellow
        case 3: *r = 0;   *g = 255; *b = 0;   break; // green
        case 4: *r = 0;   *g = 0;   *b = 255; break; // blue
        default: *r = 0; *g = 0; *b = 0;      break;
    }
}

static int get_player_pos(int player) {
    if (!is_valid_player(player)) return -1;
    return player_pos[player];
}

static inline void set_player_pos(int player, int pos) {
    if (!is_valid_player(player)) return;
    if (pos < 1) pos = 1;
    if (pos > NUM_TILES) pos = NUM_TILES;
    player_pos[player] = pos;
}

static inline bool is_valid_player(int player) {
    return (player >= 1 && player <= total_players);
}

static inline bool is_human_player(int player) {
    return is_valid_player(player) && player != AI_PLAYER;
}

static inline void set_total_humans(int humans) {
    if (humans < 1) humans = 1;
    if (humans > MAX_HUMAN_PLAYERS) humans = MAX_HUMAN_PLAYERS;
    total_players = 1 + humans;
    for (int p = 1; p <= MAX_PLAYERS; p++) {
        if (p > total_players) {
            turns_to_skip[p] = 0;
            move_backwards_flag[p] = false;
            player_pos[p] = 1;
        } else if (player_pos[p] < 1 || player_pos[p] > NUM_TILES) {
            player_pos[p] = 1;
        }
    }
    if (active_player > total_players) active_player = 1;
    ESP_LOGI(TAG, "Configured players: AI + %d humans (total=%d)", humans, total_players);
}

// =====================================================
// SPECIAL TILES -> color (empty square) + animation color
// =====================================================
static bool get_rule_special_tile_color(int tile, uint8_t *r, uint8_t *g, uint8_t *b) {
    const uint8_t GOOD_R = 144,   GOOD_G = 238,   GOOD_B = 144;
    const uint8_t BAD_R  = 255, BAD_G  = 100,   BAD_B  = 0;
    const uint8_t T14_R  = 128,  T14_G  = 0,  T14_B  = 128;

    switch (tile) {
        case 14:
            *r = T14_R; *g = T14_G; *b = T14_B;
            return true;

        // good
        case 6:
        case 10:
        case 19:
        case 26:
        case 31:
            *r = GOOD_R; *g = GOOD_G; *b = GOOD_B;
            return true;

        // bad
        case 4:
        case 16:
        case 22:
        case 28:
        case 35:
            *r = BAD_R; *g = BAD_G; *b = BAD_B;
            return true;

        default:
            return false;
    }
}

static inline bool is_rule_special_tile(int tile) {
    uint8_t r, g, b;
    return get_rule_special_tile_color(tile, &r, &g, &b);
}

// =====================================================
// DRAW (buffer only)
// =====================================================
static void drawPlayersToBuffer(void) {
    clear_strip_buffer();

    int64_t now_us = esp_timer_get_time();
    const int64_t SWITCH_INTERVAL_US = 2000000; // 2 s

    if (now_us - last_multi_switch_us >= SWITCH_INTERVAL_US) {
        multi_cycle_index++;
        last_multi_switch_us = now_us;
    }

    for (int tile = 1; tile <= NUM_TILES; tile++) {
        int players_here[MAX_PLAYERS];
        int count = 0;

        for (int p = 1; p <= total_players; p++) {
            if (get_player_pos(p) == tile) players_here[count++] = p;
        }

        if (count == 0) {
            uint8_t r, g, b;
            if (get_rule_special_tile_color(tile, &r, &g, &b)) {
                set_tile_led(tile, r, g, b);
            }
            continue;
        }

        int chosen_idx = 0;
        int active_index = -1;

        for (int i = 0; i < count; i++) {
            if (players_here[i] == active_player) {
                active_index = i;
                break;
            }
        }

        if (active_index != -1) {
            chosen_idx = active_index;
        } else if (count == 1) {
            chosen_idx = 0;
        } else {
            chosen_idx = multi_cycle_index % count;
        }

        uint8_t r, g, b;
        get_player_color(players_here[chosen_idx], &r, &g, &b);
        set_tile_led(tile, r, g, b);
    }
}

static void showPlayers(void) {
    drawPlayersToBuffer();
    ESP_ERROR_CHECK(led_strip_refresh(strip));
}

// ==== Active players "breathing" ====
static void pulseActivePlayer(int cycles) {
    int tile = get_player_pos(active_player);
    uint8_t baseR, baseG, baseB;
    get_player_color(active_player, &baseR, &baseG, &baseB);

    if (tile < 1 || tile > NUM_TILES) return;

    for (int c = 0; c < cycles; c++) {
        for (int br = 50; br <= 255; br += 20) {
            drawPlayersToBuffer();
            uint8_t r = (baseR * br) / 255;
            uint8_t g = (baseG * br) / 255;
            uint8_t b = (baseB * br) / 255;
            set_tile_led(tile, r, g, b);
            ESP_ERROR_CHECK(led_strip_refresh(strip));
            vTaskDelay(pdMS_TO_TICKS(30));
        }

        for (int br = 255; br >= 50; br -= 20) {
            drawPlayersToBuffer();
            uint8_t r = (baseR * br) / 255;
            uint8_t g = (baseG * br) / 255;
            uint8_t b = (baseB * br) / 255;
            set_tile_led(tile, r, g, b);
            ESP_ERROR_CHECK(led_strip_refresh(strip));
            vTaskDelay(pdMS_TO_TICKS(30));
        }
    }

    showPlayers();
}

// ==== Animation: run across strip ====
static void playRuleTileLoop(int start_tile, uint8_t r, uint8_t g, uint8_t b) {
    int start_index = tile_to_led_index(start_tile);
    if (start_index < 0) return;

    const int stepDelayMs = 40;

    for (int k = 0; k < NUM_LEDS; k++) {
        int idx = (start_index + k) % NUM_LEDS;

        clear_strip_buffer();
        ESP_ERROR_CHECK(led_strip_set_pixel(strip, idx, r, g, b));
        ESP_ERROR_CHECK(led_strip_refresh(strip));
        vTaskDelay(pdMS_TO_TICKS(stepDelayMs));
    }

    // short “landing”
    clear_strip_buffer();
    ESP_ERROR_CHECK(led_strip_set_pixel(strip, start_index, r, g, b));
    ESP_ERROR_CHECK(led_strip_refresh(strip));
    vTaskDelay(pdMS_TO_TICKS(150));

    showPlayers();
}

static void showRuleTileEffect(int tile) {
    uint8_t r, g, b;
    if (!get_rule_special_tile_color(tile, &r, &g, &b)) return;
    playRuleTileLoop(tile, r, g, b);
}

// === Wineffect ===
static void playWinEffect(uint8_t r, uint8_t g, uint8_t b) {
    const int rounds = 3;
    const int stepDelayMs = 50;

    for (int round = 0; round < rounds; round++) {
        for (int pos = 0; pos < NUM_LEDS; pos++) {
            clear_strip_buffer();
            ESP_ERROR_CHECK(led_strip_set_pixel(strip, pos, r, g, b));
            ESP_ERROR_CHECK(led_strip_refresh(strip));
            vTaskDelay(pdMS_TO_TICKS(stepDelayMs));
        }
    }

    showPlayers();
}

// ==== POS-json to host ====
// POS:{"p1":..,"p2":..,"p3":..,"p4":..,"players":..,"active":..,"last_player":..,"dice":..}
static void send_positions_to_host(void) {
    int p1 = get_player_pos(1);
    int p2 = is_valid_player(2) ? get_player_pos(2) : 1;
    int p3 = is_valid_player(3) ? get_player_pos(3) : 1;
    int p4 = is_valid_player(4) ? get_player_pos(4) : 1;

    printf("POS:{\"p1\":%d,\"p2\":%d,\"p3\":%d,\"p4\":%d,"
           "\"players\":%d,\"active\":%d,\"last_player\":%d,\"dice\":%d}\n",
           p1,
           p2,
           p3,
           p4,
           total_players,
           active_player,
           last_moved_player,
           last_dice_roll);
    fflush(stdout);

    // Push game-state updates to cloud-side agent as a proactive trigger.
    game_notify_state_mcp(
        p1,
        p2,
        p3,
        p4,
        total_players,
        active_player,
        last_moved_player,
        last_dice_roll
    );
}

static void send_winner_to_host(int winner_player) {
    printf("WIN:{\"winner\":%d}\n", winner_player);
    fflush(stdout);
}

static void reset_game_round(void) {
    for (int p = 1; p <= MAX_PLAYERS; p++) {
        player_pos[p] = 1;
        turns_to_skip[p] = 0;
        move_backwards_flag[p] = false;
    }

    active_player = AI_PLAYER;
    last_dice_roll = 0;
    last_moved_player = 0;
    multi_cycle_index = 0;
    last_multi_switch_us = esp_timer_get_time();

    quiz_pending = false;
    quiz_player = 0;
    quiz_result = 0;
    request_app_camera_confirm = false;

    ESP_LOGI(TAG, "Round reset: new game started.");
    send_positions_to_host();
    showPlayers();

    if (active_player == AI_PLAYER) {
        dice_motor_spin();
    }
}

static void send_quiz_state_to_host(const char *state, int player, int time_left_s) {
    printf("QUIZ:{\"state\":\"%s\",\"player\":%d,\"timeout\":%d}\n", state, player, time_left_s);
    fflush(stdout);
}

static void start_quiz_gate_for_player(int player) {
    if (!is_human_player(player)) {
        ESP_LOGW(TAG, "Ignore quiz gate for non-human player %d", player);
        return;
    }
    quiz_pending = true;
    quiz_player = player;
    quiz_result = 0;
    ESP_LOGI(TAG, "QUIZ pending for player %d on tile %d", player, QUIZ_TILE);
    send_quiz_state_to_host("pending", player, 0);
    game_notify_quiz_pending_mcp(player, 0);
}

static bool apply_quiz_resolution_if_ready(void) {
    if (!quiz_pending) return false;

    if (quiz_result == 0) {
        return false;
    }

    int player = quiz_player;
    if (!is_human_player(player)) {
        ESP_LOGW(TAG, "Ignore quiz resolution for non-human player %d", player);
        quiz_pending = false;
        quiz_player = 0;
        quiz_result = 0;
        return false;
    }

    int old_pos = get_player_pos(player);
    int final_pos = old_pos;
    bool correct = (quiz_result == 1);

    if (!correct) {
        final_pos = old_pos - QUIZ_PENALTY_BACK_STEPS;
        if (final_pos < 1) final_pos = 1;
        ESP_LOGI(TAG, "QUIZ failed for P%d (wrong), back to %d", player, final_pos);
    } else {
        ESP_LOGI(TAG, "QUIZ correct for P%d", player);
    }

    set_player_pos(player, final_pos);

    last_moved_player = player;
    last_dice_roll = 0;

    // User player moves manually on physical board; do not send PIC command on quiz resolution.
    send_quiz_state_to_host(correct ? "correct" : "wrong", player, 0);

    active_player = next_player(player);
    if (active_player == AI_PLAYER) {
        dice_motor_spin();
    }

    showPlayers();
    send_positions_to_host();
    auto_skip_if_needed();

    quiz_pending = false;
    quiz_player = 0;
    quiz_result = 0;
    return true;
}

// C API for MCP/voice-assistant side to report quiz result.
void game_submit_quiz_result(int correct) {
    if (!quiz_pending) return;
    quiz_result = correct ? 1 : -1;
}

int game_is_quiz_pending(void) {
    return quiz_pending ? 1 : 0;
}

int game_get_quiz_player(void) {
    return quiz_player;
}

int game_get_quiz_time_left_s(void) {
    // Timeout/countdown disabled in current ruleset.
    return 0;
}

// C API: request companion app to confirm current camera-detected dice
// (same effect as user pressing SPACE in app).
// Returns 1 if accepted, 0 if rejected.
int game_request_user_dice_confirm_from_mcp(void) {
    if (quiz_pending) {
        ESP_LOGW(TAG, "Reject MCP confirm: quiz pending");
        return 0;
    }
    if (!is_human_player(active_player)) {
        ESP_LOGW(TAG, "Reject MCP confirm: not human turn (active=%d)", active_player);
        return 0;
    }
    if (dice_available) {
        ESP_LOGW(TAG, "Reject MCP confirm: previous dice still pending");
        return 0;
    }

    request_app_camera_confirm = true;
    ESP_LOGI(TAG, "MCP user dice confirm requested");
    return 1;
}

void game_start_quiz_countdown(void) {
    // Deprecated: no countdown in current ruleset. Keep no-op for compatibility.
    (void)quiz_player;
}

// ==== Serial polling: expected "DICE:<1-6>\n" ====
static void poll_serial_for_dice(void) {
    static char line[64];
    static int len = 0;

    uint8_t buf[128];
    int n = uart_read_bytes(UART_PORT, buf, sizeof(buf), 0);
    if (n <= 0) return;

    for (int i = 0; i < n; i++) {
        char c = (char)buf[i];

        if (c == '\n' || c == '\r') {
            if (len > 0) {
                line[len] = '\0';

                if (strncmp(line, "DICE:", 5) == 0) {
                    int v = atoi(line + 5);
                    if (v >= 1 && v <= 6) {
                        pending_dice = v;
                        dice_available = true;
                        printf("LOG:DICE received: %d\n", v);
                        fflush(stdout);
                    } else {
                        printf("LOG:Invalid dice value: %s\n", line);
                        fflush(stdout);
                    }
                } else if (strncmp(line, "CFG:HUMANS:", 11) == 0) {
                    int humans = atoi(line + 11);
                    set_total_humans(humans);
                    printf("LOG:Configured humans=%d total=%d\n", humans, total_players);
                    fflush(stdout);
                    showPlayers();
                    send_positions_to_host();
                } else if (strncmp(line, "QUIZRES:", 8) == 0) {
                    const char *res = line + 8;
                    if (strcmp(res, "CORRECT") == 0) {
                        game_submit_quiz_result(1);
                        printf("LOG:QUIZ result received: CORRECT\n");
                        fflush(stdout);
                    } else if (strcmp(res, "WRONG") == 0) {
                        game_submit_quiz_result(0);
                        printf("LOG:QUIZ result received: WRONG\n");
                        fflush(stdout);
                    } else {
                        printf("LOG:Invalid quiz result: %s\n", line);
                        fflush(stdout);
                    }
                }
                len = 0;
            }
        } else {
            if (len < (int)sizeof(line) - 1) {
                line[len++] = c;
            }
        }
    }
}

// dice motor init
static void init_dicemotor(void) {
    gpio_config_t cfg = {
        .pin_bit_mask = (1ULL << MOTOR_GPIO),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE
    };
    gpio_config(&cfg);
    gpio_set_level(MOTOR_GPIO, 0);
    ESP_LOGI(TAG, "Dice motor is initialized");
}

void dice_motor_spin(void) {
    ESP_LOGI(TAG, "Dice motor ON");
    gpio_set_level(MOTOR_GPIO, 1);
    vTaskDelay(pdMS_TO_TICKS(MOTOR_SPIN_TIME_MS));
    gpio_set_level(MOTOR_GPIO, 0);
    ESP_LOGI(TAG, "Dice motor OFF");
}

// Maps tile number (1..36) to (x,y) on 10x10
static void linear_to_xy_perimeter(int pos, uint8_t *x, uint8_t *y) {
    
    
    if (pos < 1 || pos > 36) {
        *x = 0; *y = 0;
        return; 
    }
    if (pos <= 10) {
        *x = pos - 1;
        *y = 0;
    }
    else if (pos <= 19) {
        *x = 9;
        *y = pos - 10;
    }
    else if (pos <= 28) {
        *x = 28 - pos;
        *y = 9;
    }
    else {
        *x = 0;
        *y = 37 - pos;
    }
}

// Bit packing
static inline uint16_t pack_xy_5_5_6(uint8_t x, uint8_t y)
{
    x &= 0x1F;
    y &= 0x1F;
    return (uint16_t)((x << 11) | (y << 6));
}

static uint16_t tile_to_packet_bytes(int tile)
{
    uint8_t x, y;
    linear_to_xy_perimeter(tile, &x, &y);

    uint16_t cmd = pack_xy_5_5_6(x, y);
    ESP_LOGI(TAG, "DEBUG PACK tile=%d x=%u y=%u cmd=0x%04X bytes=[%02X %02X]",
             tile, x, y, cmd, (cmd >> 8) & 0xFF, cmd & 0xFF);

    return cmd;
}

// ======================
// NRF INIT (FIX: only once)
// ======================
static const uint8_t NRF_TX_ADDR[5] = {0xE8, 0xE8, 0xF0, 0xF0, 0xE1};
static const uint8_t NRF_CHANNEL = 76;
static bool nrf_ready = false;

static void nrf_init_once(void)
{
    if (nrf_ready) return;

    // nrf24_init() 
    nrf24_init();
    nrf24_tx_mode((uint8_t*)NRF_TX_ADDR, NRF_CHANNEL);

    nrf_ready = true;
    ESP_LOGI(TAG, "NRF24 initialized ONCE (channel=%d)", NRF_CHANNEL);
}

static bool nrf_send_to_pic(uint16_t command_val)
{
    nrf_init_once();

    uint8_t data[32];
    memset(data, 0, sizeof(data));

    // Big endian
    data[0] = (uint8_t)((command_val >> 8) & 0xFF);
    data[1] = (uint8_t)(command_val & 0xFF);

    ESP_LOGI(TAG, "Attempting to send: 0x%04X on channel %d", command_val, NRF_CHANNEL);

    for (int retry = 0; retry < 5; retry++) {
        uint8_t result = nrf24_transmit(data);

        if (result == 1) {
            ESP_LOGI(TAG, ">> TX Success! ACK received.");
            return true;
        } else {
            ESP_LOGW(TAG, ">> TX Failed (Attempt %d/5). Retrying...", retry + 1);
            vTaskDelay(pdMS_TO_TICKS(500));
        }
    }

    ESP_LOGE(TAG, ">> Failed to send command after multiple attempts.");
    return false;
}

static void send_position_to_pic(int linear_pos)
{
    uint16_t cmd = tile_to_packet_bytes(linear_pos);
    bool ok = nrf_send_to_pic(cmd);
    ESP_LOGI(TAG, "NRF->PIC pos=%d cmd=0x%04X %s", linear_pos, cmd, ok ? "OK" : "FAIL");
}

// ==========================
// SPECIAL TILE PROCESSING
// ==========================
static int process_special_tile(int player, int landed_pos, bool *repeat_turn){
    int final_pos = landed_pos;

    ESP_LOGI(TAG, "Player %d landed on %d", player, landed_pos);

    switch(landed_pos){

        case 4:
            ESP_LOGI(TAG, "Tile 4: Skip one turn (Cafe)");
            turns_to_skip[player] = 1;
            break;

        case 6:
            ESP_LOGI(TAG, "Tile 6: Throw again (Museum knowledge)");
            *repeat_turn = true;
            break;

        case 26:
            ESP_LOGI(TAG, "Tile 26: Roll again (Concert energy)");
            *repeat_turn = true;
            break;

        case 10:
            ESP_LOGI(TAG, "Tile 10: Move to Kruidtuin (12)");
            final_pos = 12;
            break;

        case 14:
            ESP_LOGI(TAG, "Tile 14: Go back to beginning (1)");
            final_pos = 1;
            break;

        case 19:
            ESP_LOGI(TAG, "Tile 19: Move to Stadium OHL (24)");
            final_pos = 24;
            break;

        case 22:
            ESP_LOGI(TAG, "Tile 22: Skip one turn (Reading)");
            turns_to_skip[player] = 1;
            break;

        case 28:
            ESP_LOGI(TAG, "Tile 28: Skip two turns");
            turns_to_skip[player] = 2;
            break;

        case 31:
            ESP_LOGI(TAG, "Tile 31: Move 1 space forward");
            final_pos = 32;
            break;

        case 35:
            ESP_LOGI(TAG, "Tile 35: Scared! Go back to 9");
            final_pos = 9;
            break;

        case 16:
            ESP_LOGI(TAG, "Tile 16: Throw again and go backwards next!");
            *repeat_turn = true;
            move_backwards_flag[player] = true;
            break;

        default:
            break;
    }

    return final_pos;
}

// Computes next position. Extra output: was_skip_turn
static int compute_next_position_for_player(
    int player,
    int dice,
    bool *repeat_turn,
    int *landed_pos_out,
    bool *was_skip_turn
)
{
    *repeat_turn = false;
    if (was_skip_turn) *was_skip_turn = false;

    if (turns_to_skip[player] > 0) {
        ESP_LOGI(TAG, "Player %d skips turn (%d left)", player, turns_to_skip[player]);
        turns_to_skip[player]--;

        if (landed_pos_out) *landed_pos_out = get_player_pos(player);
        if (was_skip_turn) *was_skip_turn = true;

        return get_player_pos(player);
    }

    int old_pos = get_player_pos(player);
    int calculated_pos = old_pos;

    if (move_backwards_flag[player]) {
        ESP_LOGI(TAG, "Player %d moves BACKWARDS %d steps", player, dice);
        calculated_pos = old_pos - dice;
        if (calculated_pos < 1) calculated_pos = 1;
        move_backwards_flag[player] = false;
    } else {
        calculated_pos = old_pos + dice;
        if (calculated_pos > NUM_TILES) calculated_pos = NUM_TILES;
    }

    if (landed_pos_out) *landed_pos_out = calculated_pos;
    int final_pos = process_special_tile(player, calculated_pos, repeat_turn);

    return final_pos;
}

static inline int next_player(int p) {
    p++;
    if (p > total_players) p = 1;
    return p;
}

// ======================
// FIX: AUTO-SKIP in main loop
// ======================
static void auto_skip_if_needed(void)
{
    // consume all pending skips for the current active player (and chain if multiple)
    while (turns_to_skip[active_player] > 0) {
        int skipped_player = active_player;

        ESP_LOGI(TAG, "AUTO-SKIP: Player %d skips now (remaining before=%d)",
                 skipped_player, turns_to_skip[skipped_player]);

        turns_to_skip[skipped_player]--;

        // host should see: last_player = skipped_player, dice = 0
        last_moved_player = skipped_player;
        last_dice_roll = 0;

        // Only AI player is mirrored to PIC by program control.
        if (skipped_player == AI_PLAYER) {
            send_position_to_pic(get_player_pos(skipped_player));
        }

        // advance to next player
        active_player = next_player(skipped_player);

        // keep your original motor logic consistent
        if (active_player == AI_PLAYER) {
            dice_motor_spin();
        }

        showPlayers();
        send_positions_to_host();

        // small delay so host UI updates nicely
        vTaskDelay(pdMS_TO_TICKS(80));
    }
}

void led_game_app_main(void) {
    ESP_LOGI(TAG, "Start spelers-ledstrip (wacht op DICE:n via serial)");
    init_ledstrip();
    init_dicemotor();

    // UART setup (UART0)
    uart_config_t uart_cfg = {
        .baud_rate = 115200,
        .data_bits = UART_DATA_8_BITS,
        .parity    = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT
    };

    esp_err_t err = uart_driver_install(UART_PORT, UART_RX_BUF * 2, 0, 0, NULL, 0);
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        ESP_ERROR_CHECK(err);
    }
    ESP_ERROR_CHECK(uart_param_config(UART_PORT, &uart_cfg));
    uart_set_pin(UART_PORT, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);

    // NRF init ONCE (prevents spi_bus_initialize already initialized / NO_MEM)
    nrf_init_once();

    // send first state to host
    last_dice_roll = 0;
    last_moved_player = 0;
    send_positions_to_host();

    showPlayers();
    if (active_player == AI_PLAYER) {
        dice_motor_spin();
    }

    while (true) {
        poll_serial_for_dice();

        if (request_app_camera_confirm) {
            // Companion app consumes this and performs the same flow as SPACE:
            // read stable camera dice value and send DICE:n back.
            printf("CMD:VOICE_CONFIRM_DICE\n");
            fflush(stdout);
            request_app_camera_confirm = false;
        }

        if (quiz_pending) {
            if (dice_available) {
                dice_available = false;
                pending_dice = 0;
                ESP_LOGW(TAG, "Ignoring DICE while quiz is pending");
            }
            apply_quiz_resolution_if_ready();
        }

        // AUTO-SKIP: without dice input
        if (!quiz_pending && !dice_available) {
            auto_skip_if_needed();
        }

        // === move ONLY when we received a dice value ===
        if (!quiz_pending && dice_available) {
            dice_available = false;
            int dice = pending_dice;
            pending_dice = 0;

            int current_player = active_player;

            bool repeat_turn = false;
            bool was_skip_turn = false;

            int old_pos = get_player_pos(current_player);
            int landed_pos = old_pos;

            int new_pos = compute_next_position_for_player(
                current_player, dice, &repeat_turn, &landed_pos, &was_skip_turn
            );

            ESP_LOGI(TAG, "P%d dice=%d old=%d landed=%d final=%d repeat=%d was_skip=%d skip_left=%d backflag=%d",
                current_player, dice, old_pos, landed_pos, new_pos,
                repeat_turn,
                was_skip_turn,
                turns_to_skip[current_player],
                move_backwards_flag[current_player]);

            set_player_pos(current_player, new_pos);

            // with skip: dice=0 send to host
            last_dice_roll = was_skip_turn ? 0 : dice;
            last_moved_player = current_player;

            // Only AI player movement is sent to PIC.
            if (current_player == AI_PLAYER) {
                send_position_to_pic(get_player_pos(current_player));
            }

            // effect only if actually moved AND it's a special tile (rule tile)
            if (!was_skip_turn && new_pos != old_pos && is_rule_special_tile(landed_pos)) {
                showRuleTileEffect(landed_pos);
            }

            if (!was_skip_turn && is_human_player(current_player) && new_pos == QUIZ_TILE) {
                showPlayers();
                send_positions_to_host();
                start_quiz_gate_for_player(current_player);
                continue;
            }

            // roll again logica
            if (!repeat_turn) {
                active_player = next_player(current_player);
            } else {
                ESP_LOGI(TAG, "Player %d rolls again!", current_player);
                active_player = current_player;
            }

            if (active_player == AI_PLAYER) {
                dice_motor_spin();
            }

            showPlayers();
            send_positions_to_host();
            auto_skip_if_needed();
        }

        // Breathing animation of the player who is next in turn
        pulseActivePlayer(1);

        // Check winner (tile 36)
        {
            uint8_t r = 0, g = 0, b = 0;
            bool winnerFound = false;
            int winner_player = 0;

            for (int p = 1; p <= total_players; p++) {
                if (get_player_pos(p) == NUM_TILES) {
                    get_player_color(p, &r, &g, &b);
                    winnerFound = true;
                    winner_player = p;
                    break;
                }
            }

            if (winnerFound) {
                send_winner_to_host(winner_player);
                playWinEffect(r, g, b);
                vTaskDelay(pdMS_TO_TICKS(500));
                reset_game_round();
                continue;
            }
        }

        vTaskDelay(pdMS_TO_TICKS(100));
    }
}

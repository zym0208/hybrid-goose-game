idf_component_register(SRCS "main.c" "nrf24.c"
                    INCLUDE_DIRS "."
                    REQUIRES driver
                    REQUIRES esp_timer)

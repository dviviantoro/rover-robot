/* main.cpp -- STM32 entry point.
 *
 * Thin by design: everything that decides anything lives in the portable core, which
 * is tested natively. This file only moves bytes and calls the loop.
 */
#include <Arduino.h>

extern "C" {
#include "robot_board.h"
#include "protocol.h"
#include "robot.h"

const board_io_t *board_stm32_io(void);
void board_stm32_begin(void);
void board_stm32_control_tick(void);
}

static robot_t g_robot;
static char g_line[PROTOCOL_MAX_LINE];
static size_t g_len;
static bool g_overlong;     /* discarding the rest of a line that did not fit */

void setup()
{
    board_stm32_begin();
    robot_init(&g_robot, board_stm32_io());
}

void loop()
{
    /* 1. Drain everything waiting on USB. Reading promptly is what keeps the watchdog
     *    fed, so nothing slow may come before this. */
    while (Serial.available() > 0) {
        const char c = (char)Serial.read();
        if (c == '\n' || c == '\r') {
            if (g_overlong) {
                g_overlong = false;     /* the newline ends the bad line; start clean */
                g_len = 0;
            } else if (g_len > 0) {
                g_line[g_len] = '\0';
                robot_on_line(&g_robot, g_line);
                g_len = 0;
            }
        } else if (g_overlong) {
            /* still inside an overlong line: drop it. Its tail must never be parsed
             * as if it were a line of its own. */
        } else if (g_len + 1 < sizeof(g_line)) {
            g_line[g_len++] = c;
        } else {
            g_overlong = true;
            g_len = 0;
        }
    }

    /* 2. The watchdog and timed-action expiry. Must run every pass, unconditionally:
     *    if this stops being called the robot cannot stop itself. */
    robot_tick(&g_robot, millis());

    /* 3. Velocity PID. Last, because it is the only part that can afford to be late. */
    board_stm32_control_tick();
}

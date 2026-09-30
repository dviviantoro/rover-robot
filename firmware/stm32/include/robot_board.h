/* robot_board.h -- the hardware interface the robot core is allowed to touch.
 *
 * Everything above this line is portable C with no HAL, no registers and no timers,
 * which is what makes the watchdog and the protocol testable natively (see test/).
 * Two implementations exist:
 *
 *   board_stm32.c  -- real motors, servos and LED matrix
 *   tools/stm32_sim -- a host build that links this same core, so the Jetson side and
 *                      the watchdog can be exercised without silicon
 *
 * dev-docs/architecture/serial-protocol.md is the contract both sides implement.
 *
 * NOTE ON THE NAME: this was `board.h` until it collided with STM32duino's own
 * cores/arduino/board.h. Because -Iinclude precedes the framework's include path,
 * Arduino.h picked up THIS file instead, the framework's board.h never loaded, and the
 * build died with `LL_GPIO_PULL_NO undeclared` a hundred lines away in PeripheralPins.c.
 * Keep project headers prefixed.
 */
#ifndef ROBOT_BOARD_H
#define ROBOT_BOARD_H

#include <stdint.h>

#define WHEEL_COUNT 4

/* Wheel order is fixed everywhere: front-left, front-right, rear-left, rear-right. */
enum {
    WHEEL_FRONT_LEFT = 0,
    WHEEL_FRONT_RIGHT = 1,
    WHEEL_REAR_LEFT = 2,
    WHEEL_REAR_RIGHT = 3
};

typedef struct {
    /* Target angular velocity per wheel, rad/s. The PID loops chase these.
     * Called with all zeros to stop -- including from the watchdog, which must be
     * able to stop the robot without any cooperation from the Jetson. */
    void (*set_wheels)(const float w[WHEEL_COUNT]);

    /* LED matrix expression. Returns 0 on success, non-zero if the name is unknown
     * so the caller can answer ERR. Instant -- no DONE follows. */
    int (*set_face)(const char *name);

    /* Begin a preset servo sequence. Returns 0 on success and writes how long the
     * sequence runs, so the core can emit DONE HAND when it finishes -- the protocol
     * doc lists "gesture complete" as a DONE case alongside drive expiry. */
    int (*start_gesture)(const char *name, uint32_t *duration_ms);

    /* Emit one newline-terminated line to the Jetson. */
    void (*emit)(const char *line);

    /* Milliseconds since boot, monotonic. */
    uint32_t (*now_ms)(void);

    /* Telemetry for STAT. */
    float (*battery_volts)(void);
    int (*motors_stalled)(void);
    float (*temperature_c)(void);
} board_io_t;

#endif /* ROBOT_BOARD_H */

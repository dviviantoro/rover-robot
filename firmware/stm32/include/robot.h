/* robot.h -- the command state machine.
 *
 * Owns everything the protocol doc says the STM32 owns: parsing, validation, the
 * watchdog, bounded-duration motion, and the responses. Hardware reaches it only
 * through board_io_t, which is what lets the whole thing run under test on a host.
 */
#ifndef ROBOT_ROBOT_H
#define ROBOT_ROBOT_H

#include "robot_board.h"
#include "mecanum.h"
#include "watchdog.h"

typedef struct {
    board_io_t io;
    mecanum_config_t mec;
    watchdog_t wd;

    uint8_t drive_active;
    uint32_t drive_ends_ms;
    /* Which verb started the motion, so DONE/ERR echo the right command.
     * DRIVE and WHEEL share the motion slot but are not interchangeable in a response. */
    char drive_verb[8];

    uint8_t gesture_active;
    uint32_t gesture_ends_ms;
} robot_t;

void robot_init(robot_t *r, const board_io_t *io);

/* Feed one received line (without its newline). Always answers exactly one response,
 * per "Every command gets a response" in the protocol doc. */
void robot_on_line(robot_t *r, const char *line);

/* Call frequently from the main loop. Drives the watchdog and expires timed actions. */
void robot_tick(robot_t *r, uint32_t now_ms);

/* Zero every motor output immediately and cancel any motion in flight. */
void robot_all_stop(robot_t *r);

#endif /* ROBOT_ROBOT_H */

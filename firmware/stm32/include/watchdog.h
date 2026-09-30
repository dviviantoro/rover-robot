/* watchdog.h -- the heartbeat timeout. THE safety mechanism of this project.
 *
 * dev-docs/architecture/serial-protocol.md § Heartbeat watchdog:
 *
 *   - The Jetson sends PING every 200 ms while the link is up.
 *   - If the STM32 receives no line OF ANY KIND for 500 ms, it immediately zeroes all
 *     motor outputs ON ITS OWN AUTHORITY and enters a safe idle state.
 *   - Recovery requires a fresh command after PING resumes. Motion does not auto-resume.
 *
 * The reason it exists: the Jetson runs a full Linux userspace on a memory-constrained
 * board with a real OOM-kill risk. It WILL die at a bad moment. A robot whose only stop
 * mechanism is a STOP command from a dead process is a robot that drives into a wall.
 *
 * Deliberately free of hardware dependencies so the exact code that ships can be run
 * under test on a host -- see test/test_native/test_watchdog.c.
 */
#ifndef ROBOT_WATCHDOG_H
#define ROBOT_WATCHDOG_H

#include <stdint.h>

#define WATCHDOG_TIMEOUT_MS 500u   /* non-negotiable, per the protocol doc */
#define HEARTBEAT_PERIOD_MS 200u   /* what the Jetson is expected to send */

typedef struct {
    uint32_t timeout_ms;
    uint32_t last_rx_ms;
    uint8_t link_seen;   /* has any line ever arrived? */
    uint8_t tripped;     /* currently in the safe state because of a timeout */
} watchdog_t;

void watchdog_init(watchdog_t *wd, uint32_t timeout_ms);

/* Call for EVERY received line, whatever it is. The contract says "no line of any
 * kind", so a STATUS or a malformed line feeds the watchdog just as a PING does --
 * the link being alive is what is being measured, not the content. */
void watchdog_line_received(watchdog_t *wd, uint32_t now_ms);

/* Call from the main loop. Returns 1 exactly once, on the transition into the tripped
 * state, so the caller can stop the motors and report it without repeating. */
int watchdog_check(watchdog_t *wd, uint32_t now_ms);

int watchdog_tripped(const watchdog_t *wd);

/* Milliseconds since the last line. Unsigned arithmetic, so it stays correct across
 * the 32-bit millisecond counter wrapping (~49 days). */
uint32_t watchdog_silence_ms(const watchdog_t *wd, uint32_t now_ms);

#endif /* ROBOT_WATCHDOG_H */

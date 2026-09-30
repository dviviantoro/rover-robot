#include "watchdog.h"

void watchdog_init(watchdog_t *wd, uint32_t timeout_ms)
{
    wd->timeout_ms = timeout_ms ? timeout_ms : WATCHDOG_TIMEOUT_MS;
    wd->last_rx_ms = 0u;
    wd->link_seen = 0u;
    wd->tripped = 0u;
}

void watchdog_line_received(watchdog_t *wd, uint32_t now_ms)
{
    wd->last_rx_ms = now_ms;
    wd->link_seen = 1u;
    /* Clear the trip so a later silence can be detected again. This does NOT resume
     * motion: the caller cancelled the active drive when the trip fired, and nothing
     * here can start it again. Only a fresh DRIVE moves the robot. */
    wd->tripped = 0u;
}

uint32_t watchdog_silence_ms(const watchdog_t *wd, uint32_t now_ms)
{
    if (!wd->link_seen) {
        return 0u;
    }
    return now_ms - wd->last_rx_ms;   /* wrap-safe on uint32_t */
}

int watchdog_check(watchdog_t *wd, uint32_t now_ms)
{
    if (!wd->link_seen || wd->tripped) {
        /* Nothing has ever arrived (motors were never commanded, so we are already
         * safe), or we have already tripped and reported it. */
        return 0;
    }
    if (watchdog_silence_ms(wd, now_ms) >= wd->timeout_ms) {
        wd->tripped = 1u;
        return 1;   /* edge: caller must zero the motors now */
    }
    return 0;
}

int watchdog_tripped(const watchdog_t *wd)
{
    return wd->tripped ? 1 : 0;
}

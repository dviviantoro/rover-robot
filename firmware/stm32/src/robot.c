#include "robot.h"
#include "protocol.h"

#include <stdio.h>

#include <string.h>

static void emit(robot_t *r, const char *line)
{
    if (r->io.emit) {
        r->io.emit(line);
    }
}

static void set_wheels(robot_t *r, const float w[WHEEL_COUNT])
{
    if (r->io.set_wheels) {
        r->io.set_wheels(w);
    }
}

void robot_all_stop(robot_t *r)
{
    static const float zero[WHEEL_COUNT] = { 0.0f, 0.0f, 0.0f, 0.0f };
    r->drive_active = 0u;
    r->drive_ends_ms = 0u;
    r->drive_verb[0] = '\0';
    set_wheels(r, zero);
}

void robot_init(robot_t *r, const board_io_t *io)
{
    memset(r, 0, sizeof(*r));
    r->io = *io;
    mecanum_default_config(&r->mec);
    watchdog_init(&r->wd, WATCHDOG_TIMEOUT_MS);
    robot_all_stop(r);
}

static void handle_drive(robot_t *r, const command_t *cmd, char *out, uint32_t cap)
{
    float wheels[WHEEL_COUNT];
    mecanum_solve(&r->mec, cmd->vx, cmd->vy, cmd->omega, wheels);
    set_wheels(r, wheels);
    r->drive_active = 1u;
    r->drive_ends_ms = r->io.now_ms() + cmd->duration_ms;
    snprintf(r->drive_verb, sizeof(r->drive_verb), "%s", "DRIVE");
    protocol_ok(out, cap, cmd->verb);
}

void robot_on_line(robot_t *r, const char *line)
{
    const uint32_t now = r->io.now_ms();
    char out[PROTOCOL_MAX_LINE];
    command_t cmd;

    /* Feed the watchdog FIRST, and for any line at all -- the contract measures the
     * link being alive, not whether the content was valid. A stream of malformed
     * lines still means the Jetson is running. */
    watchdog_line_received(&r->wd, now);

    if (!protocol_parse(line, &cmd)) {
        protocol_err(out, sizeof(out), cmd.verb, cmd.error);
        emit(r, out);
        return;
    }

    switch (cmd.kind) {
    case CMD_DRIVE:
        handle_drive(r, &cmd, out, sizeof(out));
        break;

    case CMD_WHEEL: {
        float wheels[WHEEL_COUNT];
        mecanum_wheel_only(&r->mec, cmd.wheel_index, cmd.wheel_rad_s, wheels);
        set_wheels(r, wheels);
        r->drive_active = 1u;   /* same bounded duration and watchdog as DRIVE */
        r->drive_ends_ms = now + cmd.duration_ms;
        snprintf(r->drive_verb, sizeof(r->drive_verb), "%s", "WHEEL");
        protocol_ok(out, sizeof(out), cmd.verb);
        break;
    }

    case CMD_STOP:
        robot_all_stop(r);
        protocol_ok(out, sizeof(out), cmd.verb);
        break;

    case CMD_FACE:
        if (r->io.set_face && r->io.set_face(cmd.arg) != 0) {
            protocol_err(out, sizeof(out), cmd.verb, "unknown");
        } else {
            protocol_ok(out, sizeof(out), cmd.verb);
        }
        break;

    case CMD_HAND: {
        uint32_t duration = 0u;
        if (r->io.start_gesture && r->io.start_gesture(cmd.arg, &duration) != 0) {
            protocol_err(out, sizeof(out), cmd.verb, "unknown");
        } else {
            r->gesture_active = 1u;
            r->gesture_ends_ms = now + duration;
            protocol_ok(out, sizeof(out), cmd.verb);
        }
        break;
    }

    case CMD_PING:
        protocol_ok(out, sizeof(out), cmd.verb);
        break;

    case CMD_STATUS:
        /* STAT is itself the response to STATUS -- no separate OK. */
        protocol_stat(out, sizeof(out),
                      r->io.battery_volts ? r->io.battery_volts() : 0.0f,
                      r->io.motors_stalled ? r->io.motors_stalled() : 0,
                      r->io.temperature_c ? r->io.temperature_c() : 0.0f,
                      now);
        break;

    default:
        protocol_err(out, sizeof(out), cmd.verb, "unknown");
        break;
    }

    emit(r, out);
}

void robot_tick(robot_t *r, uint32_t now_ms)
{
    char out[PROTOCOL_MAX_LINE];

    /* THE safety path. Checked before anything else, and it does not consult, ask or
     * wait for the Jetson -- that is the entire point (serial-protocol.md § Heartbeat
     * watchdog). */
    if (watchdog_check(&r->wd, now_ms)) {
        const int was_driving = r->drive_active;
        char verb[8];
        snprintf(verb, sizeof(verb), "%s", r->drive_verb[0] ? r->drive_verb : "DRIVE");
        robot_all_stop(r);
        r->gesture_active = 0u;
        if (was_driving) {
            /* The link is presumably dead, so this may reach nobody. It is emitted
             * anyway: if the Jetson comes back, it learns its drive was aborted rather
             * than silently believing it is still running. "Unsafe state" is exactly
             * what the doc lists ERR for. */
            protocol_err(out, sizeof(out), verb, "watchdog");
            emit(r, out);
        }
        return;
    }

    /* Bounded-duration motion: the STM32 auto-stops when the duration expires, so
     * motion never waits on a STOP that might never arrive. */
    if (r->drive_active && (int32_t)(now_ms - r->drive_ends_ms) >= 0) {
        char verb[8];
        snprintf(verb, sizeof(verb), "%s", r->drive_verb[0] ? r->drive_verb : "DRIVE");
        robot_all_stop(r);
        protocol_done(out, sizeof(out), verb);
        emit(r, out);
    }

    if (r->gesture_active && (int32_t)(now_ms - r->gesture_ends_ms) >= 0) {
        r->gesture_active = 0u;
        protocol_done(out, sizeof(out), "HAND");
        emit(r, out);
    }
}

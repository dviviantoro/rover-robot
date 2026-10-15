/* protocol.h -- the line protocol from dev-docs/architecture/serial-protocol.md.
 *
 * Jetson -> STM32:
 *   DRIVE <vx> <vy> <omega> <ms>   STOP   FACE <name>   HAND <gesture>   PING   STATUS
 *
 * STM32 -> Jetson:
 *   OK <command>   ERR <command> <reason>   DONE <command>
 *   STAT batt=<V> motors=<ok|stall> temp=<C> up=<ms>
 *
 * Discovery (every start of robot-core, to every serial port):
 *   Jetson -> {PING_robot}
 *   STM32  -> {"device":"STM32","status":"online"}
 *
 * Reply lines (Jetson -> STM32, information): one JSON object per phase of a turn,
 * starting with `{`. Swallowed without an answer -- nothing waits on them.
 *
 * Newline-terminated ASCII. Parsing is allocation-free and does not mutate its input,
 * so it is safe to call straight from the USB CDC receive path.
 */
#ifndef ROBOT_PROTOCOL_H
#define ROBOT_PROTOCOL_H

#include <stdint.h>

#define PROTOCOL_MAX_LINE 256
#define PROTOCOL_MAX_TOKEN 24

/* Safety limits. The orchestrator clamps before sending; these are the STM32's own
 * second line of defence, as the protocol doc requires. Values are placeholders until
 * the chassis is characterised -- raising them is a hardware decision, not a code one. */
#define LIMIT_VX_MPS 1.0f
#define LIMIT_VY_MPS 1.0f
#define LIMIT_OMEGA_RPS 3.0f
#define LIMIT_DURATION_MS 10000u
#define LIMIT_WHEEL_RAD_S 30.0f

typedef enum {
    CMD_UNKNOWN = 0,
    CMD_DRIVE,
    CMD_STOP,
    CMD_FACE,
    CMD_HAND,
    CMD_PING,
    CMD_STATUS,
    CMD_WHEEL      /* bring-up only -- see serial-protocol.md */
} cmd_kind_t;

typedef struct {
    cmd_kind_t kind;
    char verb[PROTOCOL_MAX_TOKEN];  /* as received, echoed back in OK/ERR/DONE */
    float vx, vy, omega;
    uint32_t duration_ms;
    int wheel_index;                /* WHEEL: 0..3 */
    float wheel_rad_s;              /* WHEEL: target angular velocity */
    char arg[PROTOCOL_MAX_TOKEN];   /* FACE / HAND argument */
    const char *error;              /* NULL when valid; a single token otherwise */
} command_t;

/* Returns 1 if the line parsed into a valid command, 0 otherwise.
 * On failure `out->error` names the reason and `out->verb` holds whatever verb was
 * received, so the caller can always answer `ERR <verb> <reason>`. */
int protocol_parse(const char *line, command_t *out);

/* Helpers for building responses. Each writes a NUL-terminated line WITHOUT a newline;
 * the transport adds the terminator. */
void protocol_ok(char *buf, uint32_t len, const char *verb);
void protocol_err(char *buf, uint32_t len, const char *verb, const char *reason);
void protocol_done(char *buf, uint32_t len, const char *verb);
/* The discovery ping. Matched case-insensitively and ignoring surrounding space. */
#define PROTOCOL_DISCOVERY_PING "{PING_robot}"
int protocol_is_discovery_ping(const char *line);
/* A reply line is any line whose first non-space character is `{` (and is not the
 * discovery ping). They are information for the body, never commands. */
int protocol_is_reply_line(const char *line);
void protocol_online(char *buf, uint32_t len, const char *device);

void protocol_stat(char *buf, uint32_t len,
                   float battery_v, int stalled, float temp_c, uint32_t uptime_ms);

#endif /* ROBOT_PROTOCOL_H */

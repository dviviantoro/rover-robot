#include "protocol.h"

#include "robot_board.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int is_space(char c) { return c == ' ' || c == '\t' || c == '\r' || c == '\n'; }

static char upper(char c) { return (c >= 'a' && c <= 'z') ? (char)(c - 'a' + 'A') : c; }

/* Case-insensitive verb comparison. The documented forms are uppercase; accepting
 * lowercase too costs nothing and matters because the protocol doc explicitly values
 * being able to drive this by hand from `screen` or `minicom`. */
static int verb_is(const char *tok, const char *want)
{
    while (*want) {
        if (upper(*tok++) != *want++) {
            return 0;
        }
    }
    return *tok == '\0';
}

/* Copy the token starting at *p into `dst`; advance *p past it. Returns token length. */
static uint32_t take_token(const char **p, char *dst, uint32_t cap)
{
    const char *s = *p;
    while (*s && is_space(*s)) s++;
    uint32_t n = 0;
    while (*s && !is_space(*s)) {
        if (n + 1 < cap) dst[n] = *s;
        n++;
        s++;
    }
    dst[(n < cap) ? n : cap - 1] = '\0';
    *p = s;
    return n;
}

static int parse_float(const char *tok, float *out)
{
    char *end = NULL;
    float v = strtof(tok, &end);
    if (end == tok || (end && *end != '\0')) {
        return 0;
    }
    /* Reject NaN and infinities before they reach the motors. */
    if (v != v || v > 1e6f || v < -1e6f) {
        return 0;
    }
    *out = v;
    return 1;
}

static int parse_u32(const char *tok, uint32_t *out)
{
    char *end = NULL;
    long v = strtol(tok, &end, 10);
    if (end == tok || (end && *end != '\0') || v < 0) {
        return 0;
    }
    *out = (uint32_t)v;
    return 1;
}

static int no_more_tokens(const char *p)
{
    while (*p && is_space(*p)) p++;
    return *p == '\0';
}

int protocol_parse(const char *line, command_t *out)
{
    memset(out, 0, sizeof(*out));
    out->kind = CMD_UNKNOWN;

    if (line == NULL) {
        out->error = "empty";
        return 0;
    }
    if (strlen(line) >= PROTOCOL_MAX_LINE) {
        out->error = "toolong";
        return 0;
    }

    const char *p = line;
    char tok[PROTOCOL_MAX_TOKEN];
    if (take_token(&p, out->verb, sizeof(out->verb)) == 0) {
        out->error = "empty";
        return 0;
    }

    if (verb_is(out->verb, "DRIVE")) {
        out->kind = CMD_DRIVE;
        float *fields[3] = { &out->vx, &out->vy, &out->omega };
        for (int i = 0; i < 3; ++i) {
            if (take_token(&p, tok, sizeof(tok)) == 0 || !parse_float(tok, fields[i])) {
                out->error = "badargs";
                return 0;
            }
        }
        if (take_token(&p, tok, sizeof(tok)) == 0 || !parse_u32(tok, &out->duration_ms)) {
            out->error = "badargs";
            return 0;
        }
        if (!no_more_tokens(p)) {
            out->error = "badargs";
            return 0;
        }
        /* Second line of defence: the orchestrator clamps too, but never trust it. */
        if (out->vx > LIMIT_VX_MPS || out->vx < -LIMIT_VX_MPS ||
            out->vy > LIMIT_VY_MPS || out->vy < -LIMIT_VY_MPS ||
            out->omega > LIMIT_OMEGA_RPS || out->omega < -LIMIT_OMEGA_RPS ||
            out->duration_ms > LIMIT_DURATION_MS) {
            out->error = "range";
            return 0;
        }
        if (out->duration_ms == 0u) {
            /* A zero-duration drive is a STOP written confusingly. Reject it rather
             * than starting motion that ends in the same millisecond. */
            out->error = "range";
            return 0;
        }
        return 1;
    }

    if (verb_is(out->verb, "WHEEL")) {
        /* Bring-up diagnostic: bypasses the kinematics to spin one motor.
         * serial-protocol.md § Verifying wheel direction. */
        out->kind = CMD_WHEEL;
        uint32_t index = 0u;
        if (take_token(&p, tok, sizeof(tok)) == 0 || !parse_u32(tok, &index)) {
            out->error = "badargs";
            return 0;
        }
        if (take_token(&p, tok, sizeof(tok)) == 0 || !parse_float(tok, &out->wheel_rad_s)) {
            out->error = "badargs";
            return 0;
        }
        if (take_token(&p, tok, sizeof(tok)) == 0 || !parse_u32(tok, &out->duration_ms)) {
            out->error = "badargs";
            return 0;
        }
        if (!no_more_tokens(p)) {
            out->error = "badargs";
            return 0;
        }
        if (index >= WHEEL_COUNT || out->duration_ms == 0u ||
            out->duration_ms > LIMIT_DURATION_MS ||
            out->wheel_rad_s > LIMIT_WHEEL_RAD_S || out->wheel_rad_s < -LIMIT_WHEEL_RAD_S) {
            out->error = "range";
            return 0;
        }
        out->wheel_index = (int)index;
        return 1;
    }

    if (verb_is(out->verb, "STOP") || verb_is(out->verb, "PING") ||
        verb_is(out->verb, "STATUS")) {
        out->kind = verb_is(out->verb, "STOP") ? CMD_STOP
                  : verb_is(out->verb, "PING") ? CMD_PING
                  : CMD_STATUS;
        if (!no_more_tokens(p)) {
            out->error = "badargs";
            return 0;
        }
        return 1;
    }

    if (verb_is(out->verb, "FACE") || verb_is(out->verb, "HAND")) {
        out->kind = verb_is(out->verb, "FACE") ? CMD_FACE : CMD_HAND;
        if (take_token(&p, out->arg, sizeof(out->arg)) == 0 || !no_more_tokens(p)) {
            out->error = "badargs";
            return 0;
        }
        return 1;
    }

    out->error = "unknown";
    return 0;
}

void protocol_ok(char *buf, uint32_t len, const char *verb)
{
    snprintf(buf, len, "OK %s", verb);
}

void protocol_err(char *buf, uint32_t len, const char *verb, const char *reason)
{
    snprintf(buf, len, "ERR %s %s", (verb && *verb) ? verb : "?", reason);
}

void protocol_done(char *buf, uint32_t len, const char *verb)
{
    snprintf(buf, len, "DONE %s", verb);
}

void protocol_stat(char *buf, uint32_t len,
                   float battery_v, int stalled, float temp_c, uint32_t uptime_ms)
{
    /* Format exactly as the doc specifies:
     * STAT batt=<V> motors=<ok|stall> temp=<C> up=<ms> */
    snprintf(buf, len, "STAT batt=%.2f motors=%s temp=%.1f up=%lu",
             (double)battery_v, stalled ? "stall" : "ok",
             (double)temp_c, (unsigned long)uptime_ms);
}

int protocol_is_discovery_ping(const char *line)
{
    if (line == NULL) {
        return 0;
    }
    while (*line && is_space(*line)) line++;
    const char *want = PROTOCOL_DISCOVERY_PING;
    while (*want) {
        if (upper(*line++) != upper(*want++)) {
            return 0;
        }
    }
    return no_more_tokens(line);
}

int protocol_is_reply_line(const char *line)
{
    if (line == NULL) {
        return 0;
    }
    while (*line && is_space(*line)) line++;
    return *line == '{' && !protocol_is_discovery_ping(line);
}

void protocol_online(char *buf, uint32_t len, const char *device)
{
    snprintf(buf, len, "{\"device\":\"%s\",\"status\":\"online\"}", device);
}

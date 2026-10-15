/* Native tests for the portable firmware core.
 *
 * These compile and run on the host with plain gcc, so the watchdog, the protocol and
 * the kinematics are covered without any silicon. Run: firmware/stm32/run_native_tests.sh
 */
#include <math.h>
#include <stdio.h>
#include <string.h>

#include "mecanum.h"
#include "pid.h"
#include "protocol.h"
#include "robot.h"
#include "watchdog.h"

static int checks = 0, failures = 0;

#define CHECK(cond, ...) do {                                        \
    checks++;                                                        \
    if (!(cond)) {                                                   \
        failures++;                                                  \
        printf("  FAIL %s:%d: ", __func__, __LINE__);                \
        printf(__VA_ARGS__);                                         \
        printf("\n");                                                \
    }                                                                \
} while (0)

#define CLOSE(a, b) (fabsf((a) - (b)) < 1e-3f)

/* ---- fake board ------------------------------------------------------- */

static uint32_t g_now;
static float g_wheels[WHEEL_COUNT];
static char g_lines[64][PROTOCOL_MAX_LINE];
static int g_line_count;
static int g_set_wheels_calls;

static void fake_set_wheels(const float w[WHEEL_COUNT])
{
    memcpy(g_wheels, w, sizeof(g_wheels));
    g_set_wheels_calls++;
}
static int fake_set_face(const char *n) { return strcmp(n, "happy") == 0 ? 0 : 1; }
static int fake_gesture(const char *n, uint32_t *d) {
    if (strcmp(n, "wave") != 0) return 1;
    *d = 800u; return 0;
}
static void fake_emit(const char *l) {
    if (g_line_count < 64) snprintf(g_lines[g_line_count++], PROTOCOL_MAX_LINE, "%s", l);
}
static uint32_t fake_now(void) { return g_now; }
static float fake_batt(void) { return 12.34f; }
static int fake_stall(void) { return 0; }
static float fake_temp(void) { return 31.5f; }

static const board_io_t FAKE_IO = {
    fake_set_wheels, fake_set_face, fake_gesture, fake_emit, fake_now,
    fake_batt, fake_stall, fake_temp
};

static void reset(robot_t *r)
{
    g_now = 1000u;              /* start away from 0 to catch zero-init assumptions */
    g_line_count = 0;
    g_set_wheels_calls = 0;
    memset(g_wheels, 0, sizeof(g_wheels));
    robot_init(r, &FAKE_IO);
}

static int wheels_all_zero(void)
{
    for (int i = 0; i < WHEEL_COUNT; ++i) {
        if (!CLOSE(g_wheels[i], 0.0f)) return 0;
    }
    return 1;
}

/* ---- watchdog --------------------------------------------------------- */

static void test_watchdog_timing(void)
{
    watchdog_t wd;
    watchdog_init(&wd, WATCHDOG_TIMEOUT_MS);

    CHECK(watchdog_check(&wd, 100000u) == 0, "must not trip before any line is seen");

    watchdog_line_received(&wd, 1000u);
    CHECK(watchdog_check(&wd, 1499u) == 0, "must not trip at 499 ms of silence");
    CHECK(watchdog_check(&wd, 1500u) == 1, "MUST trip at exactly 500 ms");
    CHECK(watchdog_check(&wd, 1600u) == 0, "trip edge must fire only once");
    CHECK(watchdog_tripped(&wd) == 1, "must stay tripped until a line arrives");

    watchdog_line_received(&wd, 1700u);
    CHECK(watchdog_tripped(&wd) == 0, "a line clears the trip so it can fire again");
    CHECK(watchdog_check(&wd, 2200u) == 1, "must be able to trip a second time");
}

static void test_watchdog_feeds_on_any_line(void)
{
    /* "no line of any kind for 500 ms" -- content is irrelevant, liveness is the point. */
    robot_t r; reset(&r);
    robot_on_line(&r, "GARBAGE NOT A COMMAND");
    g_now = 1400u;
    robot_tick(&r, g_now);
    CHECK(watchdog_tripped(&r.wd) == 0, "a malformed line must still feed the watchdog");
}

/* ---- discovery and reply lines ----------------------------------------- */

static void test_discovery_ping_is_answered_online(void)
{
    robot_t r; reset(&r);
    robot_on_line(&r, "{PING_robot}");
    CHECK(g_line_count == 1, "exactly one answer, got %d", g_line_count);
    CHECK(strcmp(g_lines[0], "{\"device\":\"STM32\",\"status\":\"online\"}") == 0,
          "wrong answer: %s", g_lines[0]);
    g_line_count = 0;
    robot_on_line(&r, "  {ping_ROBOT}  ");
    CHECK(g_line_count == 1, "case and padding must not matter");
}

static void test_reply_lines_are_swallowed_and_feed_the_watchdog(void)
{
    robot_t r; reset(&r);
    robot_on_line(&r, "{\"phase\":\"idle\",\"answer\":\"\",\"face\":\"random\"}");
    CHECK(g_line_count == 0, "a reply line gets no answer, got %s",
          g_line_count ? g_lines[0] : "");
    g_now = 1400u;
    robot_tick(&r, g_now);
    CHECK(watchdog_tripped(&r.wd) == 0, "a reply line is still a line: it feeds the watchdog");
    const int before = g_set_wheels_calls;
    robot_on_line(&r, "{\"answer\":\"DRIVE 1 0 0 5000\"}");
    CHECK(g_set_wheels_calls == before, "nothing inside a reply line is ever a command");
}

static void test_watchdog_survives_counter_wrap(void)
{
    watchdog_t wd;
    watchdog_init(&wd, WATCHDOG_TIMEOUT_MS);
    watchdog_line_received(&wd, 0xFFFFFF00u);          /* 256 ms before wrapping */
    CHECK(watchdog_check(&wd, 0x00000050u) == 0, "80 ms across the wrap is not a timeout");
    CHECK(watchdog_check(&wd, 0x000000FFu) == 1, "500 ms across the wrap IS a timeout");
}

/* ---- the mandated behaviour ------------------------------------------- */

static void test_silence_during_drive_zeroes_the_motors(void)
{
    robot_t r; reset(&r);
    robot_on_line(&r, "DRIVE 0.5 0.0 0.0 5000");
    CHECK(r.drive_active == 1, "drive should be running");
    CHECK(!wheels_all_zero(), "wheels should be turning");

    g_line_count = 0;
    g_now += WATCHDOG_TIMEOUT_MS;         /* Jetson dies here */
    robot_tick(&r, g_now);

    CHECK(wheels_all_zero(), "MOTORS MUST BE ZEROED after 500 ms of silence");
    CHECK(r.drive_active == 0, "the drive must be cancelled, not merely paused");
    CHECK(g_line_count == 1 && strcmp(g_lines[0], "ERR DRIVE watchdog") == 0,
          "should report the abort, got %s", g_line_count ? g_lines[0] : "(nothing)");
}

static void test_motion_does_not_auto_resume(void)
{
    robot_t r; reset(&r);
    robot_on_line(&r, "DRIVE 0.5 0.0 0.0 5000");
    g_now += WATCHDOG_TIMEOUT_MS;
    robot_tick(&r, g_now);
    CHECK(wheels_all_zero(), "precondition: motors stopped");

    /* Link returns. The original drive still had 4.5 s left on it. */
    for (int i = 0; i < 10; ++i) {
        g_now += HEARTBEAT_PERIOD_MS;
        robot_on_line(&r, "PING");
        robot_tick(&r, g_now);
        CHECK(wheels_all_zero(), "MOTION MUST NOT AUTO-RESUME when the link returns");
    }
    CHECK(r.drive_active == 0, "no drive should be active after recovery");

    robot_on_line(&r, "DRIVE 0.2 0.0 0.0 1000");
    CHECK(!wheels_all_zero(), "a fresh DRIVE is what resumes motion");
}

static void test_drive_expiry_auto_stops(void)
{
    robot_t r; reset(&r);
    robot_on_line(&r, "DRIVE 0.5 0.0 0.0 1000");
    g_line_count = 0;

    g_now += 900u;
    robot_on_line(&r, "PING");            /* keep the watchdog fed */
    robot_tick(&r, g_now);
    CHECK(!wheels_all_zero(), "still driving before the duration elapses");

    g_now += 100u;
    robot_on_line(&r, "PING");
    robot_tick(&r, g_now);
    CHECK(wheels_all_zero(), "STM32 must auto-stop when the duration expires");

    int saw_done = 0;
    for (int i = 0; i < g_line_count; ++i) {
        if (strcmp(g_lines[i], "DONE DRIVE") == 0) saw_done = 1;
    }
    CHECK(saw_done, "expiry must emit DONE DRIVE");
}

/* ---- protocol --------------------------------------------------------- */

static void test_documented_commands_parse(void)
{
    command_t c;
    CHECK(protocol_parse("DRIVE 0.5 0.0 0.0 1500", &c) && c.kind == CMD_DRIVE &&
          CLOSE(c.vx, 0.5f) && c.duration_ms == 1500u, "DRIVE example from the doc");
    CHECK(protocol_parse("DRIVE 0.0 0.3 0.0 1000", &c) && CLOSE(c.vy, 0.3f), "strafe example");
    CHECK(protocol_parse("DRIVE 0.0 0.0 1.2 800", &c) && CLOSE(c.omega, 1.2f), "rotate example");
    CHECK(protocol_parse("FACE happy", &c) && c.kind == CMD_FACE &&
          strcmp(c.arg, "happy") == 0, "FACE example");
    CHECK(protocol_parse("HAND wave", &c) && c.kind == CMD_HAND, "HAND example");
    CHECK(protocol_parse("STOP", &c) && c.kind == CMD_STOP, "STOP example");
    CHECK(protocol_parse("PING", &c) && c.kind == CMD_PING, "PING");
    CHECK(protocol_parse("STATUS", &c) && c.kind == CMD_STATUS, "STATUS");
    CHECK(protocol_parse("DRIVE -0.5 -0.25 -1.0 250", &c) && CLOSE(c.vx, -0.5f),
          "negative velocities are legal");
    CHECK(protocol_parse("drive 0.1 0 0 100", &c), "lowercase accepted for hand testing");
    CHECK(protocol_parse("PING\r", &c), "a trailing CR must not break parsing");
}

static void test_malformed_commands_are_rejected(void)
{
    command_t c;
    CHECK(!protocol_parse("FLY 1 2 3", &c) && strcmp(c.error, "unknown") == 0, "unknown verb");
    CHECK(!protocol_parse("DRIVE 0.5 0.0", &c) && strcmp(c.error, "badargs") == 0, "too few args");
    CHECK(!protocol_parse("DRIVE 0.5 0.0 0.0 100 extra", &c), "trailing junk rejected");
    CHECK(!protocol_parse("DRIVE x y z w", &c) && strcmp(c.error, "badargs") == 0, "non-numeric");
    CHECK(!protocol_parse("DRIVE 99 0 0 100", &c) && strcmp(c.error, "range") == 0,
          "velocity above the limit rejected");
    CHECK(!protocol_parse("DRIVE 0.1 0 0 999999", &c) && strcmp(c.error, "range") == 0,
          "duration above the limit rejected");
    CHECK(!protocol_parse("DRIVE 0.1 0 0 0", &c), "zero duration rejected");
    CHECK(!protocol_parse("DRIVE nan 0 0 100", &c), "NaN must never reach the motors");
    CHECK(!protocol_parse("DRIVE inf 0 0 100", &c), "inf must never reach the motors");
    CHECK(!protocol_parse("STOP now", &c), "STOP takes no arguments");
    CHECK(!protocol_parse("FACE", &c), "FACE needs a name");
    CHECK(!protocol_parse("", &c) && strcmp(c.error, "empty") == 0, "empty line");
}

static void test_every_command_gets_exactly_one_response(void)
{
    /* "Every command gets a response." Without it the LLM announces "driving forward"
     * while a wheel is stalled and never finds out. */
    const char *lines[] = {
        "DRIVE 0.5 0.0 0.0 1000", "STOP", "FACE happy", "FACE sideways",
        "HAND wave", "HAND nope", "PING", "STATUS", "NONSENSE", "DRIVE bad"
    };
    for (unsigned i = 0; i < sizeof(lines) / sizeof(lines[0]); ++i) {
        robot_t r; reset(&r);
        robot_on_line(&r, lines[i]);
        CHECK(g_line_count == 1, "%s produced %d responses, expected 1",
              lines[i], g_line_count);
    }
}

static void test_response_formats(void)
{
    robot_t r; reset(&r);
    robot_on_line(&r, "PING");
    CHECK(strcmp(g_lines[0], "OK PING") == 0, "got '%s'", g_lines[0]);

    reset(&r);
    robot_on_line(&r, "FACE sideways");
    CHECK(strcmp(g_lines[0], "ERR FACE unknown") == 0, "got '%s'", g_lines[0]);

    reset(&r);
    robot_on_line(&r, "STATUS");
    CHECK(strcmp(g_lines[0], "STAT batt=12.34 motors=ok temp=31.5 up=1000") == 0,
          "STAT format from the doc, got '%s'", g_lines[0]);

    reset(&r);
    robot_on_line(&r, "HAND wave");
    CHECK(strcmp(g_lines[0], "OK HAND") == 0, "got '%s'", g_lines[0]);
    g_now += 800u;
    robot_on_line(&r, "PING");
    robot_tick(&r, g_now);
    int saw = 0;
    for (int i = 0; i < g_line_count; ++i) if (!strcmp(g_lines[i], "DONE HAND")) saw = 1;
    CHECK(saw, "a finished gesture must emit DONE HAND");
}

/* ---- WHEEL (bring-up diagnostic) -------------------------------------- */

static void test_wheel_command_isolates_one_motor(void)
{
    for (int i = 0; i < WHEEL_COUNT; ++i) {
        robot_t r; reset(&r);
        char line[64];
        snprintf(line, sizeof(line), "WHEEL %d 5.0 1000", i);
        robot_on_line(&r, line);
        CHECK(strcmp(g_lines[0], "OK WHEEL") == 0, "got '%s'", g_lines[0]);
        for (int j = 0; j < WHEEL_COUNT; ++j) {
            CHECK(CLOSE(g_wheels[j], i == j ? 5.0f : 0.0f),
                  "WHEEL %d must move only wheel %d, saw %.2f on %d", i, i, g_wheels[j], j);
        }
    }
}

static void test_wheel_command_validation(void)
{
    command_t c;
    CHECK(protocol_parse("WHEEL 0 5.0 1000", &c) && c.kind == CMD_WHEEL &&
          c.wheel_index == 0 && CLOSE(c.wheel_rad_s, 5.0f), "valid WHEEL");
    CHECK(protocol_parse("WHEEL 3 -5.0 500", &c) && c.wheel_index == 3,
          "negative speed is how the opposite direction is checked");
    CHECK(!protocol_parse("WHEEL 4 5.0 1000", &c) && strcmp(c.error, "range") == 0,
          "wheel index 4 does not exist");
    CHECK(!protocol_parse("WHEEL 0 999 1000", &c) && strcmp(c.error, "range") == 0,
          "speed above the limit rejected");
    CHECK(!protocol_parse("WHEEL 0 5.0", &c), "missing duration rejected");
}

static void test_wheel_obeys_the_watchdog_too(void)
{
    /* A bring-up command must be no less safe than a normal one. */
    robot_t r; reset(&r);
    robot_on_line(&r, "WHEEL 1 8.0 10000");
    CHECK(!wheels_all_zero(), "precondition: wheel turning");
    g_line_count = 0;
    g_now += WATCHDOG_TIMEOUT_MS;
    robot_tick(&r, g_now);
    CHECK(wheels_all_zero(), "WHEEL must be stopped by the watchdog like anything else");
    CHECK(g_line_count == 1 && strcmp(g_lines[0], "ERR WHEEL watchdog") == 0,
          "the abort must name WHEEL, not DRIVE; got '%s'",
          g_line_count ? g_lines[0] : "(nothing)");
}

static void test_expiry_names_the_right_verb(void)
{
    robot_t r; reset(&r);
    robot_on_line(&r, "WHEEL 2 5.0 1000");
    g_line_count = 0;
    g_now += 1000u;
    robot_on_line(&r, "PING");
    robot_tick(&r, g_now);
    int saw = 0;
    for (int i = 0; i < g_line_count; ++i) if (!strcmp(g_lines[i], "DONE WHEEL")) saw = 1;
    CHECK(saw, "expiry of a WHEEL must emit DONE WHEEL");
}

/* ---- kinematics ------------------------------------------------------- */

static void test_mecanum_sign_patterns(void)
{
    mecanum_config_t cfg;
    mecanum_default_config(&cfg);
    float w[WHEEL_COUNT];

    /* Pure forward: all four wheels turn the same way. */
    mecanum_solve(&cfg, 1.0f, 0.0f, 0.0f, w);
    CHECK(w[0] > 0 && w[1] > 0 && w[2] > 0 && w[3] > 0, "forward: all wheels positive");
    CHECK(CLOSE(w[0], w[1]) && CLOSE(w[1], w[2]) && CLOSE(w[2], w[3]),
          "forward: all wheels equal");

    /* Strafe: the diagonal pairs oppose -- the signature of mecanum. */
    mecanum_solve(&cfg, 0.0f, 1.0f, 0.0f, w);
    CHECK(w[WHEEL_FRONT_LEFT] < 0 && w[WHEEL_FRONT_RIGHT] > 0 &&
          w[WHEEL_REAR_LEFT] > 0 && w[WHEEL_REAR_RIGHT] < 0,
          "strafe: FL-,FR+,RL+,RR- per the doc's formulas");

    /* Rotate: left side opposes right side. */
    mecanum_solve(&cfg, 0.0f, 0.0f, 1.0f, w);
    CHECK(w[WHEEL_FRONT_LEFT] < 0 && w[WHEEL_REAR_LEFT] < 0 &&
          w[WHEEL_FRONT_RIGHT] > 0 && w[WHEEL_REAR_RIGHT] > 0,
          "rotate: left negative, right positive");

    mecanum_solve(&cfg, 0.0f, 0.0f, 0.0f, w);
    CHECK(CLOSE(w[0], 0) && CLOSE(w[1], 0) && CLOSE(w[2], 0) && CLOSE(w[3], 0),
          "zero command means zero wheels");
}

static void test_direction_multipliers_and_isolation(void)
{
    mecanum_config_t cfg;
    mecanum_default_config(&cfg);
    for (int i = 0; i < WHEEL_COUNT; ++i) {
        CHECK(CLOSE(cfg.direction[i], 1.0f), "defaults are +1 until verified on hardware");
    }

    float w[WHEEL_COUNT], flipped[WHEEL_COUNT];
    mecanum_solve(&cfg, 1.0f, 0.0f, 0.0f, w);
    cfg.direction[WHEEL_REAR_RIGHT] = -1.0f;
    mecanum_solve(&cfg, 1.0f, 0.0f, 0.0f, flipped);
    CHECK(CLOSE(flipped[WHEEL_REAR_RIGHT], -w[WHEEL_REAR_RIGHT]),
          "a flipped direction inverts only its own wheel");
    CHECK(CLOSE(flipped[WHEEL_FRONT_LEFT], w[WHEEL_FRONT_LEFT]), "others untouched");

    mecanum_default_config(&cfg);
    for (int i = 0; i < WHEEL_COUNT; ++i) {
        mecanum_wheel_only(&cfg, i, 5.0f, w);
        for (int j = 0; j < WHEEL_COUNT; ++j) {
            CHECK(CLOSE(w[j], i == j ? 5.0f : 0.0f),
                  "wheel_only(%d): wheel %d should be %s", i, j, i == j ? "5" : "0");
        }
    }
}

/* ---- PID ------------------------------------------------------------- */

static void test_pid_converges_and_saturates(void)
{
    robot_pid_t p;
    pid_init(&p, 0.5f, 2.0f, 0.0f, -1.0f, 1.0f);

    /* A crude first-order wheel: duty pushes velocity toward 10 rad/s. */
    float velocity = 0.0f;
    for (int i = 0; i < 500; ++i) {
        float duty = pid_update(&p, 10.0f, velocity, 0.01f);
        CHECK(duty >= -1.0f && duty <= 1.0f, "output must stay saturated in range");
        velocity += (duty * 12.0f - velocity) * 0.05f;
    }
    CHECK(fabsf(velocity - 10.0f) < 0.5f, "should converge, got %.2f", velocity);
}

static void test_pid_integral_does_not_wind_up_against_a_stall(void)
{
    robot_pid_t p;
    pid_init(&p, 0.5f, 2.0f, 0.0f, -1.0f, 1.0f);
    /* Wheel jammed: commanded 10 rad/s, measured 0, for a long time. */
    for (int i = 0; i < 1000; ++i) {
        (void)pid_update(&p, 10.0f, 0.0f, 0.01f);
    }
    /* Now it comes free and is already at target. Output must not still be pinned. */
    float out = pid_update(&p, 10.0f, 10.0f, 0.01f);
    CHECK(out < p.out_max, "windup: still commanding full duty (%.3f) after a stall", out);
}

static void test_pid_zero_setpoint_means_stop(void)
{
    robot_pid_t p;
    pid_init(&p, 0.5f, 2.0f, 0.0f, -1.0f, 1.0f);
    for (int i = 0; i < 100; ++i) (void)pid_update(&p, 5.0f, 0.0f, 0.01f);
    CHECK(CLOSE(pid_update(&p, 0.0f, 0.0f, 0.01f), 0.0f),
          "a stopped wheel at zero setpoint must command exactly zero");
}

int main(void)
{
    printf("firmware core -- native tests\n\n");
    test_watchdog_timing();
    test_watchdog_feeds_on_any_line();
    test_watchdog_survives_counter_wrap();
    test_discovery_ping_is_answered_online();
    test_reply_lines_are_swallowed_and_feed_the_watchdog();
    test_silence_during_drive_zeroes_the_motors();
    test_motion_does_not_auto_resume();
    test_drive_expiry_auto_stops();
    test_documented_commands_parse();
    test_malformed_commands_are_rejected();
    test_every_command_gets_exactly_one_response();
    test_response_formats();
    test_wheel_command_isolates_one_motor();
    test_wheel_command_validation();
    test_wheel_obeys_the_watchdog_too();
    test_expiry_names_the_right_verb();
    test_mecanum_sign_patterns();
    test_direction_multipliers_and_isolation();
    test_pid_converges_and_saturates();
    test_pid_integral_does_not_wind_up_against_a_stall();
    test_pid_zero_setpoint_means_stop();

    printf("\n%d checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}

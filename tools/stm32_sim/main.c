/* stm32_sim -- run the real firmware core on the host, behind a pseudo-terminal.
 *
 * This is NOT a reimplementation of the protocol. It compiles and links the exact
 * files that ship to the STM32 (protocol.c, mecanum.c, watchdog.c, robot.c) and only
 * supplies a board_io_t whose "motors" are variables instead of PWM channels. So a
 * test driven through this binary is a test of the firmware's actual logic -- the
 * watchdog that stops the wheels here is the same code that will stop them on silicon.
 *
 * It exists because the STM32 is not yet attached (see ROADMAP M5). It is a substitute
 * for the microcontroller, not for the hardware bring-up.
 *
 *   ./stm32_sim [--state PATH] [--port-file PATH]
 *
 * Prints the PTY slave path on stdout. Point the Jetson side at that path exactly as
 * it would point at /dev/ttyACM0.
 */
#define _XOPEN_SOURCE 600
#define _DEFAULT_SOURCE   /* cfmakeraw */
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <termios.h>
#include <unistd.h>

#include "protocol.h"
#include "robot.h"

static int g_master = -1;
static volatile sig_atomic_t g_running = 1;
static float g_wheels[WHEEL_COUNT];
static char g_face[PROTOCOL_MAX_TOKEN] = "neutral";
static const char *g_state_path = NULL;
static uint32_t g_t0_ms;

static uint32_t millis_now(void)
{
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return (uint32_t)(tv.tv_sec * 1000u + tv.tv_usec / 1000u);
}

/* Written on every motor change so a test can assert on the wheels without parsing
 * the serial stream. This is observability for the harness, not part of the protocol. */
static void write_state(const char *event)
{
    if (!g_state_path) return;
    char tmp[512];
    snprintf(tmp, sizeof(tmp), "%s.tmp", g_state_path);
    FILE *f = fopen(tmp, "w");
    if (!f) return;
    int moving = 0;
    for (int i = 0; i < WHEEL_COUNT; ++i) {
        if (g_wheels[i] > 1e-6f || g_wheels[i] < -1e-6f) moving = 1;
    }
    fprintf(f,
            "{\"t_ms\":%u,\"event\":\"%s\",\"moving\":%s,\"face\":\"%s\","
            "\"wheels\":{\"front_left\":%.4f,\"front_right\":%.4f,"
            "\"rear_left\":%.4f,\"rear_right\":%.4f}}\n",
            millis_now() - g_t0_ms, event, moving ? "true" : "false", g_face,
            (double)g_wheels[0], (double)g_wheels[1],
            (double)g_wheels[2], (double)g_wheels[3]);
    fclose(f);
    rename(tmp, g_state_path);   /* atomic, so a reader never sees a half-written file */
}

static void sim_set_wheels(const float w[WHEEL_COUNT])
{
    memcpy(g_wheels, w, sizeof(g_wheels));
    int moving = 0;
    for (int i = 0; i < WHEEL_COUNT; ++i) {
        if (w[i] > 1e-6f || w[i] < -1e-6f) moving = 1;
    }
    fprintf(stderr, "[motors] FL=%7.2f FR=%7.2f RL=%7.2f RR=%7.2f  %s\n",
            (double)w[0], (double)w[1], (double)w[2], (double)w[3],
            moving ? "TURNING" : "*** STOPPED ***");
    fflush(stderr);
    write_state("set_wheels");
}

static int sim_set_face(const char *name)
{
    static const char *known[] = { "happy", "thinking", "listening",
                                   "speaking", "sad", "neutral", NULL };
    for (int i = 0; known[i]; ++i) {
        if (strcmp(known[i], name) == 0) {
            snprintf(g_face, sizeof(g_face), "%s", name);
            write_state("set_face");
            return 0;
        }
    }
    return 1;
}

static int sim_gesture(const char *name, uint32_t *duration_ms)
{
    static const struct { const char *n; uint32_t ms; } known[] = {
        { "wave", 1200u }, { "point", 800u }, { "home", 600u }, { "nod", 700u },
    };
    for (unsigned i = 0; i < sizeof(known) / sizeof(known[0]); ++i) {
        if (strcmp(known[i].n, name) == 0) { *duration_ms = known[i].ms; return 0; }
    }
    return 1;
}

static void sim_emit(const char *line)
{
    if (g_master < 0) return;
    char out[PROTOCOL_MAX_LINE + 2];
    int n = snprintf(out, sizeof(out), "%s\n", line);
    ssize_t ignored = write(g_master, out, (size_t)n);
    (void)ignored;
    fprintf(stderr, "[tx] %s\n", line);
    fflush(stderr);
}

static uint32_t sim_now(void) { return millis_now() - g_t0_ms; }
static float sim_batt(void) { return 12.10f; }
static int sim_stall(void) { return 0; }
static float sim_temp(void) { return 29.0f; }

static void on_signal(int sig) { (void)sig; g_running = 0; }

int main(int argc, char **argv)
{
    const char *port_file = NULL;
    for (int i = 1; i < argc; ++i) {
        if (!strcmp(argv[i], "--state") && i + 1 < argc) g_state_path = argv[++i];
        else if (!strcmp(argv[i], "--port-file") && i + 1 < argc) port_file = argv[++i];
    }

    g_master = posix_openpt(O_RDWR | O_NOCTTY);
    if (g_master < 0 || grantpt(g_master) || unlockpt(g_master)) {
        perror("posix_openpt");
        return 1;
    }
    const char *slave = ptsname(g_master);
    if (!slave) { perror("ptsname"); return 1; }

    /* Raw mode: the protocol is newline-framed and must not be mangled by line
     * discipline echo or CR/LF translation. */
    struct termios tio;
    if (tcgetattr(g_master, &tio) == 0) {
        cfmakeraw(&tio);
        tcsetattr(g_master, TCSANOW, &tio);
    }
    /* Hold the slave open ourselves so the PTY survives the Jetson process being
     * killed -- which is precisely the scenario under test. */
    int slave_fd = open(slave, O_RDWR | O_NOCTTY);

    printf("%s\n", slave);
    fflush(stdout);
    if (port_file) {
        FILE *f = fopen(port_file, "w");
        if (f) { fprintf(f, "%s\n", slave); fclose(f); }
    }

    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);

    g_t0_ms = millis_now();
    const board_io_t io = { sim_set_wheels, sim_set_face, sim_gesture, sim_emit,
                            sim_now, sim_batt, sim_stall, sim_temp };
    robot_t robot;
    robot_init(&robot, &io);
    write_state("boot");

    fprintf(stderr, "[sim] firmware core running on %s (watchdog %u ms)\n",
            slave, WATCHDOG_TIMEOUT_MS);

    char line[PROTOCOL_MAX_LINE];
    size_t len = 0;
    while (g_running) {
        struct pollfd pfd = { g_master, POLLIN, 0 };
        /* Poll briefly so robot_tick still runs when nothing is arriving -- the
         * watchdog only fires from the tick, exactly as on the real main loop. */
        int rc = poll(&pfd, 1, 5);
        if (rc > 0 && (pfd.revents & POLLIN)) {
            char buf[256];
            ssize_t n = read(g_master, buf, sizeof(buf));
            for (ssize_t i = 0; i < n; ++i) {
                char c = buf[i];
                if (c == '\n' || c == '\r') {
                    if (len > 0) {
                        line[len] = '\0';
                        fprintf(stderr, "[rx] %s\n", line);
                        fflush(stderr);
                        robot_on_line(&robot, line);
                        len = 0;
                    }
                } else if (len + 1 < sizeof(line)) {
                    line[len++] = c;
                } else {
                    len = 0;   /* overlong line: resynchronise at the next newline */
                }
            }
        }
        robot_tick(&robot, sim_now());
    }

    robot_all_stop(&robot);
    write_state("shutdown");
    if (slave_fd >= 0) close(slave_fd);
    close(g_master);
    return 0;
}

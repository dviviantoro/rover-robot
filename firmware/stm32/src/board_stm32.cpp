/* board_stm32.cpp -- the hardware half. Everything target-specific lives here.
 *
 * ⚠ UNVERIFIED ON HARDWARE. No STM32 has been attached to this project yet, so this
 * file compiles but has never run. The portable core it serves (protocol/watchdog/
 * mecanum/pid) IS tested -- see run_native_tests.sh and tools/stm32_sim.
 *
 * Bring-up order, once a board exists:
 *   1. Motors disconnected. Check PWM and direction pins with a scope or LEDs.
 *   2. One wheel at a time, off the ground: scripts/serial/verify_wheels.py.
 *      Record the sign convention in serial-protocol.md before trusting DRIVE.
 *   3. Encoders: confirm counts rise when a wheel is turned the positive way.
 *   4. Only then composite motion.
 */
#include <Arduino.h>
#include <Servo.h>
#include <Wire.h>

extern "C" {
#include "robot_board.h"
#include "pid.h"
#include "robot_pins.h"
}

namespace {

struct MotorPins { uint32_t pwm, in1, in2; };

const MotorPins MOTORS[WHEEL_COUNT] = {
    { PIN_M0_PWM, PIN_M0_IN1, PIN_M0_IN2 },
    { PIN_M1_PWM, PIN_M1_IN1, PIN_M1_IN2 },
    { PIN_M2_PWM, PIN_M2_IN1, PIN_M2_IN2 },
    { PIN_M3_PWM, PIN_M3_IN1, PIN_M3_IN2 },
};

const uint32_t ENCODER_A[WHEEL_COUNT] = { PIN_ENC0_A, PIN_ENC1_A, PIN_ENC2_A, PIN_ENC3_A };
const uint32_t ENCODER_B[WHEEL_COUNT] = { PIN_ENC0_B, PIN_ENC1_B, PIN_ENC2_B, PIN_ENC3_B };

volatile int32_t g_ticks[WHEEL_COUNT];
int32_t g_ticks_prev[WHEEL_COUNT];
float g_target[WHEEL_COUNT];      /* rad/s, from the core */
float g_measured[WHEEL_COUNT];    /* rad/s, from the encoders */
robot_pid_t g_pid[WHEEL_COUNT];
uint32_t g_last_control_us;
bool g_stalled;

Servo g_shoulder, g_elbow;

template <int N> void isr_encoder()
{
    /* Quadrature: B's level at A's edge gives the direction. */
    g_ticks[N] += (digitalRead(ENCODER_B[N]) == HIGH) ? 1 : -1;
}

void apply_duty(int wheel, float duty)
{
    const MotorPins &m = MOTORS[wheel];
    if (duty > 1.0f) duty = 1.0f;
    if (duty < -1.0f) duty = -1.0f;

    if (duty > 0.01f) {
        digitalWrite(m.in1, HIGH);
        digitalWrite(m.in2, LOW);
    } else if (duty < -0.01f) {
        digitalWrite(m.in1, LOW);
        digitalWrite(m.in2, HIGH);
    } else {
        /* Brake: both low. Coasting would let the robot roll on after a stop. */
        digitalWrite(m.in1, LOW);
        digitalWrite(m.in2, LOW);
    }
    analogWrite(m.pwm, (int)(fabsf(duty) * 255.0f));
}

/* ---- board_io_t implementation --------------------------------------- */

void hw_set_wheels(const float w[WHEEL_COUNT])
{
    bool all_zero = true;
    for (int i = 0; i < WHEEL_COUNT; ++i) {
        g_target[i] = w[i];
        if (w[i] > 1e-6f || w[i] < -1e-6f) all_zero = false;
    }
    if (all_zero) {
        /* Cut power at the driver as well as commanding zero. The watchdog calls this
         * path, and it must not depend on the control loop still running to take
         * effect -- if the loop has wedged, STBY low still stops the robot. */
        digitalWrite(PIN_MOTOR_STBY, LOW);
        for (int i = 0; i < WHEEL_COUNT; ++i) {
            pid_reset(&g_pid[i]);
            apply_duty(i, 0.0f);
        }
    } else {
        digitalWrite(PIN_MOTOR_STBY, HIGH);
    }
}

int hw_set_face(const char *name)
{
    /* 8x8 bitmaps, one row per byte. Deliberately crude -- it is a face, not a display. */
    static const uint8_t FACES[][8] = {
        { 0x00, 0x66, 0x66, 0x00, 0x81, 0x42, 0x3C, 0x00 },  /* happy */
        { 0x00, 0x66, 0x66, 0x00, 0x00, 0x3C, 0x00, 0x00 },  /* neutral */
        { 0x00, 0x66, 0x66, 0x00, 0x3C, 0x42, 0x81, 0x00 },  /* sad */
        { 0x00, 0x42, 0x66, 0x00, 0x10, 0x30, 0x10, 0x00 },  /* thinking */
        { 0x00, 0x7E, 0x42, 0x00, 0x18, 0x24, 0x18, 0x00 },  /* listening */
        { 0x00, 0x66, 0x66, 0x00, 0x3C, 0x00, 0x3C, 0x00 },  /* speaking */
    };
    static const char *NAMES[] = { "happy", "neutral", "sad",
                                   "thinking", "listening", "speaking" };
    for (unsigned i = 0; i < sizeof(NAMES) / sizeof(NAMES[0]); ++i) {
        if (strcmp(NAMES[i], name) == 0) {
            Wire.beginTransmission(LED_MATRIX_I2C_ADDR);
            Wire.write(0x00);
            for (int row = 0; row < 8; ++row) {
                Wire.write(FACES[i][row]);
                Wire.write(0x00);
            }
            Wire.endTransmission();
            return 0;
        }
    }
    return 1;   /* unknown -> the core answers ERR FACE unknown */
}

int hw_start_gesture(const char *name, uint32_t *duration_ms)
{
    /* Preset sequences only. PROJECT_BRIEF.md § Non-goals rules out real arm IK. */
    if (strcmp(name, "wave") == 0)  { g_shoulder.write(120); g_elbow.write(60);  *duration_ms = 1200; return 0; }
    if (strcmp(name, "point") == 0) { g_shoulder.write(90);  g_elbow.write(170); *duration_ms = 800;  return 0; }
    if (strcmp(name, "nod") == 0)   { g_shoulder.write(70);  g_elbow.write(90);  *duration_ms = 700;  return 0; }
    if (strcmp(name, "home") == 0)  { g_shoulder.write(90);  g_elbow.write(90);  *duration_ms = 600;  return 0; }
    return 1;
}

void hw_emit(const char *line)
{
    Serial.print(line);
    Serial.print('\n');
}

uint32_t hw_now_ms(void) { return millis(); }

float hw_battery_volts(void)
{
    const float counts = (float)analogRead(PIN_VBAT_SENSE);
    return (counts / 4095.0f) * 3.3f * VBAT_DIVIDER_RATIO;
}

int hw_motors_stalled(void) { return g_stalled ? 1 : 0; }

float hw_temperature_c(void)
{
    /* Placeholder until a sensor is fitted. Reported as-is in STAT rather than faked. */
    return 0.0f;
}

}  // namespace

extern "C" const board_io_t *board_stm32_io(void)
{
    static const board_io_t io = {
        hw_set_wheels, hw_set_face, hw_start_gesture, hw_emit,
        hw_now_ms, hw_battery_volts, hw_motors_stalled, hw_temperature_c,
    };
    return &io;
}

extern "C" void board_stm32_begin(void)
{
    Serial.begin(115200);

    pinMode(PIN_MOTOR_STBY, OUTPUT);
    digitalWrite(PIN_MOTOR_STBY, LOW);          /* motors off before anything else */

    for (int i = 0; i < WHEEL_COUNT; ++i) {
        pinMode(MOTORS[i].pwm, OUTPUT);
        pinMode(MOTORS[i].in1, OUTPUT);
        pinMode(MOTORS[i].in2, OUTPUT);
        apply_duty(i, 0.0f);
        pinMode(ENCODER_A[i], INPUT_PULLUP);
        pinMode(ENCODER_B[i], INPUT_PULLUP);
        /* Gains are a starting point; tune on the bench with one wheel at a time. */
        pid_init(&g_pid[i], 0.020f, 0.120f, 0.0f, -1.0f, 1.0f);
        g_target[i] = 0.0f;
    }
    attachInterrupt(digitalPinToInterrupt(ENCODER_A[0]), isr_encoder<0>, RISING);
    attachInterrupt(digitalPinToInterrupt(ENCODER_A[1]), isr_encoder<1>, RISING);
    attachInterrupt(digitalPinToInterrupt(ENCODER_A[2]), isr_encoder<2>, RISING);
    attachInterrupt(digitalPinToInterrupt(ENCODER_A[3]), isr_encoder<3>, RISING);

    g_shoulder.attach(PIN_SERVO_SHOULDER);
    g_elbow.attach(PIN_SERVO_ELBOW);
    g_shoulder.write(90);
    g_elbow.write(90);

    Wire.begin();
    hw_set_face("neutral");

    g_last_control_us = micros();
}

/* Fixed-rate velocity control. Called from loop() as often as possible; runs its work
 * at CONTROL_HZ. Kept out of an ISR so it can never delay USB reception -- a late
 * control tick is a wobble, a late received line is a missed watchdog feed. */
extern "C" void board_stm32_control_tick(void)
{
    const uint32_t now_us = micros();
    const uint32_t period_us = 1000000u / CONTROL_HZ;
    if ((uint32_t)(now_us - g_last_control_us) < period_us) {
        return;
    }
    const float dt = (float)(now_us - g_last_control_us) / 1000000.0f;
    g_last_control_us = now_us;

    bool any_stalled = false;
    for (int i = 0; i < WHEEL_COUNT; ++i) {
        noInterrupts();
        const int32_t ticks = g_ticks[i];
        interrupts();
        const int32_t delta = ticks - g_ticks_prev[i];
        g_ticks_prev[i] = ticks;

        g_measured[i] = ((float)delta / ENCODER_COUNTS_PER_REV) * 6.28318531f / dt;

        if (g_target[i] == 0.0f) {
            pid_reset(&g_pid[i]);
            apply_duty(i, 0.0f);
            continue;
        }
        /* Commanded to move but the encoder says otherwise: report it so the
         * orchestrator can say "I'm stuck" instead of claiming to be driving. */
        if (fabsf(g_measured[i]) < 0.2f && fabsf(g_target[i]) > 1.0f) {
            any_stalled = true;
        }
        apply_duty(i, pid_update(&g_pid[i], g_target[i], g_measured[i], dt));
    }
    g_stalled = any_stalled;
}

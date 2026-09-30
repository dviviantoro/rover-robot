/* robot_pins.h -- the wiring map. THE file to edit when the harness changes.
 *
 * ⚠ UNVERIFIED. Nothing here has been checked against real hardware -- no STM32 has
 * been attached to this project yet. Every pin below is a plausible default for a
 * WeAct Black Pill (STM32F411CE) driving two TB6612FNG dual motor drivers. Confirm
 * each one against your own board before applying power, and re-run the wheel
 * direction procedure in serial-protocol.md afterwards.
 */
#ifndef ROBOT_PINS_H
#define ROBOT_PINS_H

/* Motor driver: one PWM + two direction lines per wheel (TB6612FNG style).
 * Order matches board.h: front-left, front-right, rear-left, rear-right. */
#define PIN_M0_PWM   PB0
#define PIN_M0_IN1   PB1
#define PIN_M0_IN2   PB2

#define PIN_M1_PWM   PB3
#define PIN_M1_IN1   PB4
#define PIN_M1_IN2   PB5

#define PIN_M2_PWM   PB6
#define PIN_M2_IN1   PB7
#define PIN_M2_IN2   PB8

#define PIN_M3_PWM   PB9
#define PIN_M3_IN1   PB12
#define PIN_M3_IN2   PB13

/* Driver standby -- pulled low to cut all motor outputs at the hardware level.
 * The watchdog drives this as well as zeroing PWM, so a stuck timer cannot keep
 * the robot moving. */
#define PIN_MOTOR_STBY PB14

/* Quadrature encoders: A on an interrupt-capable pin, B read directly. */
#define PIN_ENC0_A   PA0
#define PIN_ENC0_B   PA1
#define PIN_ENC1_A   PA2
#define PIN_ENC1_B   PA3
#define PIN_ENC2_A   PA4
#define PIN_ENC2_B   PA5
#define PIN_ENC3_A   PA6
#define PIN_ENC3_B   PA7

/* Servos for the preset hand gestures. */
#define PIN_SERVO_SHOULDER PA8
#define PIN_SERVO_ELBOW    PA9

/* LED matrix face (I2C, e.g. HT16K33 8x8). */
#define PIN_I2C_SDA  PB11
#define PIN_I2C_SCL  PB10
#define LED_MATRIX_I2C_ADDR 0x70

/* Battery sense divider. */
#define PIN_VBAT_SENSE PA10
#define VBAT_DIVIDER_RATIO 11.0f   /* 10k:1k -> 0..36 V into 0..3.3 V */

/* Encoder counts per wheel revolution, after gearing and 4x quadrature decoding.
 * Wrong here means every velocity is wrong by a constant factor. Measure it: turn a
 * wheel exactly one revolution by hand and read the count. */
#define ENCODER_COUNTS_PER_REV 1440.0f

/* Control loop rate. Fast enough for smooth velocity control, slow enough that the
 * encoder counts per tick are not dominated by quantisation. */
#define CONTROL_HZ 100u

#endif /* ROBOT_PINS_H */

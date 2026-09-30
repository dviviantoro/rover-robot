/* pid.h -- per-wheel velocity PID.
 *
 * AGENTS.md constraint 6: the STM32 owns the real-time control loops. The Jetson
 * sends a body velocity; these loops are what actually make each wheel hold it.
 *
 * Portable C with no HAL so it can be tested natively -- integral windup and output
 * saturation are exactly the kind of bug that is miserable to find on a bench and
 * trivial to find in a test.
 */
#ifndef ROBOT_PID_H
#define ROBOT_PID_H

/* Named robot_pid_t, not pid_t: POSIX already owns `pid_t` for process IDs, and
 * newlib drags sys/types.h in under the Arduino framework. */
typedef struct {
    float kp, ki, kd;
    float out_min, out_max;
    float integral;
    float prev_error;
    int   primed;      /* false until the first update, so kd sees no fake step */
} robot_pid_t;

void pid_init(robot_pid_t *p, float kp, float ki, float kd, float out_min, float out_max);
void pid_reset(robot_pid_t *p);

/* setpoint and measurement in rad/s, dt in seconds. Returns a duty in
 * [out_min, out_max]. Integration stops while the output is saturated, so the
 * integral cannot wind up against a stalled wheel. */
float pid_update(robot_pid_t *p, float setpoint, float measured, float dt);

#endif /* ROBOT_PID_H */

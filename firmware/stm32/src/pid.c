#include "pid.h"

void pid_init(robot_pid_t *p, float kp, float ki, float kd, float out_min, float out_max)
{
    p->kp = kp; p->ki = ki; p->kd = kd;
    p->out_min = out_min; p->out_max = out_max;
    pid_reset(p);
}

void pid_reset(robot_pid_t *p)
{
    p->integral = 0.0f;
    p->prev_error = 0.0f;
    p->primed = 0;
}

float pid_update(robot_pid_t *p, float setpoint, float measured, float dt)
{
    if (dt <= 0.0f) {
        return 0.0f;
    }
    const float error = setpoint - measured;

    /* A zero setpoint means stop. Bleed the integral away rather than letting it hold
     * torque against a wheel that is supposed to be still -- this is what the watchdog
     * relies on when it commands all-zero. */
    if (setpoint == 0.0f && measured == 0.0f) {
        pid_reset(p);
        return 0.0f;
    }

    const float derivative = p->primed ? (error - p->prev_error) / dt : 0.0f;
    p->prev_error = error;
    p->primed = 1;

    const float candidate_i = p->integral + error * dt;
    float out = p->kp * error + p->ki * candidate_i + p->kd * derivative;

    /* Conditional integration: only accumulate when doing so does not push further
     * into saturation. Cheap, and it removes the classic windup failure where a wheel
     * jammed against furniture spins up the moment it comes free. */
    if (out > p->out_max) {
        out = p->out_max;
        if (error < 0.0f) p->integral = candidate_i;
    } else if (out < p->out_min) {
        out = p->out_min;
        if (error > 0.0f) p->integral = candidate_i;
    } else {
        p->integral = candidate_i;
    }
    return out;
}

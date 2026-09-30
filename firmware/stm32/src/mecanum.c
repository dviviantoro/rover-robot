#include "mecanum.h"

#include <string.h>

void mecanum_default_config(mecanum_config_t *cfg)
{
    /* Placeholder geometry -- measure the actual chassis and update. The wheel
     * velocities scale with these, so wrong numbers mean the robot moves at the
     * wrong speed, not that it moves wrongly. */
    cfg->wheel_radius_m = 0.048f;
    cfg->half_width_m = 0.085f;
    cfg->half_length_m = 0.095f;
    for (int i = 0; i < WHEEL_COUNT; ++i) {
        cfg->direction[i] = 1.0f;
    }
}

void mecanum_solve(const mecanum_config_t *cfg,
                   float vx, float vy, float omega,
                   float out[WHEEL_COUNT])
{
    const float k = cfg->half_width_m + cfg->half_length_m;
    const float r = cfg->wheel_radius_m;

    /* Exactly as written in serial-protocol.md. Do not "simplify" these: the sign
     * pattern is the whole content of mecanum kinematics. */
    out[WHEEL_FRONT_LEFT]  = (vx - vy - k * omega) / r;
    out[WHEEL_FRONT_RIGHT] = (vx + vy + k * omega) / r;
    out[WHEEL_REAR_LEFT]   = (vx + vy - k * omega) / r;
    out[WHEEL_REAR_RIGHT]  = (vx - vy + k * omega) / r;

    for (int i = 0; i < WHEEL_COUNT; ++i) {
        out[i] *= cfg->direction[i];
    }
}

void mecanum_wheel_only(const mecanum_config_t *cfg,
                        int wheel, float rad_per_s,
                        float out[WHEEL_COUNT])
{
    memset(out, 0, sizeof(float) * WHEEL_COUNT);
    if (wheel >= 0 && wheel < WHEEL_COUNT) {
        out[wheel] = rad_per_s * cfg->direction[wheel];
    }
}

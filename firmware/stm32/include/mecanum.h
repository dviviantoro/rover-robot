/* mecanum.h -- inverse kinematics: body velocity -> four wheel velocities.
 *
 * This is STM32-side work by design: AGENTS.md constraint 6 says the Jetson sends
 * intent and never computes wheel speeds.
 *
 * The formulas are transcribed exactly from
 * dev-docs/architecture/serial-protocol.md § Mecanum kinematics reference.
 * That doc also warns that sign conventions depend on motor wiring and mounting, and
 * that each wheel must be verified individually before composite motion is trusted --
 * see mecanum_wheel_only() and scripts/serial/verify_wheels.py.
 */
#ifndef ROBOT_MECANUM_H
#define ROBOT_MECANUM_H

#include "robot_board.h"

typedef struct {
    float wheel_radius_m;   /* r  */
    float half_width_m;     /* lx -- lateral centre-to-wheel */
    float half_length_m;    /* ly -- longitudinal centre-to-wheel */
    /* Per-wheel direction multiplier, +1 or -1. Populated by the individual-wheel
     * verification and recorded in serial-protocol.md. Until that has been done on
     * real hardware every entry is +1, which is an assumption, not a measurement. */
    float direction[WHEEL_COUNT];
} mecanum_config_t;

void mecanum_default_config(mecanum_config_t *cfg);

/* (vx, vy, omega) in m/s, m/s, rad/s -> per-wheel rad/s. */
void mecanum_solve(const mecanum_config_t *cfg,
                   float vx, float vy, float omega,
                   float out[WHEEL_COUNT]);

/* Spin exactly one wheel, bypassing the kinematics.
 * This is the primitive the direction-verification procedure uses: composite motion
 * cannot be debugged until each wheel is known to turn the right way on its own. */
void mecanum_wheel_only(const mecanum_config_t *cfg,
                        int wheel, float rad_per_s,
                        float out[WHEEL_COUNT]);

#endif /* ROBOT_MECANUM_H */

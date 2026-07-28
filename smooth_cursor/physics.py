"""Cursor inertia physics."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class InertiaState:
    angle_deg: float = 0.0
    angular_vel: float = 0.0
    last_x: float | None = None
    last_y: float | None = None
    last_vx: float = 0.0
    last_vy: float = 0.0
    velocity_to_angle: float = 0.05
    max_angle: float = 40.0
    spring: float = 85.0
    damping: float = 12.0
    impulse: float = 0.014


def update_inertia(state: InertiaState, x: float, y: float, dt: float) -> float:
    if dt <= 0:
        return state.angle_deg
    dt = min(dt, 0.05)
    if state.last_x is None:
        state.last_x, state.last_y = x, y
        return state.angle_deg

    prev_x, prev_y = state.last_x, state.last_y
    vx = (x - prev_x) / dt
    vy = (y - prev_y) / dt
    ax = (vx - state.last_vx) / dt

    target = max(-state.max_angle, min(state.max_angle, -vx * state.velocity_to_angle))
    state.angular_vel += -ax * state.impulse * dt * 60.0
    accel = state.spring * (target - state.angle_deg) - state.damping * state.angular_vel
    state.angular_vel += accel * dt
    state.angle_deg += state.angular_vel * dt
    state.angle_deg = max(-state.max_angle * 1.35, min(state.max_angle * 1.35, state.angle_deg))

    state.last_x, state.last_y = x, y
    state.last_vx, state.last_vy = vx, vy
    return state.angle_deg

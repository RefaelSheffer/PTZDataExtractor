"""Core math for mapping image pixels to ground coordinates.

Provides dataclasses for camera parameters and pure functions for
ray casting and ground intersection that are independent from any UI
framework.  These helpers allow unit testing of the geometric logic
without requiring Qt or other heavy dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, Tuple
import math
import numpy as np


@dataclass
class Intrinsics:
    """Basic camera intrinsics with focal lengths and principal point."""

    width: int
    height: int
    fx: float
    fy: float
    cx: float
    cy: float

    @classmethod
    def from_hfov(cls, width: int, height: int, hfov_deg: float) -> "Intrinsics":
        """Create intrinsics from an image size and horizontal FOV."""

        fx = (width / 2.0) / math.tan(math.radians(hfov_deg) / 2.0)
        return cls(width, height, fx, fx, width / 2.0, height / 2.0)


@dataclass
class Extrinsics:
    """Camera position and orientation in an ENU-like world frame."""
    x: float
    y: float
    z: float
    yaw: float
    pitch: float
    roll: float
    epsg: int


@dataclass
class PTZ:
    """Pan/tilt/zoom offsets from the base extrinsic orientation."""
    pan: Optional[float] = None
    tilt: Optional[float] = None
    zoom: Optional[float] = None


class DemSampler(Protocol):
    """Minimal interface for sampling a DEM/DTM surface."""

    def elevation(self, x: float, y: float) -> Optional[float]:
        """Return ground elevation (meters) at the projected coordinate.

        ``None`` is returned when the coordinate is outside of the DEM
        or when no data is available at the location.
        """
        ...


def _rotation_matrix(yaw_deg: float, pitch_deg: float, roll_deg: float) -> np.ndarray:
    """Construct a world←camera rotation matrix.

    The convention follows typical PTZ cameras with yaw (pan) around the
    vertical ``Z`` axis, pitch (tilt) around ``Y`` and roll around ``X``.
    Angles are specified in degrees.  The camera frame assumes ``+Z`` is
    forward, ``+X`` to the right and ``+Y`` down.  To align this with a
    world frame where ``+X`` is east, ``+Y`` is north and ``+Z`` is up, a
    fixed rotation is applied that maps a forward-looking camera to point
    north at the horizon when all angles are zero.
    """

    yaw_rad = math.radians(90.0 - yaw_deg)
    pitch_rad = math.radians(pitch_deg)
    roll_rad = math.radians(roll_deg)

    cy, sy = math.cos(yaw_rad), math.sin(yaw_rad)
    cp, sp = math.cos(pitch_rad), math.sin(pitch_rad)
    cr, sr = math.cos(roll_rad), math.sin(roll_rad)

    Rz = np.array([[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]], dtype=float)
    Ry = np.array([[cp, 0, sp], [0, 1, 0], [-sp, 0, cp]], dtype=float)
    Rx = np.array([[1, 0, 0], [0, cr, -sr], [0, sr, cr]], dtype=float)
    # Camera -> world at zero angles (forward to +Y, up to +Z)
    R0 = np.array([[0, 0, 1], [1, 0, 0], [0, -1, 0]], dtype=float)
    return Rz @ Ry @ Rx @ R0


def image_ray(u: int, v: int, intr: Intrinsics, ptz: PTZ, extr: Extrinsics) -> Tuple[np.ndarray, np.ndarray]:
    """Compute a ray origin and direction in world coordinates.

    Parameters
    ----------
    u, v:
        Pixel coordinates in the image (origin at top-left).
    intr:
        Camera intrinsics describing focal lengths and principal point.
    ptz:
        Optional pan/tilt offsets applied to the extrinsic pose.
    extr:
        Base camera position and orientation.

    Returns
    -------
    origin, direction : tuple of ``numpy.ndarray``
        The 3D origin of the ray and a unit-length direction vector.
    """

    # Project pixel coordinates into the camera frame using intrinsics
    x_cam = (u - intr.cx) / intr.fx
    y_cam = (v - intr.cy) / intr.fy
    d_cam = np.array([x_cam, y_cam, 1.0], dtype=float)
    d_cam /= np.linalg.norm(d_cam)

    yaw = extr.yaw + (ptz.pan or 0.0)
    # Many PTZ cameras define positive tilt as looking downwards.
    # Subtract to keep the convention that positive pitch raises the view.
    pitch = extr.pitch - (ptz.tilt or 0.0)
    roll = extr.roll
    R = _rotation_matrix(yaw, pitch, roll)
    d_world = R @ d_cam
    d_world /= np.linalg.norm(d_world)

    origin = np.array([extr.x, extr.y, extr.z], dtype=float)
    return origin, d_world


def intersect_ray_with_dem(
    ray_origin: np.ndarray,
    ray_dir: np.ndarray,
    dem: DemSampler,
    max_range_m: float = 5000.0,
    step_m: float = 20.0,
    refine_steps: int = 8,
    tol_m: float = 0.5,
) -> Optional[Tuple[float, float, float]]:
    """Intersect a ray with a DEM using adaptive stepping.

    The ray is first marched forward in coarse ``step_m`` increments until
    a sample falls below the DEM surface.  Once a crossing is detected the
    segment is refined using a binary search for ``refine_steps`` iterations
    (or until the vertical error is below ``tol_m``).  The refined
    intersection point is returned.
    """

    o = np.asarray(ray_origin, dtype=float)
    d = np.asarray(ray_dir, dtype=float)
    d /= np.linalg.norm(d)
    meters_per_unit = getattr(dem, "meters_per_unit", 1.0)
    step = step_m / meters_per_unit
    max_range = max_range_m / meters_per_unit

    t = 0.0
    while t <= max_range:
        p = o + d * t
        elev = dem.elevation(float(p[0]), float(p[1]))
        if elev is None or not math.isfinite(elev):
            t += step
            continue
        if p[2] <= elev:
            t_low, t_high = max(0.0, t - step), t
            for _ in range(refine_steps):
                t_mid = 0.5 * (t_low + t_high)
                p_mid = o + d * t_mid
                elev_mid = dem.elevation(float(p_mid[0]), float(p_mid[1]))
                if elev_mid is None or not math.isfinite(elev_mid):
                    t_low = t_mid
                    continue
                if p_mid[2] <= elev_mid:
                    t_high = t_mid
                else:
                    t_low = t_mid
                if abs(p_mid[2] - elev_mid) < tol_m:
                    p = p_mid
                    elev = elev_mid
                    break
            else:
                p = o + d * t_high
                elev = dem.elevation(float(p[0]), float(p[1]))
            return float(p[0]), float(p[1]), float(elev)
        t += step
    return None

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
    """Basic camera intrinsics derived from horizontal field of view."""
    width: int
    height: int
    hfov_deg: float


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

    The camera frame follows a standard computer-vision convention
    (``x`` right, ``y`` down, ``z`` forward).  ``yaw`` is measured
    clockwise from north (``+Y``), ``pitch`` rotates around the
    camera's right axis (positive angles tilt up) and ``roll`` rotates
    around the forward axis.  All angles are specified in degrees.
    """

    cy, sy = math.cos(math.radians(-yaw_deg)), math.sin(math.radians(-yaw_deg))
    cp, sp = math.cos(math.radians(pitch_deg)), math.sin(math.radians(pitch_deg))
    cr, sr = math.cos(math.radians(roll_deg)), math.sin(math.radians(roll_deg))

    Rz = np.array([[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]], dtype=float)
    Rx = np.array([[1, 0, 0], [0, cp, -sp], [0, sp, cp]], dtype=float)
    Ry = np.array([[cr, 0, sr], [0, 1, 0], [-sr, 0, cr]], dtype=float)

    R0 = np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]], dtype=float)
    return Rz @ Rx @ Ry @ R0


def image_ray(u: int, v: int, intr: Intrinsics, ptz: PTZ, extr: Extrinsics) -> Tuple[np.ndarray, np.ndarray]:
    """Compute a ray origin and direction in world coordinates.

    Parameters
    ----------
    u, v:
        Pixel coordinates in the image (origin at top-left).
    intr:
        Camera intrinsics (width/height/horizontal field of view).
    ptz:
        Optional pan/tilt offsets applied to the extrinsic pose.
    extr:
        Base camera position and orientation.

    Returns
    -------
    origin, direction : tuple of ``numpy.ndarray``
        The 3D origin of the ray and a unit-length direction vector.
    """

    # Build simple pin-hole intrinsics from horizontal FOV
    fx = (intr.width / 2.0) / math.tan(math.radians(intr.hfov_deg) / 2.0)
    fy = fx
    cx, cy = intr.width / 2.0, intr.height / 2.0

    x_cam = (u - cx) / fx
    y_cam = (v - cy) / fy
    d_cam = np.array([x_cam, y_cam, 1.0], dtype=float)
    d_cam /= np.linalg.norm(d_cam)

    yaw = extr.yaw + (ptz.pan or 0.0)
    # Many cameras report positive tilt when pitching downwards, while the
    # math here treats upward pitch as positive.  Subtract the PTZ tilt to
    # align with this convention.
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
    step_m: float = 5.0,
) -> Optional[Tuple[float, float, float]]:
    """Intersect a ray with a DEM using a simple stepping search.

    The function marches along the ray in ``step_m`` increments until
    either an intersection with the DEM is found or ``max_range_m`` is
    exceeded.  The first point where the ray height drops below the DEM
    height is returned.  No interpolation is performed.
    """

    o = np.asarray(ray_origin, dtype=float)
    d = np.asarray(ray_dir, dtype=float)
    d /= np.linalg.norm(d)

    t = 0.0
    while t <= max_range_m:
        p = o + d * t
        elev = dem.elevation(float(p[0]), float(p[1]))
        if elev is not None and p[2] <= elev:
            return float(p[0]), float(p[1]), float(elev)
        t += step_m
    return None

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

    The convention follows the existing application code: yaw around Z,
    pitch around Y and roll around X.  Angles are specified in degrees.
    """

    cy, sy = math.cos(math.radians(yaw_deg)), math.sin(math.radians(yaw_deg))
    cp, sp = math.cos(math.radians(pitch_deg)), math.sin(math.radians(pitch_deg))
    cr, sr = math.cos(math.radians(roll_deg)), math.sin(math.radians(roll_deg))

    Rz = np.array([[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]], dtype=float)
    Ry = np.array([[cp, 0, sp], [0, 1, 0], [-sp, 0, cp]], dtype=float)
    Rx = np.array([[1, 0, 0], [0, cr, -sr], [0, sr, cr]], dtype=float)
    return Rz @ Ry @ Rx


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
    pitch = extr.pitch + (ptz.tilt or 0.0)
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

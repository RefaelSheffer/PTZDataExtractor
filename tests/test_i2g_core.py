import numpy as np
import pytest

from core.i2g_core import (
    Intrinsics,
    Extrinsics,
    PTZ,
    image_ray,
    intersect_ray_with_dem,
)


class FlatDem:
    def __init__(self, elev: float = 0.0) -> None:
        self.elev = elev

    def elevation(self, x: float, y: float) -> float:
        return self.elev


def test_image_ray_center_forward():
    intr = Intrinsics(400, 300, 90.0)
    extr = Extrinsics(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 4326)
    ptz = PTZ(0.0, 0.0, None)
    origin, direction = image_ray(200, 150, intr, ptz, extr)
    assert np.allclose(origin, [0.0, 0.0, 0.0])
    # Facing north with no vertical component
    assert np.allclose(direction, [0.0, 1.0, 0.0])


def test_image_ray_yaw_east():
    intr = Intrinsics(400, 300, 90.0)
    extr = Extrinsics(0.0, 0.0, 0.0, 90.0, 0.0, 0.0, 4326)
    ptz = PTZ(0.0, 0.0, None)
    _, direction = image_ray(200, 150, intr, ptz, extr)
    assert np.allclose(direction, [1.0, 0.0, 0.0])


def test_image_ray_tilt_sign():
    intr = Intrinsics(400, 300, 90.0)
    extr = Extrinsics(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 4326)

    ptz_down = PTZ(0.0, 10.0, None)
    _, dir_down = image_ray(200, 150, intr, ptz_down, extr)
    assert dir_down[2] < 0.0

    ptz_up = PTZ(0.0, -10.0, None)
    _, dir_up = image_ray(200, 150, intr, ptz_up, extr)
    assert dir_up[2] > 0.0


def test_intersect_flat_dem():
    intr = Intrinsics(400, 300, 90.0)
    extr = Extrinsics(0.0, 0.0, 10.0, 90.0, -135.0, 0.0, 4326)
    ptz = PTZ(0.0, 0.0, None)
    origin, direction = image_ray(200, 150, intr, ptz, extr)
    dem = FlatDem(0.0)
    hit = intersect_ray_with_dem(origin, direction, dem, max_range_m=100.0, step_m=1.0)
    assert hit is not None
    x, y, z = hit
    assert pytest.approx(z, abs=1e-3) == 0.0
    assert pytest.approx(x, abs=0.7) == -10.6
    assert pytest.approx(y, abs=0.2) == 0.0


def test_intersect_sloped_dem():
    intr = Intrinsics(400, 300, 90.0)
    extr = Extrinsics(0.0, 0.0, 10.0, 90.0, -135.0, 0.0, 4326)
    ptz = PTZ(0.0, 0.0, None)
    origin, direction = image_ray(200, 150, intr, ptz, extr)

    class SlopeDem:
        def elevation(self, x: float, y: float) -> float:
            return 0.5 * x

    dem = SlopeDem()
    hit = intersect_ray_with_dem(origin, direction, dem, max_range_m=200.0, step_m=1.0)
    assert hit is not None
    x, y, z = hit
    assert pytest.approx(z, abs=1e-3) == pytest.approx(0.5 * x, abs=1e-3)

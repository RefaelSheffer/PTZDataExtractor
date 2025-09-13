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
    intr = Intrinsics.from_hfov(400, 300, 90.0)
    extr = Extrinsics(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 4326)
    ptz = PTZ(0.0, 0.0, None)
    origin, direction = image_ray(200, 150, intr, ptz, extr)
    assert np.allclose(origin, [0.0, 0.0, 0.0])
    assert np.allclose(direction, [0.0, 1.0, 0.0])


def test_image_ray_pan_and_tilt():
    intr = Intrinsics.from_hfov(400, 300, 90.0)
    extr = Extrinsics(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 4326)
    # Yaw 90 degrees should point east (+X)
    origin, direction = image_ray(200, 150, intr, PTZ(90.0, 0.0, None), extr)
    assert np.allclose(direction, [1.0, 0.0, 0.0])
    # Positive tilt raises the camera (Z>0)
    _, up_dir = image_ray(200, 150, intr, PTZ(0.0, 10.0, None), extr)
    _, down_dir = image_ray(200, 150, intr, PTZ(0.0, -10.0, None), extr)
    assert up_dir[2] > 0
    assert down_dir[2] < 0


def test_intersect_flat_dem():
    intr = Intrinsics.from_hfov(400, 300, 90.0)
    extr = Extrinsics(0.0, 0.0, 10.0, -90.0, 45.0, 0.0, 4326)
    ptz = PTZ(0.0, 0.0, None)
    origin, direction = image_ray(200, 150, intr, ptz, extr)
    dem = FlatDem(0.0)
    hit = intersect_ray_with_dem(origin, direction, dem, max_range_m=100.0, step_m=1.0)
    assert hit is not None
    x, y, z = hit
    assert pytest.approx(z, abs=1e-3) == 0.0
    assert pytest.approx(x, abs=0.7) == -10.0
    assert pytest.approx(y, abs=0.2) == 0.0


def test_intersect_sloped_dem():
    intr = Intrinsics.from_hfov(400, 300, 90.0)
    extr = Extrinsics(0.0, 0.0, 10.0, -90.0, 45.0, 0.0, 4326)
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

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


def test_image_ray_forward_north():
    """Zero yaw/pitch should look roughly north and above the horizon."""
    intr = Intrinsics.from_hfov(400, 300, 90.0)
    extr = Extrinsics(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 4326)
    origin, direction = image_ray(200, 150, intr, PTZ(0.0, 0.0, None), extr)
    assert np.allclose(origin, [0.0, 0.0, 0.0])
    assert direction[0] == pytest.approx(0.0)
    assert direction[1] > 0.0
    assert direction[2] >= 0.0


def test_image_ray_yaw_90_points_east():
    """Positive yaw should rotate the ray toward +X (east)."""
    intr = Intrinsics.from_hfov(400, 300, 90.0)
    extr = Extrinsics(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 4326)
    _, direction = image_ray(200, 150, intr, PTZ(90.0, 0.0, None), extr)
    assert np.allclose(direction, [1.0, 0.0, 0.0])


def test_image_ray_tilt_sign():
    """Verify tilt sign where positive tilt looks upward."""
    intr = Intrinsics.from_hfov(400, 300, 90.0)
    extr = Extrinsics(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 4326)
    _, up_dir = image_ray(200, 150, intr, PTZ(0.0, 10.0, None), extr)
    _, down_dir = image_ray(200, 150, intr, PTZ(0.0, -10.0, None), extr)
    assert up_dir[2] > 0.0
    assert down_dir[2] < 0.0


def test_image_ray_missing_tilt_defaults_zero():
    """If tilt telemetry is missing, it should default to zero."""
    intr = Intrinsics.from_hfov(400, 300, 90.0)
    extr = Extrinsics(0.0, 0.0, 0.0, 0.0, 5.0, 0.0, 4326)
    _, dir_none = image_ray(200, 150, intr, PTZ(0.0, None, None), extr)
    _, dir_zero = image_ray(200, 150, intr, PTZ(0.0, 0.0, None), extr)
    assert np.allclose(dir_none, dir_zero)


def test_intersect_flat_dem():
    intr = Intrinsics.from_hfov(400, 300, 90.0)
    extr = Extrinsics(0.0, 0.0, 10.0, -90.0, 45.0, 0.0, 4326)
    ptz = PTZ(0.0, 0.0, None)
    origin, direction = image_ray(200, 150, intr, ptz, extr)
    dem = FlatDem(0.0)
    hit = intersect_ray_with_dem(origin, direction, dem, max_range_m=100.0, step_m=20.0)
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
    hit = intersect_ray_with_dem(origin, direction, dem, max_range_m=200.0, step_m=20.0)
    assert hit is not None
    x, y, z = hit
    assert pytest.approx(z, abs=1e-3) == pytest.approx(0.5 * x, abs=1e-3)


def test_intersect_dem_ignores_non_finite():
    intr = Intrinsics.from_hfov(400, 300, 90.0)
    extr = Extrinsics(0.0, 0.0, 10.0, -90.0, 45.0, 0.0, 4326)
    ptz = PTZ(0.0, 0.0, None)
    origin, direction = image_ray(200, 150, intr, ptz, extr)

    class HoleDem:
        def elevation(self, x: float, y: float) -> float:
            if x > -5:
                return float("inf")
            return 0.0

    dem = HoleDem()
    hit = intersect_ray_with_dem(origin, direction, dem, max_range_m=100.0, step_m=20.0)
    assert hit is not None
    x, y, z = hit
    assert pytest.approx(z, abs=1e-3) == 0.0
    assert pytest.approx(x, abs=0.7) == -10.0


def test_intersect_dem_respects_meters_per_unit():
    origin = np.array([0.0, 0.0, 1.0])
    direction = np.array([1.0, 0.0, 0.0])

    class PlateauDem:
        meters_per_unit = 1000.0

        def elevation(self, x: float, y: float) -> float:
            return 2.0 if x >= 1.0 else -1.0

    dem = PlateauDem()
    hit = intersect_ray_with_dem(origin, direction, dem, max_range_m=1500.0, step_m=500.0)
    assert hit is not None
    x, y, z = hit
    assert pytest.approx(x, abs=1e-6) == 1.0
    assert pytest.approx(z, abs=1e-6) == 2.0


def test_image_ray_uses_principal_point():
    """Shifting ``cx`` should change the ray direction."""

    intr = Intrinsics.from_hfov(400, 300, 90.0)
    # Shift principal point 20 pixels to the right
    intr_off = Intrinsics(400, 300, intr.fx, intr.fy, intr.cx + 20.0, intr.cy)

    extr = Extrinsics(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 4326)
    _, dir_center = image_ray(200, 150, intr, PTZ(0.0, 0.0, None), extr)
    _, dir_off = image_ray(200, 150, intr_off, PTZ(0.0, 0.0, None), extr)

    # With an off-center principal point the ray should no longer point straight ahead
    assert dir_center[0] == pytest.approx(0.0, abs=1e-6)
    assert dir_off[0] > 0.0


def test_image_ray_tilt_90_points_up():
    """Tilting the camera 90° up should yield a +Z ray."""

    intr = Intrinsics.from_hfov(400, 300, 90.0)
    extr = Extrinsics(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 4326)
    _, direction = image_ray(
        int(intr.cx), int(intr.cy), intr, PTZ(0.0, 90.0, None), extr
    )
    assert np.allclose(direction, [0.0, 0.0, 1.0])


def test_intersect_flat_dem_returns_elevation():
    """Flat DEM at constant height should return that height."""

    intr = Intrinsics.from_hfov(400, 300, 90.0)
    extr = Extrinsics(0.0, 0.0, 10.0, 0.0, 90.0, 0.0, 4326)
    origin, direction = image_ray(
        int(intr.cx), int(intr.cy), intr, PTZ(0.0, 0.0, None), extr
    )
    dem = FlatDem(2.0)
    hit = intersect_ray_with_dem(origin, direction, dem, max_range_m=50.0, step_m=5.0)
    assert hit is not None
    x, y, z = hit
    assert pytest.approx(x, abs=1e-6) == 0.0
    assert pytest.approx(y, abs=1e-6) == 0.0
    assert pytest.approx(z, abs=1e-6) == dem.elev


def test_epsg_round_trip_precision():
    """Converting coordinates between EPSGs should preserve location."""

    pyproj = pytest.importorskip("pyproj")
    tr_fwd = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    tr_rev = pyproj.Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    lon, lat = 34.8, 31.7
    X, Y = tr_fwd.transform(lon, lat)
    lon2, lat2 = tr_rev.transform(X, Y)
    assert lon2 == pytest.approx(lon, abs=1e-9)
    assert lat2 == pytest.approx(lat, abs=1e-9)

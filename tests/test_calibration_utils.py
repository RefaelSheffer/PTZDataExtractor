import math
import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).resolve().parent.parent))

from calibration_utils import roll_error_from_horizon

def test_zero_roll_gives_zero_angle():
    assert roll_error_from_horizon(100, 100, 1000) == 0.0

def test_positive_roll_matches_expected_angle():
    width = 1920
    angle_deg = 5.0
    delta_y = width * math.tan(math.radians(angle_deg))
    result = roll_error_from_horizon(200, 200 + delta_y, width)
    assert math.isclose(result, angle_deg, rel_tol=1e-6)

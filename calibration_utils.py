"""Utility functions for camera calibration error estimation."""

import math

def roll_error_from_horizon(y_left: float, y_right: float, width: int) -> float:
    """Estimate camera roll (tilt) using two horizon samples.

    Parameters
    ----------
    y_left : float
        Vertical pixel coordinate of the horizon when the camera is panned to
        the leftmost position.
    y_right : float
        Vertical pixel coordinate of the horizon when the camera is panned to
        the rightmost position.
    width : int
        Horizontal pixel distance between the two samples, usually the image
        width.

    Returns
    -------
    float
        Estimated roll angle in degrees. Positive values mean the horizon is
        lower on the right side of the image (clockwise tilt when looking in
        the viewing direction).
    """
    dy = float(y_right) - float(y_left)
    return math.degrees(math.atan2(dy, float(width)))

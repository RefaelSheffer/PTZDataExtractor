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


def azimuth_from_ortho_points(x1: float, y1: float, x2: float, y2: float) -> float:
    """Compute azimuth angle from two orthophoto points.

    Parameters
    ----------
    x1, y1 : float
        Coordinates of the first point in the orthophoto's projected system.
    x2, y2 : float
        Coordinates of the second point in the orthophoto.

    Returns
    -------
    float
        Azimuth angle in degrees measured clockwise from north.
    """
    dx = float(x2) - float(x1)
    dy = float(y2) - float(y1)
    ang = math.degrees(math.atan2(dx, dy))
    return (ang + 360.0) % 360.0

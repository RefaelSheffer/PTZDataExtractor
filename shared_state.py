# Cross-module shared state

from typing import Optional, Dict

from PySide6 import QtCore


class _Signals(QtCore.QObject):
    """Qt signals shared across modules."""

    signal_camera_changed = QtCore.Signal(object)
    signal_stream_mode_changed = QtCore.Signal(str)


_signals = _Signals()

# Expose signals at module level for convenience
signal_camera_changed = _signals.signal_camera_changed
signal_stream_mode_changed = _signals.signal_stream_mode_changed

# אחרון DTM/אורתופוטו שנטענו (לשימוש בין הטאבים)
dtm_path: Optional[str] = None
orthophoto_path: Optional[str] = None

# מיקום המצלמה במערכת הקואורדינטות של הרסטר ממנו נלקח (פרויקטד)
# נשמר כדי לצייר CAM גם בטאב Image→Ground
# מבנה: {"x": float, "y": float, "epsg": int}
camera_proj: Optional[Dict] = None

# אחרון הגדרות ONVIF/PTZ ששיתפנו בין הטאבים
onvif_cfg: Optional[Dict] = None

# אחרון מצב וטלמטריית PTZ (מ-PtzMetaThread)
ptz_meta: Optional[Dict] = None

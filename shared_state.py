# Cross-module shared state

from typing import Optional, Dict
from PySide6 import QtCore

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


class _Signals(QtCore.QObject):
    signal_stream_mode_changed = QtCore.Signal(str)
    signal_camera_changed = QtCore.Signal(object)


signals = _Signals()
signal_stream_mode_changed = signals.signal_stream_mode_changed
signal_camera_changed = signals.signal_camera_changed

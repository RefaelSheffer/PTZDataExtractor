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


class SharedState(QtCore.QObject):
    """Container for cross-module signals and per-camera layer metadata."""

    signal_stream_mode_changed = QtCore.Signal(str)
    signal_camera_changed = QtCore.Signal(object)
    signal_layers_changed = QtCore.Signal(str, dict)  # (alias, layers)

    def __init__(self) -> None:
        super().__init__()
        # {"alias": {"ortho": str|None, "dtm": str|None, "srs": str|None}}
        self.layers_for_camera: Dict[str, Dict] = {}


_state = SharedState()
signal_stream_mode_changed = _state.signal_stream_mode_changed
signal_camera_changed = _state.signal_camera_changed
signal_layers_changed = _state.signal_layers_changed
layers_for_camera = _state.layers_for_camera
shared_state = _state

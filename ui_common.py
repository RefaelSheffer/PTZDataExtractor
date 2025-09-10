# ui_common.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Common UI helpers and widgets reused across modules.

import platform, subprocess, re
from pathlib import Path
from PySide6 import QtCore, QtWidgets
import vlc

def default_vlc_path() -> str:
    if platform.system() == 'Windows':
        p = r"C:\Program Files\VideoLAN\VLC\vlc.exe"
        return str(p) if Path(p).exists() else "vlc"
    return "vlc"

def open_folder(folder: Path):
    try:
        if platform.system()=="Windows":
            import os
            os.startfile(str(folder))
        elif platform.system()=="Darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
    except Exception:
        pass


def redact(text: str) -> str:
    """Mask credentials and IPv4 addresses in any log line."""
    if not text:
        return text
    # Mask user:pass in RTSP URLs
    text = re.sub(r'rtsp://([^:@/\s]+):([^@/\s]+)@', r'rtsp://[USER]:[***]@', text)
    # Mask IPv4 addresses
    text = re.sub(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', '[IP]', text)
    return text

class VlcVideoWidget(QtWidgets.QFrame):
    """Reusable VLC video surface for embedding into modules."""
    def __init__(self, instance: vlc.Instance):
        super().__init__()
        self.setAttribute(QtCore.Qt.WA_DontCreateNativeAncestors, True)
        self.setAttribute(QtCore.Qt.WA_NativeWindow, True)
        self.setStyleSheet("background:#111; border:1px solid #333;")
        self._instance = instance
        self._player = self._instance.media_player_new()

    def player(self) -> vlc.MediaPlayer:
        return self._player

    def ensure_video_out(self):
        wid = int(self.winId())
        if platform.system()=='Windows':
            self._player.set_hwnd(wid)
        elif platform.system()=='Darwin':
            self._player.set_nsobject(wid)
        else:
            self._player.set_xwindow(wid)

    def showEvent(self, e):
        super().showEvent(e); self.ensure_video_out()

    def resizeEvent(self, e):
        super().resizeEvent(e); self.ensure_video_out()

# --- appended helpers for click overlay ---
from PySide6 import QtGui
class ClickOverlay(QtWidgets.QWidget):
    clicked = QtCore.Signal(int, int)  # widget coords
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, False)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self._pt = None

    def mousePressEvent(self, e: QtGui.QMouseEvent):
        self._pt = e.position().toPoint()
        self.clicked.emit(self._pt.x(), self._pt.y())
        self.update()

    def paintEvent(self, e):
        if not self._pt:
            return
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        pen = QtGui.QPen(QtCore.Qt.white)
        pen.setWidth(2)
        p.setPen(pen)
        p.drawEllipse(self._pt, 8, 8)

class ClickableVideo(QtWidgets.QWidget):
    """Stack VlcVideoWidget with a transparent overlay for clicks."""
    clicked = QtCore.Signal(int, int)  # video pixel coords
    def __init__(self, instance: vlc.Instance):
        super().__init__()
        self.vlcw = VlcVideoWidget(instance)
        self.overlay = ClickOverlay(self)
        lay = QtWidgets.QStackedLayout(self)
        lay.setStackingMode(QtWidgets.QStackedLayout.StackAll)
        lay.addWidget(self.vlcw)
        lay.addWidget(self.overlay)
        self.overlay.clicked.connect(self._map_and_emit)
        self._size = (0,0)

        # periodic video size polling
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._poll_video_size)
        self._timer.start(500)

    def _poll_video_size(self):
        try:
            w,h = self.vlcw.player().video_get_size(0)
            self._size = (int(w), int(h))
        except Exception:
            pass

    def _map_and_emit(self, xw: int, yw: int):
        # map widget coords to video pixel coords considering letterboxing
        vw, vh = self._size
        if vw <= 0 or vh <= 0:
            self.clicked.emit(xw, yw)
            return
        wr = self.width()/self.height() if self.height()>0 else 1.0
        vr = vw/vh if vh>0 else 1.0
        if wr > vr:
            # widget wider -> bars left/right
            disp_h = self.height()
            disp_w = int(vr * disp_h)
            off_x = (self.width()-disp_w)//2
            off_y = 0
        else:
            # widget taller -> bars top/bottom
            disp_w = self.width()
            disp_h = int(disp_w/vr)
            off_x = 0
            off_y = (self.height()-disp_h)//2

        x = (xw - off_x) * vw / max(disp_w,1)
        y = (yw - off_y) * vh / max(disp_h,1)
        self.clicked.emit(int(x), int(y))

    def player(self):
        return self.vlcw.player()

    def ensure_video_out(self):
        self.vlcw.ensure_video_out()

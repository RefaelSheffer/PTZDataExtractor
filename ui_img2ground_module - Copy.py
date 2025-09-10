#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import sys, tempfile
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List

import numpy as np
from PySide6 import QtCore, QtWidgets, QtGui
import vlc

from camera_models import load_bundle, list_bundles
from geom3d import camera_ray_in_world, GeoRef
from dtm import DTM
from ui_map_tools import MapView, numpy_to_qimage
from raster_layer import RasterLayer
from ui_common import VlcVideoWidget
import shared_state


# ---------- Homography helpers ----------
def _homography_from_points(src: List[Tuple[float, float]],
                            dst: List[Tuple[float, float]]) -> np.ndarray:
    n = min(len(src), len(dst))
    if n < 4:
        raise ValueError("Need at least 4 matching points")
    A = []
    for (u, v), (x, y) in zip(src[:n], dst[:n]):
        A.append([u, v, 1, 0, 0, 0, -x*u, -x*v, -x])
        A.append([0, 0, 0, u, v, 1, -y*u, -y*v, -y])
    A = np.asarray(A, dtype=float)
    _, _, VT = np.linalg.svd(A)
    H = VT[-1, :].reshape(3, 3)
    if abs(H[2, 2]) > 1e-12:
        H = H / H[2, 2]
    return H


def _apply_homography(H: np.ndarray, uv: Tuple[float, float]) -> Tuple[float, float]:
    u, v = float(uv[0]), float(uv[1])
    x = H[0, 0]*u + H[0, 1]*v + H[0, 2]
    y = H[1, 0]*u + H[1, 1]*v + H[1, 2]
    w = H[2, 0]*u + H[2, 1]*v + H[2, 2]
    if abs(w) < 1e-12:
        return (np.nan, np.nan)
    return (x/w, y/w)


# ---------- פנימי: תצוגה קליקה עם אוברליי ממוספר ----------
class _ClickImage(QtWidgets.QWidget):
    clicked = QtCore.Signal(int, int)  # u,v בפיקסלי המקור

    def __init__(self, qimage: QtGui.QImage, title: str, max_pts: int, parent=None):
        super().__init__(parent)
        self._img = qimage
        self._pix = QtGui.QPixmap.fromImage(self._img)
        self._pts: List[Tuple[int,int]] = []
        self._max = max_pts

        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0,0,0,0)
        header = QtWidgets.QLabel(f"<b>{title}</b>")
        v.addWidget(header)

        self._view = QtWidgets.QLabel()
        self._view.setPixmap(self._pix)
        self._view.setAlignment(QtCore.Qt.AlignCenter)
        self._view.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        v.addWidget(self._view, 1)

        self._overlay = QtWidgets.QLabel(self._view)
        self._overlay.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self._overlay.setGeometry(self._view.rect())

        panel = QtWidgets.QHBoxLayout()
        self._lbl = QtWidgets.QLabel("0 points")
        self._btn_undo = QtWidgets.QPushButton("Undo")
        self._btn_clear = QtWidgets.QPushButton("Clear")
        panel.addWidget(self._lbl); panel.addStretch(1); panel.addWidget(self._btn_undo); panel.addWidget(self._btn_clear)
        v.addLayout(panel)

        self._btn_undo.clicked.connect(self._undo)
        self._btn_clear.clicked.connect(self._clear)
        self._view.installEventFilter(self)

    def sizeHint(self):
        return QtCore.QSize(560, 440)

    def points(self) -> List[Tuple[int,int]]:
        return list(self._pts)

    def _undo(self):
        if self._pts:
            self._pts.pop()
            self._redraw()

    def _clear(self):
        self._pts.clear()
        self._redraw()

    def _map_pos_to_uv(self, x: int, y: int) -> Optional[Tuple[int,int]]:
        px = self._view.pixmap()
        if not px: return None
        w, h = px.width(), px.height()
        W, H = self._view.width(), self._view.height()
        if w <= 0 or h <= 0: return None
        ar_s = w/float(h); ar_v = W/float(H or 1)
        if ar_s > ar_v:
            disp_w, disp_h = W, int(round(W/ar_s)); off_x, off_y = 0, (H-disp_h)//2
        else:
            disp_h, disp_w = H, int(round(H*ar_s)); off_x, off_y = (W-disp_w)//2, 0
        if not (off_x <= x < off_x+disp_w and off_y <= y < off_y+disp_h):
            return None
        u = int(round((x-off_x) * (w/float(disp_w))))
        v = int(round((y-off_y) * (h/float(disp_h))))
        return (u, v)

    def eventFilter(self, obj, ev):
        if obj is self._view:
            if ev.type() == QtCore.QEvent.MouseButtonPress and ev.button() == QtCore.Qt.LeftButton:
                pos = ev.position().toPoint()
                uv = self._map_pos_to_uv(pos.x(), pos.y())
                if uv is not None:
                    if len(self._pts) < self._max:
                        self._pts.append(uv)
                        self._redraw()
                    self.clicked.emit(uv[0], uv[1])
                return True
            elif ev.type() in (QtCore.QEvent.Resize, QtCore.QEvent.Show):
                self._overlay.setGeometry(self._view.rect())
                self._redraw()
        return super().eventFilter(obj, ev)

    def _redraw(self):
        pm = QtGui.QPixmap(self._view.size()); pm.fill(QtCore.Qt.transparent)
        p = QtGui.QPainter(pm); p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        px = self._view.pixmap()
        if px:
            w, h = px.width(), px.height()
            W, H = self._view.width(), self._view.height()
            if w <= 0 or h <= 0: w, h = 1, 1
            ar_s = w/float(h); ar_v = W/float(H or 1)
            if ar_s > ar_v:
                disp_w, disp_h = W, int(round(W/ar_s)); off_x, off_y = 0, (H-disp_h)//2
            else:
                disp_h, disp_w = H, int(round(H*ar_s)); off_x, off_y = (W-disp_w)//2, 0
            pen = QtGui.QPen(QtGui.QColor(255,80,80,230), 2); p.setPen(pen)
            f = QtGui.QFont(); f.setPointSize(10); p.setFont(f)
            for i,(u,v) in enumerate(self._pts, start=1):
                x = off_x + (u/float(w))*disp_w; y = off_y + (v/float(h))*disp_h
                p.drawEllipse(QtCore.QPointF(x,y),5,5); p.drawText(QtCore.QPointF(x+8,y-8), str(i))
        p.end()
        self._overlay.setPixmap(pm)
        self._lbl.setText(f"{len(self._pts)} points")


# ---------- דיאלוג כיול משולב (תמונה ↔ אורתופוטו) ----------
class DualPickDialog(QtWidgets.QDialog):
    """
    חלון כיול משולב: snapshot מצד שמאל, אורתופוטו מצד ימין.
    דוגמים 4–8 נק׳ בכל צד, באותו סדר. OK מופעל רק אם יש אותו מספר נק׳ (>=4).
    """
    def __init__(self, img: QtGui.QImage, ortho_img: QtGui.QImage, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Calibrate: image ↔ orthophoto (4–8 pairs)")
        self.setModal(True)
        self.setWindowFlags(self.windowFlags()
                            | QtCore.Qt.WindowMaximizeButtonHint
                            | QtCore.Qt.WindowMinimizeButtonHint)
        self.setSizeGripEnabled(True)
        self.setMinimumSize(900, 600)
        self.resize(1280, 800)

        self._max = 8
        lay = QtWidgets.QVBoxLayout(self)

        hint = QtWidgets.QLabel(
            "Click pairs in the same order: first on the IMAGE (left), then on the ORTHOPHOTO (right). "
            "You can add 4–8 pairs. Use Undo/Clear per side as needed."
        )
        hint.setWordWrap(True)
        lay.addWidget(hint)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(8)
        self._left  = _ClickImage(img, "Image (snapshot)", self._max)
        self._right = _ClickImage(ortho_img, "Orthophoto", self._max)
        self._left.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self._right.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        splitter.addWidget(self._left)
        splitter.addWidget(self._right)
        splitter.setStretchFactor(0, 1); splitter.setStretchFactor(1, 1)
        lay.addWidget(splitter, 1)

        self._status = QtWidgets.QLabel("Pairs: 0")
        lay.addWidget(self._status)

        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        self._ok = bb.button(QtWidgets.QDialogButtonBox.Ok)
        self._ok.setText("Apply & Close")
        self._ok.setDefault(True)
        self._ok.setEnabled(False)
        lay.addWidget(bb)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)

        self._left.clicked.connect(lambda *_: self._update_state())
        self._right.clicked.connect(lambda *_: self._update_state())

        self._update_state()

    def _update_state(self):
        nL, nR = len(self._left.points()), len(self._right.points())
        n = min(nL, nR)
        self._status.setText(f"Pairs: {n} (image={nL}, ortho={nR})")
        self._ok.setEnabled(nL == nR and 4 <= nL <= 8)

    def image_points(self) -> List[Tuple[int,int]]:
        return self._left.points()

    def ortho_points(self) -> List[Tuple[int,int]]:
        return self._right.points()


# ---------- Single-pick dialog (נ״צ בודד מן ה-snapshot) ----------
class SinglePickDialog(QtWidgets.QDialog):
    def __init__(self, img: QtGui.QImage, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Pick point (snapshot)")
        self.setModal(True)
        self.setWindowFlags(self.windowFlags()
                            | QtCore.Qt.WindowMaximizeButtonHint
                            | QtCore.Qt.WindowMinimizeButtonHint)
        self.setSizeGripEnabled(True)
        self.resize(1000, 700)
        self.setCursor(QtCore.Qt.CrossCursor)

        self._pix = QtGui.QPixmap.fromImage(img)
        self._uv: Optional[Tuple[int,int]] = None

        v = QtWidgets.QVBoxLayout(self)
        self._view = QtWidgets.QLabel()
        self._view.setPixmap(self._pix)
        self._view.setAlignment(QtCore.Qt.AlignCenter)
        self._view.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        v.addWidget(self._view, 1)

        self._overlay = QtWidgets.QLabel(self._view)
        self._overlay.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self._overlay.setGeometry(self._view.rect())

        v.addWidget(QtWidgets.QLabel("Click to choose a point. Press OK to confirm."))

        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        self._ok = bb.button(QtWidgets.QDialogButtonBox.Ok)
        self._ok.setDefault(True)
        self._ok.setEnabled(False)
        v.addWidget(bb)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)

        self._view.installEventFilter(self)

    def _map_pos_to_uv(self, x: int, y: int) -> Optional[Tuple[int,int]]:
        px = self._view.pixmap()
        if not px: return None
        w, h = px.width(), px.height()
        W, H = self._view.width(), self._view.height()
        if w <= 0 or h <= 0: return None
        ar_s = w/float(h); ar_v = W/float(H or 1)
        if ar_s > ar_v:
            disp_w, disp_h = W, int(round(W/ar_s)); off_x, off_y = 0, (H-disp_h)//2
        else:
            disp_h, disp_w = H, int(round(H*ar_s)); off_x, off_y = (W-disp_w)//2, 0
        if not (off_x <= x < off_x+disp_w and off_y <= y < off_y+disp_h):
            return None
        u = int(round((x-off_x) * (w/float(disp_w))))
        v = int(round((y-off_y) * (h/float(disp_h))))
        return (u, v)

    def eventFilter(self, obj, ev):
        if obj is self._view:
            if ev.type() == QtCore.QEvent.MouseButtonPress and ev.button() == QtCore.Qt.LeftButton:
                pos = ev.position().toPoint()
                uv = self._map_pos_to_uv(pos.x(), pos.y())
                if uv is not None:
                    self._uv = uv
                    self._redraw()
                    self._ok.setEnabled(True)
                return True
            elif ev.type() in (QtCore.QEvent.Resize, QtCore.QEvent.Show):
                self._overlay.setGeometry(self._view.rect())
                self._redraw()
        return super().eventFilter(obj, ev)

    def _redraw(self):
        pm = QtGui.QPixmap(self._view.size()); pm.fill(QtCore.Qt.transparent)
        if self._uv is not None:
            p = QtGui.QPainter(pm); p.setRenderHint(QtGui.QPainter.Antialiasing, True)
            px = self._view.pixmap()
            w, h = px.width(), px.height()
            W, H = self._view.width(), self._view.height()
            ar_s = w/float(h); ar_v = W/float(H or 1)
            if ar_s > ar_v:
                disp_w, disp_h = W, int(round(W/ar_s)); off_x, off_y = 0, (H-disp_h)//2
            else:
                disp_h, disp_w = H, int(round(H*ar_s)); off_x, off_y = (W-disp_w)//2, 0
            u, v = self._uv
            x = off_x + (u/float(w))*disp_w; y = off_y + (v/float(h))*disp_h
            pen = QtGui.QPen(QtGui.QColor(255,220,0), 2)
            p.setPen(pen); p.setBrush(QtGui.QBrush(QtGui.QColor(255,220,0,160)))
            p.drawEllipse(QtCore.QPointF(x,y), 5,5)
            p.end()
        self._overlay.setPixmap(pm)

    def picked_uv(self) -> Optional[Tuple[int,int]]:
        return self._uv


# ---------- Windows native click tap (ל־Live pick) ----------
class WinMouseTap(QtCore.QObject, QtCore.QAbstractNativeEventFilter):
    clicked = QtCore.Signal(int, int)
    def __init__(self, target_widget: QtWidgets.QWidget, player: vlc.MediaPlayer):
        super().__init__()
        self._w = target_widget
        self._p = player
        self._hwnd = int(target_widget.winId())

    def nativeEventFilter(self, etype, msg):
        if etype != "windows_generic_MSG":
            return False, 0
        import ctypes
        from ctypes import wintypes
        MSG = wintypes.MSG.from_address(int(msg))
        WM_LBUTTONDOWN = 0x0201
        if MSG.hwnd != self._hwnd or MSG.message != WM_LBUTTONDOWN:
            return False, 0
        x = ctypes.c_short(MSG.lParam & 0xffff).value
        y = ctypes.c_short((MSG.lParam >> 16) & 0xffff).value
        try:
            W,H = self._w.width(), self._w.height()
            w,h  = self._p.video_get_size(0)
        except Exception:
            w=h=0
        if w<=0 or h<=0: return False, 0
        ar_s = w/float(h); ar_v = W/float(H or 1)
        if ar_s > ar_v:
            disp_w, disp_h = W, int(round(W/ar_s)); off_x, off_y = 0, (H-disp_h)//2
        else:
            disp_h, disp_w = H, int(round(H*ar_s)); off_x, off_y = (W-disp_w)//2, 0
        if not (off_x <= x < off_x+disp_w and off_y <= y < off_y+disp_h):
            return False, 0
        u = int(round((x-off_x) * (w/float(disp_w))))
        v = int(round((y-off_y) * (h/float(disp_h))))
        self.clicked.emit(u, v)
        return False, 0


# ---------- Main module ----------
class Img2GroundModule(QtCore.QObject):
    title = "Image → Ground"
    icon = None

    def __init__(self, vlc_instance: vlc.Instance, log_func=print):
        super().__init__()
        self._log = log_func
        self._vlc = vlc_instance

        self.video = VlcVideoWidget(self._vlc)
        self._player = self.video.player()
        self._media: Optional[vlc.Media] = None

        self._bundle: Optional[Dict[str, Any]] = None
        self._dtm: Optional[DTM] = None
        self._map: Optional[MapView] = None
        self._ortho_layer: Optional[RasterLayer] = None
        self._ortho_pix: Optional[QtWidgets.QGraphicsPixmapItem] = None
        self._img_pts: List[Tuple[int,int]] = []
        self._ortho_pts_scene: List[Tuple[float,float]] = []
        self._H: Optional[np.ndarray] = None
        self._last_geo = None
        self._native_tap: Optional[WinMouseTap] = None

        # למסגרת הווידאו שמצויירת על האורתופוטו
        self._calib_img_wh: Optional[Tuple[int,int]] = None
        self._frame_item: Optional[QtWidgets.QGraphicsPathItem] = None

        # לנקודת ה־Pick האחרונה (אייטם קבוע שלא נעלם)
        self._last_pick_item: Optional[QtWidgets.QGraphicsEllipseItem] = None
        self._last_pick_label: Optional[QtWidgets.QGraphicsSimpleTextItem] = None

        self._root = self._build_ui()
        self._attach_vlc_events()
        QtCore.QTimer.singleShot(0, self._try_load_shared_ortho)

        self._t = QtCore.QTimer(self._root); self._t.timeout.connect(self._update_metrics); self._t.start(800)

    def widget(self) -> QtWidgets.QWidget:
        return self._root

    # ----- UI -----
    def _build_ui(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget(); g = QtWidgets.QGridLayout(w)
        r = 0

        # Bundle
        g.addWidget(QtWidgets.QLabel("<b>Bundle</b>"), r, 0, 1, 8); r += 1
        self.cmb_bundle = QtWidgets.QComboBox(); self._refresh_bundles()
        btn_load = QtWidgets.QPushButton("Load"); btn_load.clicked.connect(self._on_load_bundle)
        self.lbl_bundle = QtWidgets.QLabel("(no bundle)")
        g.addWidget(QtWidgets.QLabel("Existing:"), r, 0)
        g.addWidget(self.cmb_bundle, r, 1, 1, 3)
        g.addWidget(btn_load, r, 4)
        g.addWidget(self.lbl_bundle, r, 5, 1, 3); r += 1

        # Mode
        g.addWidget(QtWidgets.QLabel("<b>Mode</b>"), r, 0, 1, 8); r += 1
        self.rb_online = QtWidgets.QRadioButton("Online (RTSP/ONVIF)")
        self.rb_mock   = QtWidgets.QRadioButton("Mockup (Local file)")
        self.rb_online.setChecked(True); self.rb_online.toggled.connect(self._update_mode_enabled)
        g.addWidget(self.rb_online, r, 0, 1, 4); g.addWidget(self.rb_mock, r, 4, 1, 4); r += 1

        self.ed_rtsp = QtWidgets.QLineEdit("rtsp://127.0.0.1:8554/cam")
        self.btn_play_rtsp = QtWidgets.QPushButton("Play RTSP"); self.btn_play_rtsp.clicked.connect(self._play_rtsp)
        g.addWidget(QtWidgets.QLabel("RTSP URL:"), r, 0); g.addWidget(self.ed_rtsp, r, 1, 1, 5); g.addWidget(self.btn_play_rtsp, r, 6, 1, 2); r += 1

        self.ed_file = QtWidgets.QLineEdit()
        self.btn_browse = QtWidgets.QPushButton("Browse video…"); self.btn_browse.clicked.connect(self._browse_file)
        self.btn_play_file = QtWidgets.QPushButton("Play file"); self.btn_play_file.clicked.connect(self._play_file)
        g.addWidget(QtWidgets.QLabel("Video file:"), r, 0)
        g.addWidget(self.ed_file, r, 1, 1, 5); g.addWidget(self.btn_browse, r, 6); g.addWidget(self.btn_play_file, r, 7); r += 1

        # Splitter: video ↔ map
        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(8)
        self.video.setMinimumHeight(360)
        self.video.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        splitter.addWidget(self.video)
        self._map = MapView(); self._map.setMinimumHeight(360)
        self._map.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self._map.clicked.connect(self._on_map_click)
        splitter.addWidget(self._map)
        splitter.setStretchFactor(0,1); splitter.setStretchFactor(1,1)
        g.addWidget(splitter, r, 0, 1, 8); r += 1

        # Ortho / calibration tools
        rowm = QtWidgets.QHBoxLayout()
        self.btn_load_ortho = QtWidgets.QPushButton("Load Orthophoto…"); self.btn_load_ortho.clicked.connect(self._load_orthophoto)
        self.btn_use_shared = QtWidgets.QPushButton("Use Orthophoto from Preparation"); self.btn_use_shared.clicked.connect(self._try_load_shared_ortho)
        self.btn_dual_cal = QtWidgets.QPushButton("Calibrate (image↔ortho)…"); self.btn_dual_cal.clicked.connect(self._dual_calibration)
        self.btn_reset = QtWidgets.QPushButton("Reset calibration"); self.btn_reset.clicked.connect(self._reset_calibration)
        rowm.addWidget(self.btn_load_ortho); rowm.addWidget(self.btn_use_shared); rowm.addStretch(1)
        rowm.addWidget(self.btn_dual_cal); rowm.addWidget(self.btn_reset)
        g.addLayout(rowm, r, 0, 1, 8); r += 1

        # Point extraction
        rowp = QtWidgets.QHBoxLayout()
        self.btn_pick_now = QtWidgets.QPushButton("Pick now (snapshot)"); self.btn_pick_now.clicked.connect(self._pick_now_snapshot)
        self.btn_live = QtWidgets.QPushButton("Live pick (no overlay)"); self.btn_live.setCheckable(True); self.btn_live.toggled.connect(self._enable_live_pick)
        rowp.addWidget(self.btn_pick_now); rowp.addWidget(self.btn_live); rowp.addStretch(1)
        g.addLayout(rowp, r, 0, 1, 8); r += 1

        # Status + metrics
        self.lbl_status = QtWidgets.QLabel(""); g.addWidget(self.lbl_status, r, 0, 1, 8); r += 1
        self.lbl_metrics = QtWidgets.QLabel("State: idle | 0x0 | FPS:?"); g.addWidget(self.lbl_metrics, r, 0, 1, 8); r += 1

        # Playback utils
        row2 = QtWidgets.QHBoxLayout()
        self.btn_pause = QtWidgets.QPushButton("Pause/Resume"); self.btn_pause.clicked.connect(self._toggle_pause)
        self.btn_stop  = QtWidgets.QPushButton("Stop"); self.btn_stop.clicked.connect(self._stop)
        self.btn_copy  = QtWidgets.QPushButton("Copy last coords"); self.btn_copy.clicked.connect(self._copy_last)
        self.btn_qr    = QtWidgets.QPushButton("Show QR"); self.btn_qr.clicked.connect(self._show_qr)
        row2.addWidget(self.btn_pause); row2.addWidget(self.btn_stop); row2.addStretch(1); row2.addWidget(self.btn_copy); row2.addWidget(self.btn_qr)
        g.addLayout(row2, r, 0, 1, 8); r += 1

        g.setRowStretch(r, 1)
        self._update_mode_enabled()
        return w

    # ----- VLC events / metrics -----
    def _attach_vlc_events(self):
        ev = self._player.event_manager()
        ev.event_attach(vlc.EventType.MediaPlayerPlaying, lambda e: self._log("VLC: Playing"))
        ev.event_attach(vlc.EventType.MediaPlayerEncounteredError, lambda e: self._log("VLC: EncounteredError"))
        ev.event_attach(vlc.EventType.MediaPlayerEndReached, lambda e: self._log("VLC: EndReached"))

    def _update_mode_enabled(self):
        on = self.rb_online.isChecked()
        self.ed_rtsp.setEnabled(on); self.btn_play_rtsp.setEnabled(on)
        self.ed_file.setEnabled(not on); self.btn_browse.setEnabled(not on); self.btn_play_file.setEnabled(not on)

    # ----- Play controls -----
    def _play_rtsp(self):
        url = self.ed_rtsp.text().strip()
        if not url:
            QtWidgets.QMessageBox.information(None, "RTSP", "Enter RTSP URL."); return
        self._set_media(url, is_file=False)

    def _browse_file(self):
        p, _ = QtWidgets.QFileDialog.getOpenFileName(None, "Open Video", "",
                        "Video files (*.mp4 *.mkv *.avi *.mov *.ts *.m4v);;All files (*.*)")
        if p: self.ed_file.setText(p)

    def _play_file(self):
        p = self.ed_file.text().strip()
        if not p or not Path(p).exists():
            QtWidgets.QMessageBox.information(None, "Video", "Choose a valid video file."); return
        self._set_media(p, is_file=True)

    def _set_media(self, mrl: str, is_file: bool):
        try:
            if is_file:
                p = Path(mrl)
                if not p.exists():
                    QtWidgets.QMessageBox.warning(None, "File", f"Not found:\n{p}"); return
                uri = p.as_uri()
                media = self._vlc.media_new(uri)
                if hasattr(media, "add_option"):
                    media.add_option(":file-caching=1200")
                    if p.suffix.lower() == ".avi":
                        media.add_option(":demux=avi")
            else:
                media = self._vlc.media_new(mrl)
                if hasattr(media, "add_option"):
                    media.add_option(":network-caching=800")
                    media.add_option(":rtsp-tcp")
            if hasattr(media, "add_option"):
                media.add_option(":avcodec-hw=none")
                media.add_option(":no-video-title-show")

            self._media = media
            self._player.set_media(self._media)
            self._player.play()
            QtCore.QTimer.singleShot(150, lambda: self.video.ensure_video_out())
            self._log(f"Playing: {mrl}")
            self.lbl_status.setText(f"Playing: {mrl}")
        except Exception as e:
            QtWidgets.QMessageBox.warning(None, "VLC", f"Failed to play:\n{e}")
            self._log(f"_set_media failed: {e}")

    def _toggle_pause(self):
        try: self._player.pause()
        except Exception: pass

    def _stop(self):
        try: self._player.stop()
        except Exception: pass

    # ----- Ortho/map -----
    def _ensure_scene(self) -> QtWidgets.QGraphicsScene:
        sc = self._map.scene()
        if sc is None:
            sc = QtWidgets.QGraphicsScene(); self._map.setScene(sc)
        return sc

    def _try_load_shared_ortho(self):
        path = getattr(shared_state, "orthophoto_path", None)
        if path and Path(path).exists():
            self._load_orthophoto(path)
            QtCore.QTimer.singleShot(50, self._draw_shared_camera_marker)

    def _draw_shared_camera_marker(self):
        if self._ortho_layer is None: return
        cam = getattr(shared_state, "camera_proj", None)
        if not cam: return
        try:
            epsg_here = self._ortho_layer.ds.crs.to_epsg()
            X, Y = float(cam["x"]), float(cam["y"]); epsg_cam = cam.get("epsg")
            if epsg_here and epsg_cam and epsg_cam != epsg_here:
                from pyproj import Transformer
                tr = Transformer.from_crs(f"EPSG:{epsg_cam}", f"EPSG:{epsg_here}", always_xy=True)
                X, Y = tr.transform(X, Y)
            xs, ys = self._ortho_layer.geo_to_scene(X, Y)
            self._map.set_marker(xs, ys)
        except Exception as e:
            self._log(f"draw_shared_camera_marker failed: {e}")

    def _load_orthophoto(self, path: Optional[str] = None):
        if not path:
            path, _ = QtWidgets.QFileDialog.getOpenFileName(None, "Open Orthophoto (GeoTIFF)", "",
                                                            "GeoTIFF (*.tif *.tiff);;All files (*.*)")
            if not path: return
        try:
            self._ortho_layer = RasterLayer(path, max_size=2048)
            img = numpy_to_qimage(self._ortho_layer.downsampled_image())
            pix = QtGui.QPixmap.fromImage(img)
            sc = self._ensure_scene(); sc.clear()
            self._ortho_pix = QtWidgets.QGraphicsPixmapItem(pix); self._ortho_pix.setZValue(0); sc.addItem(self._ortho_pix)
            self._map.setSceneRect(self._ortho_pix.boundingRect()); self._map.fit()
            self._log(f"Orthophoto loaded (EPSG={self._ortho_layer.ds.crs.to_epsg()})")
            self._remove_last_pick()
            QtCore.QTimer.singleShot(50, self._draw_shared_camera_marker)
            QtCore.QTimer.singleShot(60, self._draw_video_frame_outline)
        except Exception as e:
            QtWidgets.QMessageBox.warning(None, "Orthophoto", f"Failed to load: {e}")

    # ----- Dual calibration flow -----
    def _dual_calibration(self):
        if self._ortho_layer is None:
            QtWidgets.QMessageBox.information(None, "Calibration", "Load an orthophoto first."); return
        try: self._player.set_pause(True)
        except Exception: pass

        img = self._snapshot_image()
        if not img:
            QtWidgets.QMessageBox.information(None, "Calibration", "Failed to capture frame from video.")
            try: self._player.set_pause(False)
            except Exception: pass
            return

        ortho_img = numpy_to_qimage(self._ortho_layer.downsampled_image())
        dlg = DualPickDialog(img, ortho_img, self._root)
        if dlg.exec() == QtWidgets.QDialog.Accepted:
            img_pts = [(int(u),int(v)) for (u,v) in dlg.image_points()]
            ortho_pts = [(float(x),float(y)) for (x,y) in dlg.ortho_points()]
            try:
                H = _homography_from_points(img_pts, ortho_pts)
                self._img_pts = img_pts
                self._ortho_pts_scene = ortho_pts
                self._H = H
                self._calib_img_wh = (img.width(), img.height())
                self._draw_video_frame_outline()
                self.lbl_status.setText(f"Calibration ready with {len(img_pts)} pairs. Use Pick-now / Live-pick.")
                self._log("Homography computed (dual-dialog).")
            except Exception as e:
                QtWidgets.QMessageBox.warning(None, "Calibration", f"Failed to compute homography: {e}")

        try: self._player.set_pause(False)
        except Exception: pass

    def _reset_calibration(self):
        self._img_pts.clear(); self._ortho_pts_scene.clear(); self._H = None
        self._calib_img_wh = None
        self._remove_video_frame_outline()
        self._remove_last_pick()
        self._map.clear_temp(); self.lbl_status.setText("Calibration reset.")

    # ----- Map click handler (לחיצה על האורתופוטו במוד הראשי) -----
    def _on_map_click(self, xs: float, ys: float):
        sc = self._ensure_scene()
        dot = sc.addEllipse(-3, -3, 6, 6,
                            QtGui.QPen(QtGui.QColor(180, 220, 255), 2),
                            QtGui.QBrush(QtGui.QColor(180, 220, 255)))
        dot.setPos(xs, ys)
        if self._ortho_layer is None:
            return
        try:
            X, Y = self._ortho_layer.scene_to_geo(xs, ys)
            epsg = self._ortho_layer.ds.crs.to_epsg()
            from pyproj import Transformer
            tr = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
            lon, lat = tr.transform(X, Y)
            self._last_geo = {"lat": float(lat), "lon": float(lon), "epsg": epsg, "X": float(X), "Y": float(Y)}
            self.lbl_status.setText(f"MAP: lat={lat:.7f}, lon={lon:.7f} | XY(EPSG:{epsg})=({X:.2f}, {Y:.2f})")
        except Exception as e:
            self.lbl_status.setText(f"map click failed: {e}")

    # ----- Real-time extraction -----
    def _pick_now_snapshot(self):
        try: self._player.set_pause(True)
        except Exception: pass

        img = self._snapshot_image()
        if not img:
            QtWidgets.QMessageBox.information(None, "Pick", "Failed to capture frame.")
            try: self._player.set_pause(False)
            except Exception: pass
            return

        dlg = SinglePickDialog(img, self._root)
        if dlg.exec() == QtWidgets.QDialog.Accepted and dlg.picked_uv() is not None:
            uu, vv = dlg.picked_uv()
            self._on_video_click(int(uu), int(vv))

        try: self._player.set_pause(False)
        except Exception: pass

    def _enable_live_pick(self, en: bool):
        if sys.platform != "win32":
            QtWidgets.QMessageBox.information(None, "Live pick", "Windows only."); self.btn_live.setChecked(False); return
        app = QtWidgets.QApplication.instance()
        if en:
            self.video.setCursor(QtCore.Qt.CrossCursor)
            self._native_tap = WinMouseTap(self.video, self._player)
            self._native_tap.clicked.connect(self._on_video_click)
            app.installNativeEventFilter(self._native_tap)
            self._log("Live-pick armed")
        else:
            self.video.unsetCursor()
            if self._native_tap:
                try: app.removeNativeEventFilter(self._native_tap)
                except Exception: pass
                self._native_tap = None
            self._log("Live-pick disarmed")

    # ----- Click → map / fallback -----
    def _on_video_click(self, u: int, v: int):
        # אם יש הומוגרפיה - נשרטט נקודה באורתופוטו ונחשב קואורדינטות
        if self._H is not None and self._ortho_layer is not None:
            xs, ys = _apply_homography(self._H, (u, v))
            if not np.isfinite(xs) or not np.isfinite(ys):
                self.lbl_status.setText("Calibration mapping failed."); return

            # צייר/עדכן סימון קבוע על האורתופוטו
            self._show_pick_on_map(xs, ys)

            # חישוב ל־LLA
            try:
                X, Y = self._ortho_layer.scene_to_geo(xs, ys)
                epsg = self._ortho_layer.ds.crs.to_epsg()
                from pyproj import Transformer
                tr = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
                lon, lat = tr.transform(X, Y)
            except Exception as e:
                self.lbl_status.setText(f"scene_to_geo failed: {e}"); return

            # עדכן תווית ליד הסימון
            self._show_pick_on_map(xs, ys, text=f"{lat:.5f},{lon:.5f}")

            self._last_geo = {"lat": float(lat), "lon": float(lon), "epsg": epsg, "X": float(X), "Y": float(Y)}
            self.lbl_status.setText(f"CALIB: lat={lat:.7f}, lon={lon:.7f} | XY(EPSG:{epsg})=({X:.2f}, {Y:.2f})")
            return

        # Fallback: Bundle+DTM
        if self._bundle is None or self._dtm is None:
            QtWidgets.QMessageBox.information(None, "Image→Ground", "Load a bundle or perform calibration first."); return
        try:
            intr_d = self._bundle["intrinsics"]; pose_d = self._bundle["pose"]; georef_d = self._bundle["georef"]
            from geom3d import CameraIntrinsics, CameraPose, intersect_ray_with_dtm
            yaw = pose_d.get("yaw_deg", pose_d.get("yaw", 0.0))
            pitch = pose_d.get("pitch_deg", pose_d.get("pitch", 0.0))
            roll = pose_d.get("roll_deg", pose_d.get("roll", 0.0))
            intr = CameraIntrinsics(intr_d["width"], intr_d["height"], intr_d["fx"], intr_d["fy"], intr_d["cx"], intr_d["cy"])
            pose = CameraPose(pose_d["x"], pose_d["y"], pose_d["z"], yaw, pitch, roll)
            georef = GeoRef.from_dict(georef_d)
            o, d = camera_ray_in_world(u, v, intr, pose)
            p = intersect_ray_with_dtm(o, d, self._dtm, georef)
            if p is None: self.lbl_status.setText("Missed DTM / No intersection."); return
            g = georef.local_to_geographic(p)
            lla = g["lla"]; prj = g["projected"]
            self._last_geo = {"lat": lla['lat'], "lon": lla['lon'], "epsg": (prj or {}).get("epsg")}
            msg = f"GEOM: LLA lat={lla['lat']:.7f}, lon={lla['lon']:.7f}, alt={lla['alt']:.3f}"
            if prj: msg += f" | XY=({prj['x']:.2f}, {prj['y']:.2f})"
            self.lbl_status.setText(msg)
        except Exception as e:
            QtWidgets.QMessageBox.warning(None, "Image→Ground", f"Failed: {e}")

    # ----- ציור/ניקוי מסגרת הווידאו על האורתופוטו -----
    def _remove_video_frame_outline(self):
        if self._frame_item is not None:
            try:
                self._frame_item.scene().removeItem(self._frame_item)
            except Exception:
                pass
            self._frame_item = None

    def _draw_video_frame_outline(self):
        """מצייר פוליגון של גבולות הווידאו (snapshot ששימש לכיול) על האורתופוטו."""
        if self._H is None or self._ortho_layer is None or self._calib_img_wh is None:
            return
        W, H = self._calib_img_wh
        if W <= 0 or H <= 0:
            return
        corners = [(0,0),(W-1,0),(W-1,H-1),(0,H-1),(0,0)]
        mapped: List[Tuple[float,float]] = []
        for u,v in corners:
            xs, ys = _apply_homography(self._H, (u, v))
            if not (np.isfinite(xs) and np.isfinite(ys)):
                return
            mapped.append((xs, ys))

        sc = self._ensure_scene()
        self._remove_video_frame_outline()
        path = QtGui.QPainterPath(QtCore.QPointF(mapped[0][0], mapped[0][1]))
        for i in range(1, len(mapped)):
            path.lineTo(mapped[i][0], mapped[i][1])

        item = QtWidgets.QGraphicsPathItem(path)
        pen = QtGui.QPen(QtGui.QColor(0, 255, 180), 2)
        pen.setCosmetic(True)  # עובי קבוע ללא קשר לזום
        item.setPen(pen)
        item.setBrush(QtCore.Qt.NoBrush)
        item.setZValue(15)
        sc.addItem(item)
        self._frame_item = item

    # ----- ניהול סימון הנ״צ האחרון -----
    def _remove_last_pick(self):
        if self._last_pick_item is not None:
            try:
                self._last_pick_item.scene().removeItem(self._last_pick_item)
            except Exception:
                pass
            self._last_pick_item = None
        if self._last_pick_label is not None:
            try:
                self._last_pick_label.scene().removeItem(self._last_pick_label)
            except Exception:
                pass
            self._last_pick_label = None

    def _show_pick_on_map(self, xs: float, ys: float, text: Optional[str] = None):
        sc = self._ensure_scene()
        if self._last_pick_item is None:
            item = QtWidgets.QGraphicsEllipseItem(-5, -5, 10, 10)
            item.setBrush(QtGui.QBrush(QtGui.QColor(255, 220, 0)))
            item.setPen(QtGui.QPen(QtGui.QColor(30, 30, 30), 1))
            item.setZValue(50)
            sc.addItem(item)
            self._last_pick_item = item
        self._last_pick_item.setPos(xs, ys)

        if text:
            if self._last_pick_label is None:
                lab = QtWidgets.QGraphicsSimpleTextItem("")
                lab.setBrush(QtGui.QBrush(QtGui.QColor(255, 255, 255)))
                lab.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0)))
                lab.setZValue(51)
                sc.addItem(lab)
                self._last_pick_label = lab
            self._last_pick_label.setText(text)
            self._last_pick_label.setPos(xs + 10, ys - 10)

        try:
            self._map.centerOn(xs, ys)
        except Exception:
            pass
        self._map.viewport().update()

    # ----- Bundles -----
    def _refresh_bundles(self):
        self.cmb_bundle.clear(); self.cmb_bundle.addItems(["(choose)"] + list_bundles())

    def _on_load_bundle(self):
        name = self.cmb_bundle.currentText().strip()
        if not name or name == "(choose)":
            QtWidgets.QMessageBox.information(None, "Bundle", "Choose a bundle first."); return
        try:
            res = load_bundle(name)
            if isinstance(res, tuple) and len(res) >= 5:
                intr, pose, terrain_path, meta, georef = res[:5]
                self._bundle = {
                    "intrinsics": getattr(intr, "to_dict", lambda: intr)(),
                    "pose": getattr(pose, "to_dict", lambda: pose)(),
                    "terrain_path": terrain_path,
                    "georef": georef if isinstance(georef, dict) else getattr(georef, "to_dict", lambda: {})(),
                }
                model_path = terrain_path
            else:
                self._bundle = dict(res)
                model_path = self._bundle.get("model_path") or self._bundle.get("terrain_path")

            if not model_path or not Path(model_path).exists():
                QtWidgets.QMessageBox.warning(None, "Bundle", f"DTM path not found:\n{model_path}"); return
            if self._dtm is not None: self._dtm.close()
            self._dtm = DTM(model_path)
            self.lbl_bundle.setText(f"Loaded: {name}"); self._log(f"Bundle loaded: {name}")
        except Exception as e:
            QtWidgets.QMessageBox.warning(None, "Bundle", f"Failed to load bundle: {e}")

    # ----- Utils -----
    def _snapshot_image(self) -> Optional[QtGui.QImage]:
        try:
            tmp = Path(tempfile.gettempdir()) / "img2ground_snapshot.png"
            ok = (self._player.video_take_snapshot(0, str(tmp), 0, 0) == 0)
            if not ok or not tmp.exists():
                pm = self.video.grab()
                return pm.toImage() if not pm.isNull() else None
            img = QtGui.QImage(str(tmp))
            return img if not img.isNull() else None
        except Exception:
            return None

    def _copy_last(self):
        if not self._last_geo:
            QtWidgets.QMessageBox.information(None, "Copy", "No coordinates yet."); return
        QtWidgets.QApplication.clipboard().setText(f"{self._last_geo['lat']:.7f},{self._last_geo['lon']:.7f}")

    def _show_qr(self):
        if not self._last_geo:
            QtWidgets.QMessageBox.information(None, "QR", "No coordinates yet."); return
        try:
            import qrcode
            from PIL import ImageQt
        except Exception:
            url = f"https://maps.google.com/?q={self._last_geo['lat']:.7f},{self._last_geo['lon']:.7f}"
            QtWidgets.QMessageBox.information(None, "QR",
                "Install to enable QR:\n  python -m pip install qrcode pillow\n" f"URL:\n{url}")
            return
        url = f"https://maps.google.com/?q={self._last_geo['lat']:.7f},{self._last_geo['lon']:.7f}"
        img = qrcode.make(url); qim = ImageQt.ImageQt(img); pix = QtGui.QPixmap.fromImage(qim)
        dlg = QtWidgets.QDialog(); dlg.setWindowTitle("QR (Google Maps)")
        lay = QtWidgets.QVBoxLayout(dlg)
        lab = QtWidgets.QLabel(); lab.setPixmap(pix); lab.setAlignment(QtCore.Qt.AlignCenter); lay.addWidget(lab)
        lab2 = QtWidgets.QLabel(url); lab2.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse); lay.addWidget(lab2)
        dlg.resize(300, 340); dlg.exec()

    def _update_metrics(self):
        p = self._player
        if not p: return
        try: w, h = p.video_get_size(0)
        except Exception: w, h = 0, 0
        try: fps = p.get_fps() or 0.0
        except Exception: fps = 0.0
        st = p.get_state()
        self.lbl_metrics.setText(f"State: {st} | {w}x{h} | FPS:{fps:.1f}")

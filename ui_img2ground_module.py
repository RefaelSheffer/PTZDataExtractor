#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Align PTZ camera imagery with an orthophoto map.

This module combines manual homography selection with live PTZ metadata to
map image pixels to ground coordinates. Pan/tilt angles together with the
zoom (focal length) and focus values reported by the camera determine the
viewing direction and field of view used during calibration.
"""

from __future__ import annotations
import sys, math, json
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List, Protocol

import numpy as np
from PySide6 import QtCore, QtWidgets, QtGui
import vlc

from camera_models import load_bundle, list_bundles, CALIB_DIR
from geom3d import camera_ray_in_world, GeoRef
from dtm import DTM
from map_view import MapView
from ui_map_tools import numpy_to_qimage
from raster_layer import RasterLayer
from ui_common import VlcVideoWidget
from ui_calibration_module import HorizonAzimuthCalibrationDialog
import shared_state
from app_state import app_state
from event_bus import bus

from any_ptz_client import AnyPTZClient
from onvif_ptz import PTZReading
from calibration_utils import roll_error_from_horizon

APP_DIR = Path(__file__).resolve().parent
APP_CFG = APP_DIR / "app_config.json"
PROFILES_PATH = APP_DIR / "profiles.json"


def load_cfg() -> dict:
    if APP_CFG.exists():
        try:
            return json.loads(APP_CFG.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_cfg(cfg: dict) -> None:
    try:
        APP_CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


class PTZClient(Protocol):
    poll_dt: float
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def last(self) -> PTZReading: ...


# ---------------- math helpers ----------------
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

def _normalize_angle_deg(a: float) -> float:
    while a <= -180.0: a += 360.0
    while a >   180.0: a -= 360.0
    return a

def _angle_diff_deg(a: float, b: float) -> float:
    return _normalize_angle_deg(a - b)

def _mid_yaw_deg(a: float, b: float) -> float:
    da = _angle_diff_deg(b, a)
    return _normalize_angle_deg(a + 0.5*da)


# ---------------- small views/dialogs ----------------
class _GraphicsClickView(QtWidgets.QGraphicsView):
    clicked = QtCore.Signal(float, float)  # scene coords
    def mousePressEvent(self, e: QtGui.QMouseEvent):
        if e.button() == QtCore.Qt.LeftButton:
            p = self.mapToScene(e.position().toPoint())
            self.clicked.emit(p.x(), p.y())
            e.accept(); return
        super().mousePressEvent(e)


class HomographyDialog(QtWidgets.QDialog):
    """
    דיאלוג כיול הומוגרפיה 4–8 נק׳:
    שמאל: תמונת snapshot (QImage) – קואורדינטות u,v
    ימין: אותה QGraphicsScene של המפה הראשית – קואורדינטות scene xs,ys
    """
    def __init__(self, snapshot_img: QtGui.QImage, map_scene: QtWidgets.QGraphicsScene, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Homography calibration (4–8 pairs)")
        self.setModal(True)
        self.resize(1200, 700)
        self.setSizeGripEnabled(True)

        self._img = snapshot_img
        self._pix = QtGui.QPixmap.fromImage(snapshot_img)
        self._uvs: List[Tuple[int,int]] = []
        self._xys: List[Tuple[float,float]] = []
        self._tmp_items: List[QtWidgets.QGraphicsItem] = []  # על המפה

        v = QtWidgets.QVBoxLayout(self)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal); splitter.setHandleWidth(8)
        v.addWidget(splitter, 1)

        # left: image panel with overlay
        self._img_label = QtWidgets.QLabel()
        self._img_label.setPixmap(self._pix)
        self._img_label.setAlignment(QtCore.Qt.AlignCenter)
        self._img_label.setBackgroundRole(QtGui.QPalette.Base)
        self._img_label.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self._img_label.setCursor(QtCore.Qt.CrossCursor)

        self._overlay = QtWidgets.QLabel(self._img_label)
        self._overlay.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self._overlay.setGeometry(self._img_label.rect())
        self._img_label.installEventFilter(self)

        # right: the shared map scene
        self._view = _GraphicsClickView()
        self._view.setScene(map_scene)
        self._view.setRenderHints(QtGui.QPainter.Antialiasing | QtGui.QPainter.SmoothPixmapTransform)
        self._view.setDragMode(QtWidgets.QGraphicsView.ScrollHandDrag)
        self._view.clicked.connect(self._on_map_clicked)

        splitter.addWidget(self._img_label)
        splitter.addWidget(self._view)
        splitter.setStretchFactor(0,1); splitter.setStretchFactor(1,1)

        # footer
        h = QtWidgets.QHBoxLayout()
        self.lbl = QtWidgets.QLabel("Pairs: 0 (need 4–8). Click LEFT image, then RIGHT map, לסירוגין.")
        self.btn_undo = QtWidgets.QPushButton("Undo last pair"); self.btn_undo.clicked.connect(self._undo_last)
        self.btn_clear = QtWidgets.QPushButton("Clear"); self.btn_clear.clicked.connect(self._clear)
        h.addWidget(self.lbl); h.addStretch(1); h.addWidget(self.btn_undo); h.addWidget(self.btn_clear)
        v.addLayout(h)

        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        self._ok = bb.button(QtWidgets.QDialogButtonBox.Ok); self._ok.setEnabled(False)
        v.addWidget(bb)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)

        self._awaiting = "image"  # 'image' -> 'map' -> 'image' ...

    def eventFilter(self, obj, ev):
        if obj is self._img_label:
            if ev.type() == QtCore.QEvent.MouseButtonPress and ev.button() == QtCore.Qt.LeftButton:
                if self._awaiting != "image":
                    return True
                pos = ev.position().toPoint()
                uv = self._map_pos_to_uv(pos.x(), pos.y())
                if uv is not None:
                    self._uvs.append(uv)
                    self._draw_overlay()
                    self._awaiting = "map"
                    self._update_lbl()
                return True
            elif ev.type() in (QtCore.QEvent.Resize, QtCore.QEvent.Show):
                self._overlay.setGeometry(self._img_label.rect())
                self._draw_overlay()
        return super().eventFilter(obj, ev)

    def _map_pos_to_uv(self, x: int, y: int) -> Optional[Tuple[int,int]]:
        px = self._img_label.pixmap()
        if not px: return None
        w, h = px.width(), px.height()
        W, H = self._img_label.width(), self._img_label.height()
        if w <= 0 or h <= 0: return None
        ar_s = w/float(h); ar_v = W/float(H or 1)
        if ar_s > ar_v:
            disp_w, disp_h = W, int(round(W/ar_s)); off_x, off_y = 0, (H-disp_h)//2
        else:
            disp_h, disp_w = H, int(round(H*ar_s)); off_x, off_y = (W-disp_w)//2, 0
        if not (off_x <= x < off_x+disp_w and off_y <= y < off_y+disp_h):
            return None
        u = int(round((x-off_x) * (self._pix.width()/float(disp_w))))
        v = int(round((y-off_y) * (self._pix.height()/float(disp_h))))
        return (u, v)

    def _draw_overlay(self):
        pm = QtGui.QPixmap(self._img_label.size()); pm.fill(QtCore.Qt.transparent)
        p = QtGui.QPainter(pm); p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        px = self._img_label.pixmap()
        if px and self._uvs:
            w, h = px.width(), px.height()
            W, H = self._img_label.width(), self._img_label.height()
            ar_s = w/float(h); ar_v = W/float(H or 1)
            if ar_s > ar_v:
                disp_w, disp_h = W, int(round(W/ar_s)); off_x, off_y = 0, (H-disp_h)//2
            else:
                disp_h, disp_w = H, int(round(H*ar_s)); off_x, off_y = (W-disp_w)//2, 0
            pen = QtGui.QPen(QtGui.QColor(255, 90, 90), 2); p.setPen(pen)
            p.setBrush(QtGui.QBrush(QtGui.QColor(255, 90, 90, 150)))
            font = QtGui.QFont(); font.setPointSize(10); p.setFont(font)
            for i, (u,v) in enumerate(self._uvs, start=1):
                x = off_x + (u/float(w))*disp_w
                y = off_y + (v/float(h))*disp_h
                p.drawEllipse(QtCore.QPointF(x,y), 5,5)
                p.drawText(QtCore.QPointF(x+8, y-8), str(i))
        p.end()
        self._overlay.setPixmap(pm)

    def _on_map_clicked(self, xs: float, ys: float):
        if self._awaiting != "map":
            return
        it = QtWidgets.QGraphicsEllipseItem(-4, -4, 8, 8)
        it.setPen(QtGui.QPen(QtGui.QColor(90, 220, 255), 2))
        it.setBrush(QtGui.QBrush(QtGui.QColor(90, 220, 255, 160)))
        it.setZValue(80); it.setPos(xs, ys)
        self._view.scene().addItem(it)
        num = QtWidgets.QGraphicsSimpleTextItem(str(len(self._xys)+1))
        num.setBrush(QtGui.QBrush(QtGui.QColor(255,255,255)))
        num.setPen(QtGui.QPen(QtGui.QColor(0,0,0)))
        num.setZValue(81); num.setPos(xs+8, ys-8)
        self._view.scene().addItem(num)
        self._tmp_items += [it, num]

        self._xys.append((xs, ys))
        self._awaiting = "image"
        self._update_lbl()

    def _undo_last(self):
        if self._xys and self._uvs:
            self._xys.pop(); self._uvs.pop()
            if self._tmp_items:
                try: t = self._tmp_items.pop(); t.scene().removeItem(t)
                except Exception: pass
            if self._tmp_items:
                try: t = self._tmp_items.pop(); t.scene().removeItem(t)
                except Exception: pass
            self._draw_overlay(); self._update_lbl()

    def _clear(self):
        for it in self._tmp_items:
            try: it.scene().removeItem(it)
            except Exception: pass
        self._tmp_items.clear()
        self._uvs.clear(); self._xys.clear()
        self._awaiting = "image"
        self._draw_overlay(); self._update_lbl()

    def _update_lbl(self):
        n = min(len(self._uvs), len(self._xys))
        self.lbl.setText(f"Pairs: {n} (need 4–8). Next: {'image' if self._awaiting=='image' else 'map'}")
        self._ok.setEnabled(4 <= n <= 8)

    def exec(self):
        try:
            return super().exec()
        finally:
            for it in self._tmp_items:
                try: it.scene().removeItem(it)
                except Exception: pass
            self._tmp_items.clear()

    def result(self) -> Optional[Tuple[List[Tuple[int,int]], List[Tuple[float,float]], Tuple[int,int]]]:
        n = min(len(self._uvs), len(self._xys))
        if n < 4: return None
        return (self._uvs[:n], self._xys[:n], (self._pix.width(), self._pix.height()))


class SinglePickDialog(QtWidgets.QDialog):
    def __init__(self, qimage: QtGui.QImage, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Pick point (snapshot)")
        self.setModal(True)
        self.setWindowFlags(self.windowFlags()
                            | QtCore.Qt.WindowMaximizeButtonHint
                            | QtCore.Qt.WindowMinimizeButtonHint)
        self.setSizeGripEnabled(True)
        self.resize(1000, 700)
        self.setCursor(QtCore.Qt.CrossCursor)
        self._pix = QtGui.QPixmap.fromImage(qimage)
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
        self._ok = bb.button(QtWidgets.QDialogButtonBox.Ok); self._ok.setEnabled(False)
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
                    self._uv = uv; self._redraw(); self._ok.setEnabled(True)
                return True
            elif ev.type() in (QtCore.QEvent.Resize, QtCore.QEvent.Show):
                self._overlay.setGeometry(self._view.rect()); self._redraw()
        return super().eventFilter(obj, ev)

    def _redraw(self):
        pm = QtGui.QPixmap(self._view.size()); pm.fill(QtCore.Qt.transparent)
        if self._uv is not None:
            p = QtGui.QPainter(pm); p.setRenderHint(QtGui.QPainter.Antialiasing, True)
            px = self._view.pixmap(); w, h = px.width(), px.height()
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
            p.drawEllipse(QtCore.QPointF(x,y), 5,5); p.end()
        self._overlay.setPixmap(pm)

    def picked_uv(self) -> Optional[Tuple[int,int]]:
        return self._uv


# ---------------- main module ----------------
class Img2GroundModule(QtCore.QObject):
    title = "Image → Ground"
    icon = None

    def __init__(self, vlc_instance: vlc.Instance, log_func=print):
        super().__init__()
        self._log = log_func
        self._vlc = vlc_instance
        self._cfg = load_cfg()

        # video
        self.video = VlcVideoWidget(self._vlc)
        self._player = self.video.player()
        self._media: Optional[vlc.Media] = None

        # world
        self._bundle: Optional[Dict[str, Any]] = None
        self._dtm: Optional[DTM] = None
        self._map: Optional[MapView] = None
        self._ortho_layer: Optional[RasterLayer] = None
        self._ortho_pix: Optional[QtWidgets.QGraphicsPixmapItem] = None

        # homography
        self._H: Optional[np.ndarray] = None
        self._calib_img_wh: Optional[Tuple[int,int]] = None
        self._frame_item: Optional[QtWidgets.QGraphicsPathItem] = None

        # PTZ
        self._ptz_meta: Optional[PTZClient] = None
        self._ptz_last: PTZReading = PTZReading()
        self._yaw_offset_deg: Optional[float] = None
        self._hfov_deg: Optional[float] = None
        self._fx_from_hfov: Optional[float] = None
        self._fov_items: List[QtWidgets.QGraphicsLineItem] = []
        self._azimuth_item: Optional[QtWidgets.QGraphicsLineItem] = None

        # last pick
        self._last_geo = None
        self._last_pick_item: Optional[QtWidgets.QGraphicsEllipseItem] = None
        self._last_pick_label: Optional[QtWidgets.QGraphicsSimpleTextItem] = None

        self._root = self._build_ui()
        self._attach_vlc_events()
        QtCore.QTimer.singleShot(0, self._apply_layers_from_shared)
        self.chk_use_active.blockSignals(True)
        self.chk_use_active.setChecked(True)
        self.chk_use_active.blockSignals(False)
        try:
            self.btn_play_rtsp.hide(); self.btn_play_file.hide()
        except Exception:
            pass
        self._t = QtCore.QTimer(self._root); self._t.timeout.connect(self._update_metrics); self._t.start(800)
        self._ptz_timer = QtCore.QTimer(self._root); self._ptz_timer.timeout.connect(self._poll_ptz_ui); self._ptz_timer.start(400)
        self._az_btn_timer = QtCore.QTimer(self._root)
        self._az_btn_timer.setInterval(800)
        self._az_btn_timer.timeout.connect(self._refresh_az_btn_state)
        self._az_btn_timer.timeout.connect(self._refresh_readiness)
        self._az_btn_timer.start()
        shared_state.signal_camera_changed.connect(self._on_active_camera_changed)
        shared_state.signal_stream_mode_changed.connect(self._on_stream_mode_changed)
        shared_state.signal_camera_changed.connect(lambda ctx: self._apply_layers_from_shared())
        shared_state.signal_layers_changed.connect(self._on_layers_changed)
        bus.signal_camera_changed.connect(lambda ctx: self._apply_layers_from_shared())
        bus.signal_ortho_changed.connect(self._on_ortho_changed)
        if app_state.current_camera:
            self.use_active_camera(force=True)
        self._on_stream_mode_changed(getattr(app_state, "stream_mode", "online"))
        self._refresh_readiness()
        self._refresh_az_btn_state()
        self._refresh_level_btn_state()

    def widget(self) -> QtWidgets.QWidget:
        return self._root

    def _persist_ctx_offsets(self, ctx) -> None:
        """Store current context offsets in the project and profile."""
        proj = getattr(app_state, "project", None)
        alias = getattr(ctx, "alias", None)
        yaw = getattr(ctx, "yaw_offset_deg", 0.0)
        pitch = getattr(ctx, "pitch_offset_deg", 0.0)
        roll = getattr(ctx, "roll_offset_deg", 0.0)

        if proj is not None and alias:
            d = getattr(proj, "offset_for_camera", {}) or {}
            d[alias] = {
                "yaw_offset_deg": yaw,
                "pitch_offset_deg": pitch,
                "roll_offset_deg": roll,
            }
            proj.offset_for_camera = d

        if alias:
            try:
                profiles = []
                if PROFILES_PATH.exists():
                    profiles = json.loads(PROFILES_PATH.read_text(encoding="utf-8"))
                updated = False
                for p in profiles:
                    if p.get("name") == alias:
                        p["yaw_offset_deg"] = yaw
                        p["pitch_offset_deg"] = pitch
                        p["roll_offset_deg"] = roll
                        updated = True
                        break
                if updated:
                    PROFILES_PATH.write_text(json.dumps(profiles, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass

        # Also persist offsets to the current bundle file if available
        bundle_path = getattr(self, "_bundle_path", None)
        if bundle_path:
            try:
                data = json.loads(Path(bundle_path).read_text(encoding="utf-8"))
                data["yaw_offset_deg"] = yaw
                data["pitch_offset_deg"] = pitch
                data["roll_offset_deg"] = roll
                Path(bundle_path).write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except Exception:
                pass

    # ----- UI -----
    def _build_ui(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget(); g = QtWidgets.QGridLayout(w); r = 0

        # bundle
        g.addWidget(QtWidgets.QLabel("<b>Bundle</b>"), r, 0, 1, 8); r += 1
        self.cmb_bundle = QtWidgets.QComboBox(); self._refresh_bundles()
        btn_load = QtWidgets.QPushButton("Load"); btn_load.clicked.connect(self._on_load_bundle)
        self.lbl_bundle = QtWidgets.QLabel("(no bundle)")
        g.addWidget(QtWidgets.QLabel("Existing:"), r, 0)
        g.addWidget(self.cmb_bundle, r, 1, 1, 3)
        g.addWidget(btn_load, r, 4)
        g.addWidget(self.lbl_bundle, r, 5, 1, 3); r += 1

        # mode
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

        # splitter
        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal); splitter.setChildrenCollapsible(False); splitter.setHandleWidth(8)
        self.video.setMinimumHeight(360); splitter.addWidget(self.video)
        self._map = MapView(); self._map.setMinimumHeight(360); splitter.addWidget(self._map)
        self._map.clicked.connect(self._on_map_click)
        splitter.setStretchFactor(0,1); splitter.setStretchFactor(1,1)
        g.addWidget(splitter, r, 0, 1, 8); r += 1

        # Ortho/DTM are managed ONLY in Preparation tab
        rowm = QtWidgets.QHBoxLayout()
        self.lbl_layers_source = QtWidgets.QLabel("Layers source: Preparation (read-only)")
        self.ed_dtm = QtWidgets.QLineEdit(); self.ed_dtm.setReadOnly(True); self.ed_dtm.setPlaceholderText("DTM path (managed in Preparation)")
        self.ed_ortho = QtWidgets.QLineEdit(); self.ed_ortho.setReadOnly(True); self.ed_ortho.setPlaceholderText("Orthophoto path (managed in Preparation)")
        self.btn_open_prep = QtWidgets.QToolButton(); self.btn_open_prep.setText("Open Preparation…")
        self.btn_open_prep.clicked.connect(self._open_prep_tab)
        rowm.addWidget(self.lbl_layers_source)
        rowm.addWidget(self.ed_dtm, 1)
        rowm.addWidget(self.ed_ortho, 1)
        rowm.addWidget(self.btn_open_prep)
        g.addLayout(rowm, r, 0, 1, 8); r += 1

        # Reset calibration button
        self.btn_reset = QtWidgets.QPushButton("Reset calibration")
        self.btn_reset.clicked.connect(self._reset_calibration)
        g.addWidget(self.btn_reset, r, 0, 1, 8); r += 1

        # readiness indicators
        ready_box = QtWidgets.QGroupBox("Calibration readiness")
        lr = QtWidgets.QVBoxLayout(ready_box)
        self.chk_ortho = QtWidgets.QCheckBox("Ortho")
        self.chk_ortho.setEnabled(False)
        self.chk_camxy = QtWidgets.QCheckBox("Camera XY")
        self.chk_camxy.setEnabled(False)
        self.chk_ptz = QtWidgets.QCheckBox("PTZ pan")
        self.chk_ptz.setEnabled(False)
        self.chk_intr = QtWidgets.QCheckBox("Intrinsics")
        self.chk_intr.setEnabled(False)
        lr.addWidget(self.chk_ortho)
        lr.addWidget(self.chk_camxy)
        lr.addWidget(self.chk_ptz)
        lr.addWidget(self.chk_intr)
        g.addWidget(ready_box, r, 0, 1, 8); r += 1

        # simple calibration group
        grp_simple = QtWidgets.QGroupBox("Calibration (Simple)")
        gls = QtWidgets.QHBoxLayout(grp_simple)
        self.btn_level_horizon = QtWidgets.QPushButton("Level from horizon…")
        self.btn_level_horizon.setToolTip("לחץ 2 נק’ על קו האופק בתמונה כדי לאפס roll/pitch.")
        self.btn_level_horizon.clicked.connect(self._calibrate_from_horizon)
        self.btn_az_from_ortho = QtWidgets.QPushButton("Azimuth from ortho…")
        self._az_tip_ready = "בחר נקודה/ות באורתו שהמצלמה מביטה אליהן. ההיסט אזימוט יחושב מול ה-pan החי."
        self._az_tip_wait = "מחכה ל-PTZ / טען Ortho"
        self.btn_az_from_ortho.setToolTip(self._az_tip_ready)
        self.btn_az_from_ortho.clicked.connect(self._calibrate_azimuth_from_ortho)
        gls.addWidget(self.btn_level_horizon); gls.addWidget(self.btn_az_from_ortho)
        g.addWidget(grp_simple, r, 0, 1, 8); r += 1

        # camera model / calibration
        self.btn_level_horizon.setEnabled(False)
        self.btn_az_from_ortho.setEnabled(False)
        self.chk_use_active = QtWidgets.QCheckBox("Use active camera (from RTSP tab)")
        self.chk_use_active.toggled.connect(self.use_active_camera)
        self.btn_refresh_cam = QtWidgets.QPushButton("\uD83D\uDD04 Refresh from live")
        self.btn_refresh_cam.clicked.connect(lambda: self.use_active_camera(force=True))
        self.chk_lock_cam = QtWidgets.QCheckBox("\U0001F512 Lock")
        self.chk_lock_cam.toggled.connect(self._update_lock_cam)
        hrow = QtWidgets.QHBoxLayout()
        hrow.addWidget(self.chk_use_active)
        hrow.addWidget(self.btn_refresh_cam)
        hrow.addWidget(self.chk_lock_cam)
        hrow.addStretch(1)
        g.addLayout(hrow, r, 0, 1, 8); r += 1

        grp_cam = QtWidgets.QGroupBox("Camera Model (Calibration)")
        grp_cam.setToolTip("This refers to intrinsic/extrinsic parameters. Click 'Use active camera' to import from your live connection.")
        glc = QtWidgets.QGridLayout(grp_cam)
        self.fx = QtWidgets.QDoubleSpinBox(); self.fx.setRange(-1e6, 1e6)
        self.fy = QtWidgets.QDoubleSpinBox(); self.fy.setRange(-1e6, 1e6)
        self.cx = QtWidgets.QDoubleSpinBox(); self.cx.setRange(-1e6, 1e6)
        self.cy = QtWidgets.QDoubleSpinBox(); self.cy.setRange(-1e6, 1e6)
        self.k1 = QtWidgets.QDoubleSpinBox(); self.k1.setRange(-1e3, 1e3)
        self.k2 = QtWidgets.QDoubleSpinBox(); self.k2.setRange(-1e3, 1e3)
        self.p1 = QtWidgets.QDoubleSpinBox(); self.p1.setRange(-1e3, 1e3)
        self.p2 = QtWidgets.QDoubleSpinBox(); self.p2.setRange(-1e3, 1e3)
        self.k3 = QtWidgets.QDoubleSpinBox(); self.k3.setRange(-1e3, 1e3)
        glc.addWidget(QtWidgets.QLabel("fx"),0,0); glc.addWidget(self.fx,0,1)
        glc.addWidget(QtWidgets.QLabel("fy"),0,2); glc.addWidget(self.fy,0,3)
        glc.addWidget(QtWidgets.QLabel("cx"),1,0); glc.addWidget(self.cx,1,1)
        glc.addWidget(QtWidgets.QLabel("cy"),1,2); glc.addWidget(self.cy,1,3)
        glc.addWidget(QtWidgets.QLabel("k1"),2,0); glc.addWidget(self.k1,2,1)
        glc.addWidget(QtWidgets.QLabel("k2"),2,2); glc.addWidget(self.k2,2,3)
        glc.addWidget(QtWidgets.QLabel("p1"),3,0); glc.addWidget(self.p1,3,1)
        glc.addWidget(QtWidgets.QLabel("p2"),3,2); glc.addWidget(self.p2,3,3)
        glc.addWidget(QtWidgets.QLabel("k3"),4,0); glc.addWidget(self.k3,4,1)
        g.addWidget(grp_cam, r, 0, 1, 8); r += 1
        for spn in (self.fx, self.fy, self.cx, self.cy):
            spn.valueChanged.connect(self._refresh_readiness)

        # PTZ group
        grp = QtWidgets.QGroupBox("PTZ / ONVIF")
        self.grp_ptz = grp
        gl = QtWidgets.QGridLayout(grp)
        self.ed_host = QtWidgets.QLineEdit("192.168.1.108")
        self.ed_port = QtWidgets.QSpinBox(); self.ed_port.setRange(1,65535); self.ed_port.setValue(80)
        self.ed_user = QtWidgets.QLineEdit(""); self.ed_pwd = QtWidgets.QLineEdit(""); self.ed_pwd.setEchoMode(QtWidgets.QLineEdit.Password)
        self.ptz_cgi_port = QtWidgets.QSpinBox(); self.ptz_cgi_port.setRange(1, 65535); self.ptz_cgi_port.setValue(int(self._cfg.get("ptz_cgi_port", 80)))
        self.ptz_cgi_channel = QtWidgets.QSpinBox(); self.ptz_cgi_channel.setRange(1, 16); self.ptz_cgi_channel.setValue(int(self._cfg.get("ptz_cgi_channel", 1)))
        self.ptz_cgi_poll = QtWidgets.QDoubleSpinBox(); self.ptz_cgi_poll.setRange(0.1, 30.0); self.ptz_cgi_poll.setSingleStep(0.5); self.ptz_cgi_poll.setValue(float(self._cfg.get("ptz_cgi_poll_hz", 5.0)))
        self.ptz_cgi_https = QtWidgets.QCheckBox("HTTPS"); self.ptz_cgi_https.setChecked(bool(self._cfg.get("ptz_cgi_https", False)))
        self.btn_ptz_connect = QtWidgets.QPushButton("Connect PTZ"); self.btn_ptz_connect.clicked.connect(self._connect_ptz)
        self.btn_ptz_from_cam = QtWidgets.QPushButton("Use from Cameras tab"); self.btn_ptz_from_cam.clicked.connect(self._ptz_load_from_shared)
        self.lbl_ptz = QtWidgets.QLabel("Pan=?, Tilt=?, Zoom=?, F(mm)=?")
        self.btn_fov_cal = QtWidgets.QPushButton("Calibrate FOV with PTZ…"); self.btn_fov_cal.clicked.connect(self._calibrate_fov_with_ptz)
        self.btn_fov_cal.setEnabled(False)
        gl.addWidget(QtWidgets.QLabel("Host:"), 0,0); gl.addWidget(self.ed_host,0,1)
        gl.addWidget(QtWidgets.QLabel("Port:"), 0,2); gl.addWidget(self.ed_port,0,3)
        gl.addWidget(self.btn_ptz_from_cam, 0,4)
        gl.addWidget(QtWidgets.QLabel("User:"), 1,0); gl.addWidget(self.ed_user,1,1)
        gl.addWidget(QtWidgets.QLabel("Pass:"), 1,2); gl.addWidget(self.ed_pwd,1,3)
        gl.addWidget(self.btn_ptz_connect, 1,4)
        gl.addWidget(QtWidgets.QLabel("CGI port:"), 2,0); gl.addWidget(self.ptz_cgi_port,2,1)
        gl.addWidget(QtWidgets.QLabel("Chan:"), 2,2); gl.addWidget(self.ptz_cgi_channel,2,3)
        gl.addWidget(self.ptz_cgi_https,2,4)
        gl.addWidget(QtWidgets.QLabel("CGI Hz:"),3,0); gl.addWidget(self.ptz_cgi_poll,3,1)
        gl.addWidget(self.lbl_ptz,4,0,1,5)
        gl.addWidget(self.btn_fov_cal,5,0,1,5)
        self.ptz_cgi_port.valueChanged.connect(lambda _: self._save_app_cfg())
        self.ptz_cgi_channel.valueChanged.connect(lambda _: self._save_app_cfg())
        self.ptz_cgi_poll.valueChanged.connect(lambda _: self._save_app_cfg())
        self.ptz_cgi_https.stateChanged.connect(lambda _: self._save_app_cfg())
        g.addWidget(grp, r, 0, 1, 8); r += 1

        self.lbl_ptz_status = QtWidgets.QLabel("PTZ: not connected")
        self.lbl_ptz_status.setStyleSheet("color: #9ec;")
        g.addWidget(self.lbl_ptz_status, r, 0, 1, 8); r += 1

        self.chk_use_active.toggled.connect(self._update_ptz_visibility)
        QtCore.QTimer.singleShot(0, self._update_ptz_visibility)

        # mapping mode + pick
        rowp = QtWidgets.QHBoxLayout()
        self.cmb_mapping = QtWidgets.QComboBox()
        self.cmb_mapping.addItems(["Auto (prefer Homography)", "Homography only", "PTZ+DTM only"])
        self.btn_pick_now = QtWidgets.QPushButton("Pick now"); self.btn_pick_now.clicked.connect(self._pick_now_snapshot)
        self.btn_pick_now.setToolTip("לכידת פריים ותרגום לנ״צ/‏XY; נשלח גם ל-QR.")
        rowp.addWidget(QtWidgets.QLabel("Mapping mode:")); rowp.addWidget(self.cmb_mapping)
        rowp.addStretch(1); rowp.addWidget(self.btn_pick_now)
        g.addLayout(rowp, r, 0, 1, 8); r += 1

        # utilities for last coordinates
        row2 = QtWidgets.QHBoxLayout()
        self.btn_copy = QtWidgets.QPushButton("Copy last coords"); self.btn_copy.clicked.connect(self._copy_last)
        self.btn_qr = QtWidgets.QPushButton("Show QR"); self.btn_qr.clicked.connect(self._show_qr)
        row2.addWidget(self.btn_copy); row2.addWidget(self.btn_qr); row2.addStretch(1)
        g.addLayout(row2, r, 0, 1, 8); r += 1

        # status + metrics
        self.lbl_status = QtWidgets.QLabel(""); g.addWidget(self.lbl_status, r, 0, 1, 8); r += 1
        self.lbl_metrics = QtWidgets.QLabel("State: idle | 0x0 | FPS:?"); g.addWidget(self.lbl_metrics, r, 0, 1, 8); r += 1

        g.setRowStretch(r, 1)
        self._update_mode_enabled()
        self._update_lock_cam()
        return w

    def _save_app_cfg(self) -> None:
        self._cfg.update({
            "ptz_cgi_port": self.ptz_cgi_port.value(),
            "ptz_cgi_channel": self.ptz_cgi_channel.value(),
            "ptz_cgi_poll_hz": self.ptz_cgi_poll.value(),
            "ptz_cgi_https": self.ptz_cgi_https.isChecked(),
        })
        save_cfg(self._cfg)

    # ----- VLC -----
    def _attach_vlc_events(self):
        ev = self._player.event_manager()
        ev.event_attach(vlc.EventType.MediaPlayerPlaying, lambda e: self._on_vlc_playing())
        ev.event_attach(vlc.EventType.MediaPlayerEncounteredError, lambda e: self._log("VLC: EncounteredError"))
        ev.event_attach(vlc.EventType.MediaPlayerEndReached, lambda e: self._log("VLC: EndReached"))

    def _on_vlc_playing(self):
        self._log("VLC: Playing")
        self._refresh_level_btn_state()
        self._refresh_readiness()
        self._refresh_az_btn_state()

    def _update_mode_enabled(self):
        on = self.rb_online.isChecked()
        self.ed_rtsp.setEnabled(on); self.btn_play_rtsp.setEnabled(on)
        self.ed_file.setEnabled(not on); self.btn_browse.setEnabled(not on); self.btn_play_file.setEnabled(not on)

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

    def _set_media(self, mrl: str, is_file: bool, ctx=None):
        try:
            if is_file:
                p = Path(mrl); uri = p.as_uri()
                media = self._vlc.media_new(uri)
                if hasattr(media, "add_option"):
                    media.add_option(":file-caching=1200")
                    if p.suffix.lower() == ".avi":
                        media.add_option(":demux=avi")
            else:
                media = self._vlc.media_new(mrl)
                if hasattr(media, "add_option"):
                    media.add_option(":network-caching=1200")
                    # TCP/UDP לפי ההקשר (ברירת־מחדל: TCP)
                    transport = getattr(ctx, "transport", "tcp") if ctx else "tcp"
                    if transport == "tcp":
                        media.add_option(":rtsp-tcp")
                    # אם יש user/pass מה־Active Camera – להוסיף
                    u = getattr(ctx, "user", None) if ctx else None
                    p = getattr(ctx, "pwd",  None) if ctx else None
                    if u:
                        media.add_option(f":rtsp-user={u}")
                    if p:
                        media.add_option(f":rtsp-pwd={p}")
            if hasattr(media, "add_option"):
                media.add_option(":clock-jitter=0")
                media.add_option(":avcodec-hw=none")
                media.add_option(":no-video-title-show")
            self._media = media; self._player.set_media(self._media); self._player.play()
            QtCore.QTimer.singleShot(150, lambda: self.video.ensure_video_out())
            self._log(f"Playing: {mrl}"); self.lbl_status.setText(f"Playing: {mrl}")
        except Exception as e:
            QtWidgets.QMessageBox.warning(None, "VLC", f"Failed to play:\n{e}")
            self._log(f"_set_media failed: {e}")

    def _update_lock_cam(self):
        lock = self.chk_lock_cam.isChecked()
        for w in (self.fx, self.fy, self.cx, self.cy, self.k1, self.k2, self.p1, self.p2, self.k3):
            w.setReadOnly(lock)

    def _update_ptz_visibility(self):
        use = self.chk_use_active.isChecked()
        self.grp_ptz.setVisible(not use)
        self.lbl_ptz_status.setVisible(True)

    def _on_active_camera_changed(self, ctx) -> None:
        if ctx is not None:
            self._yaw_offset_deg = getattr(ctx, "yaw_offset_deg", None)
        if self.chk_use_active.isChecked() and ctx and getattr(ctx, "rtsp_url", None):
            try: self.ed_rtsp.setText(ctx.rtsp_url)
            except Exception: pass
            self._set_media(ctx.rtsp_url, is_file=False, ctx=ctx)
            self._log("IG: playing from active camera")

        meta = getattr(app_state, "ptz_meta", None)
        if meta is None:
            meta = getattr(shared_state, "ptz_meta", None)
        if meta:
            try:
                self._ptz_meta = meta
                if hasattr(meta, "last"):
                    self._ptz_last = meta.last()
                self.lbl_ptz_status.setText("PTZ: attached (shared)")
                self._refresh_readiness()
                self._refresh_az_btn_state()
                return
            except Exception:
                pass

        if ctx and getattr(ctx, "host", None):
            self.ed_host.setText(ctx.host or "")
            self.ed_user.setText(getattr(ctx, "user", "") or "")
            self.ed_pwd.setText(getattr(ctx, "pwd", "") or "")
            try:
                host = getattr(ctx, "host", "") or ""
                user = getattr(ctx, "user", "") or ""
                pwd  = getattr(ctx, "pwd",  "") or ""
                port = getattr(ctx, "port", 80) or 80
                if host:
                    self._connect_ptz_auto(host, port, user, pwd)
            except Exception as e:
                self._log(f"Auto PTZ connect failed: {e}")

        self._refresh_readiness()
        self._refresh_az_btn_state()
        self._refresh_level_btn_state()

    def _on_stream_mode_changed(self, mode: str) -> None:
        ctx = app_state.current_camera
        if ctx and self.chk_use_active.isChecked():
            self._apply_active(ctx)

    def _apply_active(self, ctx) -> None:
        mode = getattr(app_state, "stream_mode", "online")
        if mode == "mockup":
            path = getattr(ctx, "mock_file", None)
            if not path:
                project = getattr(app_state, "project", None)
                alias = getattr(ctx, "alias", None)
                path = (getattr(project, "mock_file_for_camera", {}) or {}).get(alias)
            if not path:
                self._toast("No mockup file defined"); return
            self._set_media(path, is_file=True)
            self._log("IG: playing mockup")
        else:
            url = getattr(ctx, "rtsp_url", None)
            if not url:
                self._toast("No RTSP URL from active camera"); return
            self._set_media(url, is_file=False, ctx=ctx)

    def _toast(self, msg: str) -> None:
        QtWidgets.QToolTip.showText(QtGui.QCursor.pos(), msg, self._root)
        self._log(msg)

    def use_active_camera(self, force: bool = False):
        ctx = getattr(app_state, "current_camera", None)
        if not ctx:
            if force:
                QtWidgets.QMessageBox.warning(None, "Active camera", "No active camera")
            return
        if not force and not self.chk_use_active.isChecked():
            return
        if ctx.rtsp_url:
            self.ed_rtsp.setText(ctx.rtsp_url)
        if self.chk_lock_cam.isChecked():
            return
        if ctx.intrinsics:
            self.fx.setValue(ctx.intrinsics.fx)
            self.fy.setValue(ctx.intrinsics.fy)
            self.cx.setValue(ctx.intrinsics.cx)
            self.cy.setValue(ctx.intrinsics.cy)
        if ctx.distortion:
            self.k1.setValue(ctx.distortion.k1)
            self.k2.setValue(ctx.distortion.k2)
            self.p1.setValue(ctx.distortion.p1)
            self.p2.setValue(ctx.distortion.p2)
            if ctx.distortion.k3 is not None:
                self.k3.setValue(ctx.distortion.k3)
        self._refresh_readiness()
        self._refresh_az_btn_state()
        self._refresh_level_btn_state()

    # ----- Ortho / Map -----
    def _ensure_scene(self) -> QtWidgets.QGraphicsScene:
        sc = self._map.scene()
        if sc is None:
            sc = QtWidgets.QGraphicsScene(); self._map.setScene(sc)
        return sc


    def apply_ortho(self, layer: RasterLayer) -> None:
        try:
            self._ortho_layer = layer
            img = numpy_to_qimage(layer.downsampled_image())
            pix = QtGui.QPixmap.fromImage(img)
            sc = self._ensure_scene(); sc.clear()
            self._ortho_pix = QtWidgets.QGraphicsPixmapItem(pix); self._ortho_pix.setZValue(0); sc.addItem(self._ortho_pix)
            self._map.setSceneRect(self._ortho_pix.boundingRect())
            try:
                self._map.fit()
            except Exception:
                pass
            shared_state.orthophoto_path = layer.path
            self._log(f"Orthophoto loaded (EPSG={layer.ds.crs.to_epsg()})")
            self._remove_last_pick(); self._remove_video_frame_outline(); self._remove_fov_wedge()
            self._refresh_readiness()
            self._refresh_az_btn_state()
            self._refresh_level_btn_state()
        except Exception as e:
            QtWidgets.QMessageBox.warning(None, "Orthophoto", f"Failed to load: {e}")

    def _load_orthophoto(self, path: Optional[str] = None) -> None:
        if not path:
            self._open_prep_tab()
            return
        try:
            layer = RasterLayer(path, max_size=2048)
            self.apply_ortho(layer)
        except Exception as e:
            QtWidgets.QMessageBox.warning(None, "Orthophoto", f"Failed to load: {e}")

    def _on_ortho_changed(self, alias: str, layer) -> None:
        if alias == getattr(app_state.current_camera, "alias", None):
            self.apply_ortho(layer)

    def _on_layers_changed(self, alias: str, layers: dict) -> None:
        if alias == getattr(app_state.current_camera, "alias", None):
            self._apply_layers_from_shared()

    def _apply_layers_from_shared(self) -> None:
        alias = getattr(app_state.current_camera, "alias", None)
        if not alias:
            return
        layers = shared_state.layers_for_camera.get(alias)
        if layers is None:
            proj = getattr(app_state, "project", None)
            if proj is not None:
                layers = getattr(proj, "layers_for_camera", {}).get(alias)
        if not layers:
            return
        dtm = self._resolve_path(layers.get("dtm"))
        ortho = self._resolve_path(layers.get("ortho"))
        if dtm:
            self._load_dtm_path(dtm)
        if ortho:
            self._load_orthophoto_path(ortho)
        try:
            self._map.fit()
        except Exception:
            pass
        self.ed_dtm.setText(str(dtm) if dtm else "")
        self.ed_ortho.setText(str(ortho) if ortho else "")
        QtCore.QTimer.singleShot(0, self._refresh_readiness)
        QtCore.QTimer.singleShot(0, self._refresh_az_btn_state)
        QtCore.QTimer.singleShot(0, self._refresh_level_btn_state)

    def _load_dtm_path(self, path: str) -> None:
        try:
            if self._dtm is not None:
                self._dtm.close()
            self._dtm = DTM(path)
            self._dtm_path = path
        except Exception:
            pass

    def _load_orthophoto_path(self, path: str) -> None:
        try:
            self._load_orthophoto(path)
            # Flag the presence of an orthophoto for readiness checks
            self._ortho_layer = getattr(self._map, "ortho_layer", None) or True
        except Exception:
            pass

    def _open_prep_tab(self):
        QtWidgets.QMessageBox.information(None, "Preparation", "Load DTM/Orthophoto in the Preparation tab.")

    def _resolve_path(self, p: str | None) -> str | None:
        if not p:
            return None
        from pathlib import Path
        pp = Path(p)
        if pp.exists():
            return str(pp)
        proj = getattr(app_state, "project", None)
        root = getattr(proj, "root_dir", None) if proj else None
        if root and (Path(root) / pp).exists():
            return str((Path(root) / pp).resolve())
        return str(pp)

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
                    "meta": meta or {},
                }
                model_path = terrain_path
            else:
                self._bundle = dict(res)
                model_path = self._bundle.get("model_path") or self._bundle.get("terrain_path")
            if not model_path or not Path(model_path).exists():
                QtWidgets.QMessageBox.warning(None, "Bundle", f"DTM path not found:\n{model_path}"); return
            if self._dtm is not None:
                self._dtm.close()
            self._dtm = DTM(model_path)
            self._dtm_path = model_path
            self._bundle_path = CALIB_DIR / f"{name}.json"
            self.lbl_bundle.setText(f"Loaded: {name}"); self._log(f"Bundle loaded: {name}")
        except Exception as e:
            QtWidgets.QMessageBox.warning(None, "Bundle", f"Failed to load bundle: {e}")

    # ----- PTZ bridge to Cameras tab -----
    def _ptz_load_from_shared(self):
        cfg = getattr(shared_state, "onvif_cfg", None)
        if not cfg:
            QtWidgets.QMessageBox.information(None, "PTZ", "No ONVIF settings from Cameras tab.\n"
                                                           "Connect once there, it will populate shared_state.onvif_cfg")
            return
        self.ed_host.setText(cfg.get("host",""))
        self.ed_port.setValue(int(cfg.get("port", 80)))
        self.ed_user.setText(cfg.get("user",""))
        self.ed_pwd.setText(cfg.get("pwd",""))
        # connect immediately
        self._connect_ptz()

    # ----- PTZ -----
    def _connect_ptz(self):
        host = self.ed_host.text().strip()
        user = self.ed_user.text().strip()
        pwd = self.ed_pwd.text().strip()
        onvif_port = int(self.ed_port.value())

        try:
            if self._ptz_meta:
                self._ptz_meta.stop()
        except Exception:
            pass
        self._ptz_meta = None

        try:
            cgi_port = int(self.ptz_cgi_port.value())
            cgi_chan = int(self.ptz_cgi_channel.value())
            cgi_hz = float(self.ptz_cgi_poll.value())
            cgi_https = self.ptz_cgi_https.isChecked()
            self._ptz_meta = AnyPTZClient(
                host,
                onvif_port,
                user,
                pwd,
                cgi_port=cgi_port,
                cgi_channel=cgi_chan,
                cgi_poll_hz=cgi_hz,
                https=cgi_https,
            )
            if hasattr(self._ptz_meta, "updated"):
                try:
                    self._ptz_meta.updated.connect(self._on_ptz_update)
                except Exception:
                    pass
            self._ptz_meta.start()
            self.lbl_ptz_status.setText("PTZ: connecting…")
            if getattr(self._ptz_meta, "mode", None) == "onvif":
                self._log("PTZ: ONVIF connected")
            elif getattr(self._ptz_meta, "mode", None) == "cgi":
                proto = "https" if cgi_https else "http"
                self._log(f"PTZ: CGI connected ({proto})")
        except Exception as e:
            QtWidgets.QMessageBox.warning(None, "PTZ", str(e))
            self._ptz_meta = None

    def _connect_ptz_auto(self, host, port, user, pwd):
        try:
            if self._ptz_meta:
                self._ptz_meta.stop()
        except Exception:
            pass
        self._ptz_meta = AnyPTZClient(
            host,
            port,
            user,
            pwd,
            cgi_port=int(getattr(self.ptz_cgi_port, "value", lambda: 80)()),
            cgi_channel=int(getattr(self.ptz_cgi_channel, "value", lambda: 1)()),
            cgi_poll_hz=float(getattr(self.ptz_cgi_poll, "value", lambda: 4.5)()),
            https=bool(getattr(self.ptz_cgi_https, "isChecked", lambda: False)()),
        )
        if hasattr(self._ptz_meta, "updated"):
            try:
                self._ptz_meta.updated.connect(self._on_ptz_update)
            except Exception:
                pass
        self._ptz_meta.start()
        self.lbl_ptz_status.setText("PTZ: connecting…")

    def _poll_ptz_ui(self):
        if not self._ptz_meta:
            return
        try:
            self._on_ptz_update(self._ptz_meta.last())
        except Exception:
            pass

    def _on_ptz_update(self, last: PTZReading):
        self._ptz_last = last

        def fmt(v, spec):
            return "?" if v is None else format(float(v), spec)

        try:
            s = f"Pan={fmt(last.pan_deg,'.2f')}°, Tilt={fmt(last.tilt_deg,'.2f')}°, Zoom={fmt(last.zoom_norm,'.3f')}"
            if last.zoom_mm is not None:
                s += f", F(mm)={fmt(last.zoom_mm,'.2f')}"
            self.lbl_ptz.setText(s)
        except Exception:
            self.lbl_ptz.setText("Pan=?, Tilt=?, Zoom=?, F(mm)=?")

        proto = "ONVIF" if getattr(last, "via_onvif", False) else "CGI"
        zoom_val = last.zoom_mm if getattr(last, "zoom_mm", None) is not None else getattr(last, "zoom_norm", None)
        try:
            self.lbl_ptz_status.setText(
                f"PTZ: {proto} | pan={fmt(last.pan_deg,'.1f')}° tilt={fmt(last.tilt_deg,'.1f')}° zoom={fmt(zoom_val,'.2f')}"
            )
        except Exception:
            pass
        self._refresh_readiness()
        self._refresh_az_btn_state()
        self._refresh_level_btn_state()
        
    # ----- FOV calib via PTZ -----
    def _get_pan_now(self):
        # 1) מקומי (אם יש לקוח PTZ שרץ כאן)
        if (
            getattr(self, "_ptz_last", None)
            and getattr(self._ptz_last, "pan_deg", None) is not None
        ):
            return float(self._ptz_last.pan_deg)

        # 2) גלובלי: יכול להיות אובייקט עם last(), או dict ממוזג של CGI/ONVIF
        meta = getattr(app_state, "ptz_meta", None)
        if meta is not None:
            for cand in (
                getattr(meta, "cgi_last", None),
                getattr(meta, "onvif_last", None),
                meta.last() if hasattr(meta, "last") else meta,
            ):
                if cand is None:
                    continue
                pan = cand.get("pan_deg") if isinstance(cand, dict) else getattr(
                    cand, "pan_deg", None
                )
                if pan is not None:
                    try:
                        return float(pan)
                    except Exception:
                        pass

        # 3) מה־context (אם שמרת שם ערכים)
        ctx = getattr(app_state, "current_camera", None)
        if ctx and getattr(ctx, "pan_deg", None) is not None:
            return float(ctx.pan_deg)
        return None

    def _ready_flags(self) -> tuple[bool, bool, bool, bool]:
        has_ortho = bool(self._ortho_layer)
        has_xy = bool(getattr(shared_state, "camera_proj", None))
        has_pan = self._get_pan_now() is not None
        has_intr = bool(getattr(getattr(app_state, "current_camera", None), "intrinsics", None))
        return has_ortho, has_xy, has_pan, has_intr

    def _refresh_readiness(self) -> None:
        if not getattr(self, "_dbg_readiness_printed", False):
            self._log(
                f"DBG readiness: ortho={bool(self._ortho_layer)} "
                f"xy={bool(getattr(shared_state,'camera_proj',None))} "
                f"pan={self._get_pan_now()}"
            )
            self._dbg_readiness_printed = True
        has_ortho, has_xy, has_pan, has_intr = self._ready_flags()
        self.chk_ortho.setChecked(has_ortho)
        self.chk_camxy.setChecked(has_xy)
        self.chk_ptz.setChecked(has_pan)
        self.chk_intr.setChecked(has_intr)
        all_ok = has_ortho and has_xy and has_pan and has_intr
        self.btn_fov_cal.setEnabled(all_ok)

    def _refresh_az_btn_state(self) -> None:
        has_ortho, has_xy, has_pan, _ = self._ready_flags()
        enabled = has_ortho and has_xy and has_pan
        self.btn_az_from_ortho.setEnabled(enabled)
        tip = self._az_tip_ready if enabled else self._az_tip_wait
        self.btn_az_from_ortho.setToolTip(tip)

    def _refresh_level_btn_state(self) -> None:
        has_frame = bool(self.video.grab())
        self.btn_level_horizon.setEnabled(has_frame)

    def _calibrate_fov_with_ptz(self):
        """Calibrate yaw offset and FOV using PTZ telemetry.

        Uses the current pan angle reported by the camera and two
        user-picked ground points to determine the viewing direction.
        Zoom (focal length) and focus values from the PTZ feed are kept in
        ``self._ptz_last`` for completeness, although they are not directly
        used in this computation.
        """
        if self._ortho_layer is None:
            QtWidgets.QMessageBox.information(None, "FOV", "Load an orthophoto first."); return
        if self._ptz_last.pan_deg is None:
            QtWidgets.QMessageBox.information(None, "FOV", "PTZ not connected (no pan)."); return

        dlg = TwoPointsDialog(self._map, self._root)
        if dlg.exec() != QtWidgets.QDialog.Accepted: return
        pts = dlg.result_points()
        if not pts: return
        (xl, yl), (xr, yr) = pts

        try:
            Xl, Yl = self._ortho_layer.scene_to_geo(xl, yl)
            Xr, Yr = self._ortho_layer.scene_to_geo(xr, yr)
        except Exception as e:
            QtWidgets.QMessageBox.warning(None, "FOV", f"scene_to_geo failed: {e}"); return

        cam_proj = getattr(shared_state, "camera_proj", None)
        if not cam_proj:
            QtWidgets.QMessageBox.information(None, "FOV", "Camera position not set in Preparation."); return
        Xc, Yc = float(cam_proj["x"]), float(cam_proj["y"])

        def yaw_to(x, y):
            dx = x - Xc; dy = y - Yc
            return _normalize_angle_deg(math.degrees(math.atan2(dx, dy)))  # atan2(East, North)

        yaw_l = yaw_to(Xl, Yl); yaw_r = yaw_to(Xr, Yr)
        yaw_center_world = _mid_yaw_deg(yaw_l, yaw_r)
        hfov = abs(_angle_diff_deg(yaw_r, yaw_l))

        self._hfov_deg = hfov
        self._fx_from_hfov = None
        pan_now = self._ptz_last.pan_deg or 0.0
        self._yaw_offset_deg = _normalize_angle_deg(yaw_center_world - pan_now)

        self.lbl_status.setText(f"FOV calibrated: center={yaw_center_world:.2f}°, hfov={hfov:.2f}°, offset={self._yaw_offset_deg:.2f}°")
        self._draw_fov_wedge(Xc, Yc, yaw_l, yaw_r)
        self._remove_video_frame_outline()

    # ----- simple calibration helpers -----
    def _calibrate_from_horizon(self):
        # חייבים וידאו מנוגן; אחרת ה-snapshot יחזור שחור
        try:
            if not self._player or not self._player.is_playing():
                QtWidgets.QMessageBox.information(None, "Level from horizon", "Start the video first."); return
        except Exception:
            pass
        pm = self._grab_snapshot_pixmap()
        if pm is None or pm.isNull():
            QtWidgets.QMessageBox.information(None, "Level from horizon", "Snapshot failed."); return
        img = pm.toImage()
        dlg = HorizonDialog(img, self._root)
        if dlg.exec() != QtWidgets.QDialog.Accepted:
            return
        pts = dlg.result_points()
        if not pts:
            return
        (x1, y1), (x2, y2) = pts
        width = abs(x2 - x1)
        if width < 1e-6:
            QtWidgets.QMessageBox.information(None, "Level from horizon", "Points too close."); return
        roll = roll_error_from_horizon(y1, y2, width)
        y_hor = 0.5 * (y1 + y2)
        fy = self.fy.value() or 1.0
        cy = self.cy.value()
        pitch = math.degrees(math.atan((cy - y_hor) / fy))
        ctx = getattr(app_state, "current_camera", None)
        if ctx is not None:
            setattr(ctx, "roll_offset_deg", getattr(ctx, "roll_offset_deg", 0.0) - roll)
            setattr(ctx, "pitch_offset_deg", getattr(ctx, "pitch_offset_deg", 0.0) + pitch)
            # (אופציונלי) שמירת מטריקות מה-PTZ של אותו רגע
            last = getattr(self, "_ptz_last", None)
            if last:
                setattr(ctx, "tilt_at_level_deg", getattr(last, "tilt_deg", None))
                setattr(ctx, "zoom_at_level", getattr(last, "zoom", None))
            self._persist_ctx_offsets(ctx)
            shared_state.signal_camera_changed.emit(ctx)
        self.lbl_status.setText(f"Level applied: roll_offset={-roll:.2f}°, pitch≈{pitch:.2f}°")
        QtWidgets.QMessageBox.information(None, "Level from horizon",
            f"Applied offsets:\nRoll: {-roll:.2f}°\nPitch≈ {pitch:.2f}°")

    def _grab_snapshot_pixmap(self) -> QtGui.QPixmap | None:
        # לוקח snapshot דרך VLC לקובץ זמני ואז טוען ל-Pixmap (הדרך היציבה ב-Windows)
        try:
            from pathlib import Path
            import time, tempfile
            tmp = Path(tempfile.gettempdir()) / f"ig_snap_{int(time.time()*1000)}.png"
            try:
                self._player.video_take_snapshot(0, str(tmp), 0, 0)
            except Exception:
                return None
            # חכה קצת לכתיבה לדיסק
            for _ in range(6):
                if tmp.exists() and tmp.stat().st_size > 0:
                    pm = QtGui.QPixmap(str(tmp))
                    try:
                        tmp.unlink(missing_ok=True)
                    except Exception:
                        pass
                    return pm if not pm.isNull() else None
                QtCore.QThread.msleep(120)
        except Exception:
            pass
        return None

    def _calibrate_azimuth_from_ortho(self):
        if self._ortho_layer is None:
            QtWidgets.QMessageBox.information(None, "Azimuth from ortho", "Load an orthophoto first."); return
        pan_now = self._get_pan_now()
        if pan_now is None:
            QtWidgets.QMessageBox.information(None, "Azimuth from ortho", "PTZ not connected (no pan)."); return
        cam_proj = getattr(shared_state, "camera_proj", None)
        if not cam_proj:
            QtWidgets.QMessageBox.information(None, "Azimuth from ortho", "Camera position not set in Preparation."); return
        # Inline pick: קליק בודד על המפה
        self._toast("Click a point on the ortho…")
        self._map.enable_single_click(lambda xs, ys: self._on_azimuth_pick_scene(xs, ys, float(pan_now)))
        return

    def _on_azimuth_pick_scene(self, xs: float, ys: float, pan_now: float):
        if not self._ortho_layer:
            return
        Xc = float(getattr(shared_state, "camera_proj", {}).get("x", 0.0))
        Yc = float(getattr(shared_state, "camera_proj", {}).get("y", 0.0))
        try:
            X, Y = self._ortho_layer.scene_to_geo(xs, ys)
            dx = X - Xc; dy = Y - Yc
            az = _normalize_angle_deg(math.degrees(math.atan2(dx, dy)))
        except Exception as e:
            QtWidgets.QMessageBox.warning(None, "Azimuth from ortho", f"scene_to_geo failed: {e}"); return
        self._map.disable_single_click()
        yaw_avg = az
        self._yaw_offset_deg = _normalize_angle_deg(yaw_avg - pan_now)
        ctx = getattr(app_state, "current_camera", None)
        if ctx is not None:
            setattr(ctx, "yaw_offset_deg", getattr(ctx, "yaw_offset_deg", 0.0) + self._yaw_offset_deg)
            self._persist_ctx_offsets(ctx)
            shared_state.signal_camera_changed.emit(ctx)
        self.lbl_status.setText(f"Azimuth offset={self._yaw_offset_deg:.2f}° (bearing {yaw_avg:.2f}°)")
        QtWidgets.QMessageBox.information(None, "Azimuth from ortho",
            f"Bearing to point: {yaw_avg:.2f}°\nPan now: {pan_now:.2f}°\nApplied yaw offset: {self._yaw_offset_deg:.2f}°")
        pose_d = self._bundle.get("pose") if self._bundle else None
        georef = GeoRef.from_dict(self._bundle["georef"]) if self._bundle else None
        if pose_d and georef:
            o = np.array([pose_d.get("x", 0.0), pose_d.get("y", 0.0), pose_d.get("z", 0.0)])
            yaw_rad = math.radians(yaw_avg)
            d = np.array([math.sin(yaw_rad), math.cos(yaw_rad), 0.0])
            self._draw_azimuth_line(o, d, georef)
        self._refresh_readiness()
        self._refresh_az_btn_state()

    # ----- FOV wedge drawing -----
    def _remove_fov_wedge(self):
        for it in self._fov_items:
            try: it.scene().removeItem(it)
            except Exception: pass
        self._fov_items = []

    def _draw_fov_wedge(self, Xc: float, Yc: float, yaw_l: float, yaw_r: float, length: float = 200.0):
        self._remove_fov_wedge()
        if self._ortho_layer is None: return
        try:
            xl = Xc + length*math.sin(math.radians(yaw_l))
            yl = Yc + length*math.cos(math.radians(yaw_l))
            xr = Xc + length*math.sin(math.radians(yaw_r))
            yr = Yc + length*math.cos(math.radians(yaw_r))
            xsl, ysl = self._ortho_layer.geo_to_scene(xl, yl)
            xsr, ysr = self._ortho_layer.geo_to_scene(xr, yr)
            xsc, ysc = self._ortho_layer.geo_to_scene(Xc, Yc)
            sc = self._ensure_scene()
            pen = QtGui.QPen(QtGui.QColor(0, 255, 180), 2); pen.setCosmetic(True)
            l1 = sc.addLine(xsc, ysc, xsl, ysl, pen)
            l2 = sc.addLine(xsc, ysc, xsr, ysr, pen)
            l1.setZValue(20); l2.setZValue(20)
            self._fov_items = [l1, l2]
        except Exception:
            pass

    # ----- Azimuth line drawing -----
    def _remove_azimuth_line(self):
        if self._azimuth_item is not None:
            try: self._azimuth_item.scene().removeItem(self._azimuth_item)
            except Exception: pass
            self._azimuth_item = None

    def _draw_azimuth_line(self, o: np.ndarray, d: np.ndarray, georef: GeoRef, length: float = 200.0):
        self._remove_azimuth_line()
        if self._ortho_layer is None:
            return
        dir_xy = np.array([d[0], d[1]], dtype=float)
        n = np.linalg.norm(dir_xy)
        if n < 1e-6:
            return
        dir_xy /= n
        start = o
        end = o + np.array([dir_xy[0]*length, dir_xy[1]*length, 0.0])
        try:
            g1 = georef.local_to_geographic(start)
            g2 = georef.local_to_geographic(end)
            prj1, prj2 = g1.get("projected"), g2.get("projected")
            if prj1 and prj2:
                xs1, ys1 = self._ortho_layer.geo_to_scene(prj1["x"], prj1["y"])
                xs2, ys2 = self._ortho_layer.geo_to_scene(prj2["x"], prj2["y"])
                sc = self._ensure_scene()
                pen = QtGui.QPen(QtGui.QColor(255, 0, 0), 2); pen.setCosmetic(True)
                self._azimuth_item = sc.addLine(xs1, ys1, xs2, ys2, pen)
                self._azimuth_item.setZValue(25)
        except Exception:
            pass

    def _open_calib_tools(self):
        dlg = HorizonAzimuthCalibrationDialog(self._root)
        dlg.exec()

    # ----- Homography calibration -----
    def _run_homography_dialog(self):
        if self._ortho_layer is None:
            QtWidgets.QMessageBox.information(None, "Homography", "Load an orthophoto first."); return
        try: self._player.set_pause(True)
        except Exception: pass
        pm = self.video.grab()
        if pm.isNull():
            QtWidgets.QMessageBox.information(None, "Homography", "Failed to capture frame.")
            try: self._player.set_pause(False)
            except Exception: pass
            return
        img = pm.toImage()
        dlg = HomographyDialog(img, self._ensure_scene(), self._root)
        if dlg.exec() == QtWidgets.QDialog.Accepted:
            res = dlg.result()
            if res:
                uvs, xys, (iw, ih) = res
                try:
                    H = _homography_from_points(uvs, xys)
                    self._H = H
                    self._calib_img_wh = (iw, ih)
                    self.lbl_status.setText(f"Homography OK (pairs={len(uvs)}).")
                    self._draw_video_frame_outline()
                except Exception as e:
                    QtWidgets.QMessageBox.warning(None, "Homography", f"Failed to compute H: {e}")
        try: self._player.set_pause(False)
        except Exception: pass

    def _draw_video_frame_outline(self):
        self._remove_video_frame_outline()
        if self._H is None or self._ortho_layer is None or not self._calib_img_wh:
            return
        iw, ih = self._calib_img_wh
        corners = [(0,0), (iw-1,0), (iw-1,ih-1), (0,ih-1)]
        pts = []
        for u,v in corners:
            xs, ys = _apply_homography(self._H, (u, v))
            if not np.isfinite(xs) or not np.isfinite(ys):
                return
            pts.append(QtCore.QPointF(xs, ys))
        path = QtGui.QPainterPath(pts[0])
        for p in pts[1:]:
            path.lineTo(p)
        path.closeSubpath()
        sc = self._ensure_scene()
        item = QtWidgets.QGraphicsPathItem(path)
        pen = QtGui.QPen(QtGui.QColor(255, 200, 0), 2); pen.setCosmetic(True)
        brush = QtGui.QBrush(QtGui.QColor(255, 200, 0, 35))
        item.setPen(pen); item.setBrush(brush); item.setZValue(25)
        sc.addItem(item)
        self._frame_item = item

    def _remove_video_frame_outline(self):
        if self._frame_item is not None:
            try: self._frame_item.scene().removeItem(self._frame_item)
            except Exception: pass
            self._frame_item = None

    # ----- last-pick marker -----
    def _remove_last_pick(self):
        if self._last_pick_item is not None:
            try: self._last_pick_item.scene().removeItem(self._last_pick_item)
            except Exception: pass
            self._last_pick_item = None
        if self._last_pick_label is not None:
            try: self._last_pick_label.scene().removeItem(self._last_pick_label)
            except Exception: pass
            self._last_pick_label = None

    def _show_pick_on_map(self, xs: float, ys: float, text: Optional[str] = None):
        sc = self._ensure_scene()
        if self._last_pick_item is None:
            it = QtWidgets.QGraphicsEllipseItem(-5, -5, 10, 10)
            it.setBrush(QtGui.QBrush(QtGui.QColor(255, 220, 0)))
            it.setPen(QtGui.QPen(QtGui.QColor(30,30,30),1))
            it.setZValue(50); sc.addItem(it)
            self._last_pick_item = it
        self._last_pick_item.setPos(xs, ys)
        if text:
            if self._last_pick_label is None:
                lab = QtWidgets.QGraphicsSimpleTextItem("")
                lab.setBrush(QtGui.QBrush(QtGui.QColor(255,255,255)))
                lab.setPen(QtGui.QPen(QtGui.QColor(0,0,0)))
                lab.setZValue(51); sc.addItem(lab)
                self._last_pick_label = lab
            self._last_pick_label.setText(text); self._last_pick_label.setPos(xs+10, ys-10)
        try: self._map.centerOn(xs, ys)
        except Exception: pass
        self._map.viewport().update()

    # ----- map click -----
    def _on_map_click(self, xs: float, ys: float):
        sc = self._ensure_scene()
        dot = sc.addEllipse(-3, -3, 6, 6,
                            QtGui.QPen(QtGui.QColor(180,220,255),2),
                            QtGui.QBrush(QtGui.QColor(180,220,255)))
        dot.setPos(xs, ys)
        if self._ortho_layer is None: return
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

    # ----- Pick from snapshot -----
    def _pick_now_snapshot(self):
        try: self._player.set_pause(True)
        except Exception: pass
        pm = self.video.grab()
        if pm.isNull():
            QtWidgets.QMessageBox.information(None, "Pick", "Failed to capture frame.")
            try: self._player.set_pause(False)
            except Exception: pass
            return
        img = pm.toImage()
        dlg = SinglePickDialog(img, self._root)
        if dlg.exec() == QtWidgets.QDialog.Accepted and dlg.picked_uv() is not None:
            uu, vv = dlg.picked_uv()
            self._map_from_click(uu, vv, uv_in_calib_space=True)
        try: self._player.set_pause(False)
        except Exception: pass

    # ----- core mapping switch -----
    def _map_from_click(self, u: int, v: int, uv_in_calib_space: bool = False):
        mode = self.cmb_mapping.currentText()
        prefer_h = (mode.startswith("Auto") or mode.startswith("Homography"))
        allow_ptz = (mode.startswith("Auto") or mode.startswith("PTZ"))

        used_h = False
        if prefer_h and self._H is not None and self._ortho_layer is not None:
            xs, ys = self._map_by_homography(u, v, uv_in_calib_space=uv_in_calib_space)
            if xs is not None:
                used_h = True
                self._finalize_show_coords(xs, ys)
                return

        if allow_ptz:
            ok = self._map_by_ptz(u, v)
            if ok:
                return

        if not used_h:
            QtWidgets.QMessageBox.information(None, "Image→Ground",
                    "No valid mapping (need Homography or PTZ+DTM with calibration).")

    def _map_by_homography(self, u: int, v: int, uv_in_calib_space: bool) -> Tuple[Optional[float], Optional[float]]:
        if self._H is None or self._ortho_layer is None or not self._calib_img_wh:
            return (None, None)
        uu, vv = float(u), float(v)
        if not uv_in_calib_space:
            try:
                vw, vh = self._player.video_get_size(0)
            except Exception:
                vw = self.video.width(); vh = self.video.height()
            iw, ih = self._calib_img_wh
            if vw and vh and iw and ih:
                uu = u * (iw / float(vw))
                vv = v * (ih / float(vh))
        xs, ys = _apply_homography(self._H, (uu, vv))
        if not (np.isfinite(xs) and np.isfinite(ys)):
            return (None, None)
        return (xs, ys)

    def _map_by_ptz(self, u: int, v: int) -> bool:
        if not (self._bundle and self._ortho_layer and self._yaw_offset_deg is not None):
            return False
        intr_d = self._bundle["intrinsics"]
        W = intr_d["width"]; H = intr_d["height"]
        if self._hfov_deg is not None:
            fx = (W/2.0) / math.tan(math.radians(self._hfov_deg/2.0))
            fy = fx; cx = W/2.0; cy = H/2.0
        else:
            fx = intr_d["fx"]; fy = intr_d["fy"]; cx = intr_d["cx"]; cy = intr_d["cy"]
        pose_d = self._bundle["pose"]
        yaw = (self._ptz_last.pan_deg or 0.0) + (self._yaw_offset_deg or 0.0)
        pitch = pose_d.get("pitch_deg", pose_d.get("pitch", 0.0))
        roll  = pose_d.get("roll_deg",  pose_d.get("roll", 0.0))
        from geom3d import CameraIntrinsics, CameraPose, intersect_ray_with_dtm
        intr = CameraIntrinsics(W, H, fx, fy, cx, cy)
        pose = CameraPose(pose_d["x"], pose_d["y"], pose_d["z"], yaw, pitch, roll)
        o, d = camera_ray_in_world(u, v, intr, pose)
        georef = GeoRef.from_dict(self._bundle["georef"])

        # always draw azimuth line
        self._draw_azimuth_line(o, d, georef)

        if self._dtm is None:
            self._remove_last_pick()
            self.lbl_status.setText("No DTM: showing azimuth only.")
            return True

        p = intersect_ray_with_dtm(o, d, self._dtm, georef)
        if p is None:
            self._remove_last_pick()
            self.lbl_status.setText("Missed DTM / No intersection."); return True
        g = georef.local_to_geographic(p)
        lla = g["lla"]; prj = g["projected"]
        try:
            epsg = self._ortho_layer.ds.crs.to_epsg()
            if prj and prj.get("epsg") == epsg:
                X, Y = prj["x"], prj["y"]
            else:
                from pyproj import Transformer
                tr = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
                X, Y = tr.transform(lla["lon"], lla["lat"])
            xs, ys = self._ortho_layer.geo_to_scene(X, Y)
            self._show_pick_on_map(xs, ys, text=f"{lla['lat']:.5f},{lla['lon']:.5f}")
            self.lbl_status.setText(f"PTZ: lat={lla['lat']:.7f}, lon={lla['lon']:.7f}, alt={lla['alt']:.2f}")
        except Exception:
            pass
        return True

    def _finalize_show_coords(self, xs: float, ys: float):
        self._show_pick_on_map(xs, ys)
        try:
            X, Y = self._ortho_layer.scene_to_geo(xs, ys)
            epsg = self._ortho_layer.ds.crs.to_epsg()
            from pyproj import Transformer
            tr = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
            lon, lat = tr.transform(X, Y)
            self._last_geo = {"lat": float(lat), "lon": float(lon), "epsg": epsg, "X": float(X), "Y": float(Y)}
            self.lbl_status.setText(f"CALIB: lat={lat:.7f}, lon={lon:.7f} | XY(EPSG:{epsg})=({X:.2f}, {Y:.2f})")
        except Exception as e:
            self.lbl_status.setText(f"scene_to_geo failed: {e}")

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
        img = qrcode.make(url)
        qim = ImageQt.ImageQt(img)
        pix = QtGui.QPixmap.fromImage(qim)
        dlg = QtWidgets.QDialog(); dlg.setWindowTitle("QR (Google Maps)")
        lay = QtWidgets.QVBoxLayout(dlg)
        lab = QtWidgets.QLabel(); lab.setPixmap(pix); lab.setAlignment(QtCore.Qt.AlignCenter); lay.addWidget(lab)
        lab2 = QtWidgets.QLabel(url); lab2.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse); lay.addWidget(lab2)
        dlg.resize(300, 340); dlg.exec()

    def _on_video_click(self, u: int, v: int):
        self._map_from_click(u, v, uv_in_calib_space=False)

    # ----- reset / misc -----
    def _reset_calibration(self):
        self._H = None; self._calib_img_wh = None; self._remove_video_frame_outline()
        self._hfov_deg = None; self._fx_from_hfov = None; self._yaw_offset_deg = None; self._remove_fov_wedge()
        self._remove_last_pick(); self._remove_azimuth_line()
        self.lbl_status.setText("Calibration cleared.")

    def _update_metrics(self):
        p = self._player
        if not p: return
        try: w, h = p.video_get_size(0)
        except Exception: w, h = 0, 0
        try: fps = p.get_fps() or 0.0
        except Exception: fps = 0.0
        st = p.get_state()
        self.lbl_metrics.setText(f"State: {st} | {w}x{h} | FPS:{fps:.1f}")


# ----- small dialogs -----
class HorizonDialog(QtWidgets.QDialog):
    def __init__(self, snapshot_img: QtGui.QImage, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Pick horizon points")
        self.setModal(True)
        v = QtWidgets.QVBoxLayout(self)
        v.addWidget(QtWidgets.QLabel("Click two points on the horizon."))
        self._view = _GraphicsClickView()
        sc = QtWidgets.QGraphicsScene(self._view)
        self._pix = QtGui.QPixmap.fromImage(snapshot_img)
        sc.addPixmap(self._pix)
        sc.setSceneRect(0, 0, self._pix.width(), self._pix.height())
        self._view.setScene(sc)
        self._view.setRenderHints(QtGui.QPainter.Antialiasing | QtGui.QPainter.SmoothPixmapTransform)
        self._view.clicked.connect(self._on_click)
        v.addWidget(self._view, 1)
        info = QtWidgets.QHBoxLayout()
        self.lbl = QtWidgets.QLabel("Picked: 0/2")
        self.btn_clear = QtWidgets.QPushButton("Clear"); self.btn_clear.clicked.connect(self._clear)
        info.addWidget(self.lbl); info.addStretch(1); info.addWidget(self.btn_clear)
        v.addLayout(info)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        self._ok = bb.button(QtWidgets.QDialogButtonBox.Ok); self._ok.setEnabled(False)
        v.addWidget(bb)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        self._xs: List[float] = []; self._ys: List[float] = []
        self._tmp: List[QtWidgets.QGraphicsEllipseItem] = []

    def _on_click(self, x: float, y: float):
        if len(self._xs) >= 2: return
        sc = self._view.scene()
        it = sc.addEllipse(-4, -4, 8, 8, QtGui.QPen(QtGui.QColor(0, 255, 0), 2),
                            QtGui.QBrush(QtGui.QColor(0, 255, 0, 120)))
        it.setZValue(50); it.setPos(x, y)
        self._tmp.append(it)
        self._xs.append(x); self._ys.append(y)
        self.lbl.setText(f"Picked: {len(self._xs)}/2"); self._ok.setEnabled(len(self._xs)==2)

    def _clear(self):
        self._xs.clear(); self._ys.clear()
        for it in self._tmp:
            try: it.scene().removeItem(it)
            except Exception: pass
        self._tmp.clear()
        self.lbl.setText("Picked: 0/2"); self._ok.setEnabled(False)

    def result_points(self) -> Optional[Tuple[Tuple[float,float], Tuple[float,float]]]:
        if len(self._xs) != 2: return None
        return ((self._xs[0], self._ys[0]), (self._xs[1], self._ys[1]))


class PointsDialog(QtWidgets.QDialog):
    def __init__(self, map_view: MapView, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Pick point(s) on orthophoto")
        self.setModal(True)
        self.setMinimumSize(400, 160)
        v = QtWidgets.QVBoxLayout(self)
        v.addWidget(QtWidgets.QLabel("Click one or more points on the orthophoto."))
        info = QtWidgets.QHBoxLayout()
        self.lbl = QtWidgets.QLabel("Picked: 0")
        self.btn_clear = QtWidgets.QPushButton("Clear"); self.btn_clear.clicked.connect(self._clear)
        info.addWidget(self.lbl); info.addStretch(1); info.addWidget(self.btn_clear)
        v.addLayout(info)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        self._ok = bb.button(QtWidgets.QDialogButtonBox.Ok); self._ok.setEnabled(False)
        v.addWidget(bb)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        self._map = map_view
        self._xs: List[float] = []; self._ys: List[float] = []
        self._tmp_items: List[QtWidgets.QGraphicsEllipseItem] = []
        self._conn = self._map.clicked.connect(self._on_map_click)

    def _on_map_click(self, xs: float, ys: float):
        self._xs.append(xs); self._ys.append(ys)
        sc = self._map.scene()
        it = sc.addEllipse(-4, -4, 8, 8, QtGui.QPen(QtGui.QColor(255, 180, 0), 2),
                           QtGui.QBrush(QtGui.QColor(255, 180, 0, 180)))
        it.setZValue(80); it.setPos(xs, ys)
        self._tmp_items.append(it)
        self.lbl.setText(f"Picked: {len(self._xs)}"); self._ok.setEnabled(len(self._xs) > 0)

    def _clear(self):
        self._xs.clear(); self._ys.clear()
        for it in self._tmp_items:
            try: it.scene().removeItem(it)
            except Exception: pass
        self._tmp_items.clear()
        self.lbl.setText("Picked: 0"); self._ok.setEnabled(False)

    def exec(self):
        try:
            return super().exec()
        finally:
            try: self._map.clicked.disconnect(self._on_map_click)
            except Exception: pass
            for it in self._tmp_items:
                try: it.scene().removeItem(it)
                except Exception: pass
            self._tmp_items.clear()

    def result_points(self) -> Optional[List[Tuple[float,float]]]:
        if not self._xs:
            return None
        return list(zip(self._xs, self._ys))


# ----- helper dialog for PTZ (left/right) -----
class TwoPointsDialog(QtWidgets.QDialog):
    def __init__(self, map_view: MapView, parent=None):
        super().__init__(parent)
        self.setWindowTitle("FOV calibration: pick Left & Right on orthophoto")
        self.setModal(True)
        self.setMinimumSize(700, 160)
        v = QtWidgets.QVBoxLayout(self)
        v.addWidget(QtWidgets.QLabel("Click two points on the orthophoto: LEFT then RIGHT edge."))
        info = QtWidgets.QHBoxLayout()
        self.lbl = QtWidgets.QLabel("Picked: 0/2")
        self.btn_clear = QtWidgets.QPushButton("Clear"); self.btn_clear.clicked.connect(self._clear)
        info.addWidget(self.lbl); info.addStretch(1); info.addWidget(self.btn_clear)
        v.addLayout(info)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        self._ok = bb.button(QtWidgets.QDialogButtonBox.Ok); self._ok.setEnabled(False)
        v.addWidget(bb)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        self._map = map_view; self._xs: List[float]=[]; self._ys: List[float]=[]
        self._tmp_items: List[QtWidgets.QGraphicsEllipseItem] = []
        self._conn = self._map.clicked.connect(self._on_map_click)

    def _on_map_click(self, xs: float, ys: float):
        if len(self._xs) >= 2: return
        self._xs.append(xs); self._ys.append(ys)
        sc = self._map.scene()
        it = sc.addEllipse(-4, -4, 8, 8, QtGui.QPen(QtGui.QColor(255, 180, 0), 2),
                           QtGui.QBrush(QtGui.QColor(255, 180, 0, 180)))
        it.setZValue(80); it.setPos(xs, ys)
        self._tmp_items.append(it)
        self.lbl.setText(f"Picked: {len(self._xs)}/2"); self._ok.setEnabled(len(self._xs)==2)

    def _clear(self):
        self._xs.clear(); self._ys.clear()
        for it in self._tmp_items:
            try: it.scene().removeItem(it)
            except Exception: pass
        self._tmp_items.clear()
        self.lbl.setText("Picked: 0/2"); self._ok.setEnabled(False)

    def exec(self):
        try:
            return super().exec()
        finally:
            try: self._map.clicked.disconnect(self._on_map_click)
            except Exception: pass
            for it in self._tmp_items:
                try: it.scene().removeItem(it)
                except Exception: pass
            self._tmp_items.clear()

    def result_points(self) -> Optional[Tuple[Tuple[float,float], Tuple[float,float]]]:
        if len(self._xs) != 2: return None
        return ((self._xs[0], self._ys[0]), (self._xs[1], self._ys[1]))

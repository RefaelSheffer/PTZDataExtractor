# ui_user_module.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Operational tab with live video preview and map overlay.

This module exposes a user-friendly workspace that combines the active
camera preview on the left and an orthophoto map on the right.  The
implementation intentionally keeps the logic lightweight – many of the
advanced geospatial features are placeholders and can be extended later
without touching the surrounding application.
"""

from __future__ import annotations

from io import BytesIO
from typing import Callable, Optional, Tuple

import vlc
import qrcode
from PySide6 import QtCore, QtWidgets, QtGui

from ui_img2ground_module import SinglePickDialog

from ui_common import VlcVideoWidget
from ui_map_tools import MapView, numpy_to_qimage
from raster_layer import RasterLayer
from app_state import app_state
import shared_state
from event_bus import bus


class UserTab(QtWidgets.QWidget):
    """Main widget used in the *USER* tab."""

    def __init__(self, vlc_instance: vlc.Instance, log_func: Callable[[str], None] = print, parent=None):
        super().__init__(parent)
        self._vlc = vlc_instance
        self._log = log_func
        self._az_item = None
        self._last_pick: Optional[Tuple[float, float]] = None
        self._last_pick_item: QtWidgets.QGraphicsEllipseItem | None = None
        self._last_pick_label: QtWidgets.QGraphicsSimpleTextItem | None = None
        self._ortho_layer: RasterLayer | None = None
        self._dtm_path: str | None = None

        # ----- toolbar -----
        bar = QtWidgets.QToolBar()
        self.act_pick = bar.addAction("Pick now")
        self.act_az = bar.addAction("Toggle Azimuth")
        self.act_copy = bar.addAction("Copy GMaps link")
        self.act_qr = bar.addAction("Show QR")
        self.cmb_mapping = QtWidgets.QComboBox()
        self.cmb_mapping.addItems(["Auto (prefer Homography)", "Homography only", "PTZ+DTM only"])
        bar.addWidget(self.cmb_mapping)

        # ----- layer selectors -----
        self.dtm_edit = QtWidgets.QLineEdit()
        self.ortho_edit = QtWidgets.QLineEdit()
        btn_load = QtWidgets.QPushButton("Override…")
        hrow = QtWidgets.QHBoxLayout()
        hrow.addWidget(QtWidgets.QLabel("DTM:"))
        hrow.addWidget(self.dtm_edit, 1)
        hrow.addWidget(QtWidgets.QLabel("Ortho:"))
        hrow.addWidget(self.ortho_edit, 1)
        hrow.addWidget(btn_load)

        # ----- video + map -----
        self.video_container = QtWidgets.QFrame()
        self.video_container.setLayout(QtWidgets.QVBoxLayout())
        self.video_container.layout().setContentsMargins(0, 0, 0, 0)
        self.map = MapView()

        splitter = QtWidgets.QSplitter()
        splitter.addWidget(self.video_container)
        splitter.addWidget(self.map)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)

        # ----- layout -----
        vbox = QtWidgets.QVBoxLayout(self)
        vbox.addWidget(bar)
        vbox.addLayout(hrow)
        self.lbl_calib = QtWidgets.QLabel("Calibration: N/A")
        vbox.addWidget(self.lbl_calib)
        vbox.addWidget(splitter, 1)

        # ----- signal wiring -----
        self.act_pick.triggered.connect(self.on_pick_now)
        self.act_az.triggered.connect(self.on_toggle_azimuth)
        self.act_copy.triggered.connect(self.on_copy_gmaps)
        self.act_qr.triggered.connect(self.on_make_qr)
        btn_load.clicked.connect(self.on_load_layers)

        bus.signal_camera_changed.connect(self._on_active_camera_changed)
        shared_state.signal_camera_changed.connect(
            lambda ctx: self._apply_layers_for(getattr(ctx, "alias", None))
        )
        shared_state.signal_layers_changed.connect(self._on_layers_changed)
        self.mount_video_preview()
        if app_state.current_camera:
            self._on_active_camera_changed(app_state.current_camera)

    # ------------------------------------------------------------------
    # UI helpers
    def mount_video_preview(self) -> None:
        """Embed a VLC player for the active camera."""
        lay = self.video_container.layout()
        while lay.count():
            w = lay.takeAt(0).widget()
            if w:
                w.deleteLater()

        cam = app_state.current_camera
        if not cam or not getattr(cam, "rtsp_url", None):
            lbl = QtWidgets.QLabel("No active camera")
            lbl.setAlignment(QtCore.Qt.AlignCenter)
            lay.addWidget(lbl)
            return

        vw = VlcVideoWidget(self._vlc)
        lay.addWidget(vw)
        vw.ensure_video_out()

        # Build media with options similar to the camera setup module so that
        # authentication and transport settings are respected. Without these
        # options some cameras would refuse the connection, resulting in no
        # video being shown in the *USER* tab.
        opts = [":avcodec-hw=none", ":network-caching=800", ":no-video-title-show"]
        if getattr(cam, "transport", "udp") == "tcp" or getattr(cam, "used_tcp", False):
            opts.append(":rtsp-tcp")
        media = self._vlc.media_new(cam.rtsp_url, *opts)
        if getattr(cam, "user", None):
            media.add_option(f":rtsp-user={cam.user}")
        if getattr(cam, "pwd", None):
            media.add_option(f":rtsp-pwd={cam.pwd}")

        player = vw.player()
        player.set_media(media)
        # Delay playback slightly so that the underlying widget is fully
        # realized and the native window handle is valid for VLC. Starting
        # the player too early can result in audio only or a black frame on
        # some platforms (especially Windows) because the video output has
        # not yet been bound. Using a singleShot timer ensures the call
        # happens after the current event loop iteration when the widget has
        # been shown.
        QtCore.QTimer.singleShot(100, player.play)

    # ------------------------------------------------------------------
    # Layers
    def on_load_layers(self, *, broadcast: bool = True) -> None:
        dtm = self.dtm_edit.text().strip()
        ortho = self.ortho_edit.text().strip()
        shared_state.dtm_path = dtm or None
        shared_state.orthophoto_path = ortho or None
        try:
            if ortho:
                self._ortho_layer = RasterLayer(ortho, max_size=2048)
                img = numpy_to_qimage(self._ortho_layer.downsampled_image())
                pix = QtGui.QPixmap.fromImage(img)
                sc = self.map.scene() or QtWidgets.QGraphicsScene()
                sc.clear()
                sc.addPixmap(pix)
                self.map.setScene(sc)
                self.map.fit()
                if not self._az_item:
                    self.on_toggle_azimuth()
            if dtm:
                self._dtm_path = dtm
            self._toast("Layers loaded")
            if broadcast:
                self._update_shared_layers()
        except Exception as e:  # pragma: no cover - UI feedback
            self._toast(f"Layer load failed: {e}", error=True)

    def _on_active_camera_changed(self, ctx):
        if not ctx:
            return
        self.mount_video_preview()
        layers = getattr(ctx, "layers", None)
        if layers:
            self.dtm_edit.setText(layers.get("dtm", ""))
            self.ortho_edit.setText(layers.get("ortho", ""))
            self.on_load_layers(broadcast=False)
        calib = getattr(ctx, "calibration", None)
        if calib:
            self._fill_calibration_ui(calib)

    def _update_shared_layers(self) -> None:
        alias = getattr(app_state.current_camera, "alias", "default")
        layers = {"dtm": self.dtm_edit.text().strip() or None,
                  "ortho": self.ortho_edit.text().strip() or None,
                  "srs": None}
        if app_state.current_camera:
            app_state.current_camera.layers = layers
        shared_state.layers_for_camera[alias] = layers
        shared_state.signal_layers_changed.emit(alias, layers)

    def _on_layers_changed(self, alias: str, layers: dict) -> None:
        if alias == getattr(app_state.current_camera, "alias", None):
            self._apply_layers(layers)

    def _apply_layers_for(self, alias: str | None) -> None:
        if not alias:
            return
        layers = shared_state.layers_for_camera.get(alias)
        if layers:
            self._apply_layers(layers)

    def _apply_layers(self, layers: dict) -> None:
        dtm = self._resolve_path(layers.get("dtm"))
        ortho = self._resolve_path(layers.get("ortho"))
        self.dtm_edit.setText(dtm or "")
        self.ortho_edit.setText(ortho or "")
        self.on_load_layers(broadcast=False)

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

    def _fill_calibration_ui(self, calib: dict) -> None:
        parts = []
        for key in ("fx", "fy", "cx", "cy", "k1", "k2", "p1", "p2", "k3"):
            if key in calib and calib[key] is not None:
                parts.append(f"{key}={calib[key]:.2f}")
        self.lbl_calib.setText("Calibration: " + ", ".join(parts) if parts else "Calibration: N/A")

    # ------------------------------------------------------------------
    # Actions - mostly placeholders for now
    def on_pick_now(self) -> None:
        lay = self.video_container.layout()
        if not self._ortho_layer or lay.count() == 0:
            self._toast("Need video and orthophoto loaded", error=True)
            return
        vw = lay.itemAt(0).widget()
        if not vw:
            self._toast("No video widget", error=True)
            return
        pm = vw.grab()
        img = pm.toImage()
        dlg = SinglePickDialog(img, self)
        if dlg.exec() != QtWidgets.QDialog.Accepted or dlg.picked_uv() is None:
            return
        uu, vv = dlg.picked_uv()
        xs, ys = float(uu), float(vv)
        try:
            from pyproj import Transformer
            X, Y = self._ortho_layer.scene_to_geo(xs, ys)
            epsg = self._ortho_layer.ds.crs.to_epsg()
            tr = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
            lon, lat = tr.transform(X, Y)
            self._last_pick = (lon, lat)
            self._show_pick_on_map(xs, ys, text=f"{lat:.5f},{lon:.5f}")
            QtWidgets.QApplication.clipboard().setText(f"{lat:.6f},{lon:.6f}")
            self._toast("Copied to clipboard")
        except Exception as e:
            self._toast(f"Mapping failed: {e}", error=True)

    def _show_pick_on_map(self, xs: float, ys: float, text: str | None = None) -> None:
        sc = self.map.scene()
        if sc is None:
            sc = QtWidgets.QGraphicsScene()
            self.map.setScene(sc)
        if self._last_pick_item is None:
            it = QtWidgets.QGraphicsEllipseItem(-5, -5, 10, 10)
            it.setBrush(QtGui.QBrush(QtGui.QColor(255, 220, 0)))
            it.setPen(QtGui.QPen(QtGui.QColor(30, 30, 30), 1))
            it.setZValue(50)
            sc.addItem(it)
            self._last_pick_item = it
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

    def on_toggle_azimuth(self) -> None:
        sc = self.map.scene()
        if not sc:
            self._toast("No map loaded")
            return
        if self._az_item:
            sc.removeItem(self._az_item)
            self._az_item = None
            self._toast("Azimuth OFF")
            return
        pen = QtGui.QPen(QtGui.QColor("#00AEEF"), 2)
        self._az_item = sc.addLine(0, 0, 100, 0, pen)
        self._toast("Azimuth ON")

    def on_make_qr(self) -> None:
        link = self._current_link_for_share()
        if not link:
            self._toast("No link to encode")
            return
        img = qrcode.make(link)
        buf = BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        pm = QtGui.QPixmap()
        pm.loadFromData(buf.read(), "PNG")
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("QR Code")
        lab = QtWidgets.QLabel()
        lab.setPixmap(pm)
        lab.setAlignment(QtCore.Qt.AlignCenter)
        lay = QtWidgets.QVBoxLayout(dlg)
        lay.addWidget(lab)
        dlg.resize(320, 320)
        dlg.exec()

    def on_copy_gmaps(self) -> None:
        link = self._current_link_for_share()
        if not link:
            self._toast("No link")
            return
        QtWidgets.QApplication.clipboard().setText(link)
        self._toast("Link copied")

    # ------------------------------------------------------------------
    # helpers
    def _current_link_for_share(self) -> str | None:
        if not self._last_pick:
            return None
        lon, lat = self._last_pick
        return f"https://www.google.com/maps/search/?api=1&query={lat:.6f},{lon:.6f}"

    def _toast(self, msg: str, *, error: bool = False) -> None:
        QtWidgets.QToolTip.showText(QtGui.QCursor.pos(), msg, self)
        if error:
            self._log(msg)


class UserModule(QtCore.QObject):
    """Module wrapper used by :class:`ui_main.MainWindow`."""

    title = "User"
    icon = None

    def __init__(self, vlc_instance: vlc.Instance, log_func: Callable[[str], None] = print):
        super().__init__()
        self._vlc = vlc_instance
        self._log = log_func
        self._root = UserTab(vlc_instance, log_func)

    def widget(self) -> QtWidgets.QWidget:
        return self._root


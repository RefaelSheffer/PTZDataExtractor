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
from typing import Callable

import vlc
import qrcode
from PySide6 import QtCore, QtWidgets, QtGui

from ui_common import VlcVideoWidget
from ui_map_tools import MapView, numpy_to_qimage
from raster_layer import RasterLayer
from app_state import app_state
import shared_state


class UserTab(QtWidgets.QWidget):
    """Main widget used in the *USER* tab."""

    def __init__(self, vlc_instance: vlc.Instance, log_func: Callable[[str], None] = print, parent=None):
        super().__init__(parent)
        self._vlc = vlc_instance
        self._log = log_func
        self._az_item = None
        self._last_pick = None  # type: tuple | None
        self._ortho_layer: RasterLayer | None = None
        self._dtm_path: str | None = None

        # ----- toolbar -----
        bar = QtWidgets.QToolBar()
        self.act_pick = bar.addAction("Pick now")
        self.act_az = bar.addAction("Toggle Azimuth")
        self.act_qr = bar.addAction("QR for current view")
        self.act_copy = bar.addAction("Copy GMaps link")

        # ----- layer selectors -----
        self.dtm_edit = QtWidgets.QLineEdit()
        self.ortho_edit = QtWidgets.QLineEdit()
        btn_load = QtWidgets.QPushButton("Load layers")
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
        vbox.addWidget(splitter, 1)

        # ----- signal wiring -----
        self.act_pick.triggered.connect(self.on_pick_now)
        self.act_az.triggered.connect(self.on_toggle_azimuth)
        self.act_qr.triggered.connect(self.on_make_qr)
        self.act_copy.triggered.connect(self.on_copy_gmaps)
        btn_load.clicked.connect(self.on_load_layers)

        # Auto-populate layer paths from shared state when available
        if getattr(shared_state, "dtm_path", None):
            self.dtm_edit.setText(shared_state.dtm_path)
        if getattr(shared_state, "orthophoto_path", None):
            self.ortho_edit.setText(shared_state.orthophoto_path)
            # try loading immediately when an ortho path is known
            self.on_load_layers()

        # Start the live video preview
        self.mount_video_preview()

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
        media = self._vlc.media_new(cam.rtsp_url)
        player = vw.player()
        player.set_media(media)
        player.play()
        lay.addWidget(vw)

    # ------------------------------------------------------------------
    # Layers
    def on_load_layers(self) -> None:
        dtm = self.dtm_edit.text().strip()
        ortho = self.ortho_edit.text().strip()
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
            if dtm:
                self._dtm_path = dtm
            self._toast("Layers loaded")
        except Exception as e:  # pragma: no cover - UI feedback
            self._toast(f"Layer load failed: {e}", error=True)

    # ------------------------------------------------------------------
    # Actions - mostly placeholders for now
    def on_pick_now(self) -> None:
        self._toast("Pick not implemented", error=True)

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
        lon, lat, _ = self._last_pick
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


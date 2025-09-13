# ui_main.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Generic shell (UI) that hosts pluggable modules (tabs/panels).

import sys
from pathlib import Path
from PySide6 import QtCore, QtGui, QtWidgets
import vlc
import types

from ui_cam_module import CameraModule
from ui_prep_module import PrepModule
from ui_img2ground_module import Img2GroundModule
from ui_user_module import UserModule
from project_io import export_project, load_project
import shared_state
from app_state import app_state

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Monitoring Suite — Modular UI")
        self.resize(1380, 920)

        # Single VLC instance shared across modules
        if sys.platform == "win32":
            # חשוב: להימנע מ-opengl שגורם לשחור בחלון משובץ (Embed) ב-Windows
            # משתמשים ב-direct3d11 שהוא יציב להטמעה ב-QWidget
            self.vlc_instance = vlc.Instance('--no-plugins-cache', '--vout=direct3d11')
        else:
            self.vlc_instance = vlc.Instance('--no-plugins-cache')

        # Central tabs area
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setMovable(True)
        self.setCentralWidget(self.tabs)

        # Base settings container with its own tabs
        self.settings_tabs = QtWidgets.QTabWidget()
        self.settings_tabs.setDocumentMode(True)
        self.settings_tabs.setMovable(True)
        self.settings_dock = QtWidgets.QDockWidget("Settings", self)
        self.settings_dock.setWidget(self.settings_tabs)
        self.addDockWidget(QtCore.Qt.LeftDockWidgetArea, self.settings_dock)

        # Global log dock (persist + min height)
        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(20000)
        self._dock_logs = QtWidgets.QDockWidget("Logs", self)
        self._dock_logs.setWidget(self.log_view)
        self._dock_logs.setMinimumHeight(180)
        self.addDockWidget(QtCore.Qt.BottomDockWidgetArea, self._dock_logs)
        self._dock_logs.show()

        # Simple menus
        view_menu = self.menuBar().addMenu("View")
        view_menu.addAction(self.settings_dock.toggleViewAction())
        view_menu.addAction(self._dock_logs.toggleViewAction())
        proj_menu = self.menuBar().addMenu("Project")
        act_save = proj_menu.addAction("Save Project…")
        act_open = proj_menu.addAction("Open Project…")
        act_save.triggered.connect(self.save_project)
        act_open.triggered.connect(self.open_project)

        # Status bar
        self.status = self.statusBar()
        self.status.showMessage("Ready")

        # Top status toolbar with small chips
        self.status_toolbar = QtWidgets.QToolBar()
        self.status_toolbar.setMovable(False)
        self.addToolBar(QtCore.Qt.TopToolBarArea, self.status_toolbar)

        def _chip(text: str) -> QtWidgets.QLabel:
            lbl = QtWidgets.QLabel(text)
            lbl.setStyleSheet(
                "QLabel{border:1px solid #aaa;border-radius:4px;padding:2px 6px;}"
            )
            return lbl

        self.lbl_rtsp = _chip("RTSP: Stopped")
        self.lbl_ptz = _chip("PTZ: —")
        # Combined layer readiness (both ortho and DTM must be loaded)
        self.lbl_layers = _chip("Layers: Ortho/DTM —")
        self.lbl_telemetry = _chip("Telemetry: pan —")
        for w in (self.lbl_rtsp, self.lbl_ptz, self.lbl_layers, self.lbl_telemetry):
            self.status_toolbar.addWidget(w)

        # Route Qt warnings/errors to the log dock
        def _qt_log_handler(mode, ctx, msg):
            self.log(f"Qt: {msg}")
        QtCore.qInstallMessageHandler(_qt_log_handler)

        # Register modules
        self._modules = []

        def _scroll(w: QtWidgets.QWidget) -> QtWidgets.QScrollArea:
            sc = QtWidgets.QScrollArea()
            sc.setWidget(w)
            sc.setWidgetResizable(True)
            sc.setFrameShape(QtWidgets.QFrame.NoFrame)
            return sc

        cam = CameraModule(self.vlc_instance, log_func=self.log)
        self.cam_module = cam
        self._modules.append(cam)
        self.settings_tabs.addTab(_scroll(cam.widget()), cam.title)

        prep = PrepModule(self.vlc_instance, log_func=self.log)
        self.prep_module = prep
        self._modules.append(prep)
        self.settings_tabs.addTab(_scroll(prep.widget()), prep.title)

        i2g = Img2GroundModule(self.vlc_instance, log_func=self.log)
        self._modules.append(i2g)
        self.settings_tabs.addTab(_scroll(i2g.widget()), i2g.title)

        user = UserModule(self.vlc_instance, log_func=self.log)
        self._modules.append(user)
        # Avoid wrapping the operational tab in a QScrollArea. The embedded
        # VLC video surface does not cooperate well with scroll containers and
        # would repaint repeatedly when it became the only visible widget,
        # leading to a distracting flicker. Inserting the widget directly keeps
        # the view stable when other docks are hidden.
        self.tabs.insertTab(0, user.widget(), user.title)

        # Connect shared status signals
        shared_state.signal_rtsp_state_changed.connect(self._on_rtsp_state)
        shared_state.signal_ptz_mode_changed.connect(self._on_ptz_mode)
        shared_state.signal_layers_changed.connect(self._on_layers)
        shared_state.signal_ptz_meta_changed.connect(self._on_ptz_meta)
        shared_state.signal_camera_changed.connect(lambda *_: (self._ensure_layers_for_current_camera(),
                                                               self._notify_i2g_ready()))
        shared_state.signal_layers_changed.connect(lambda *_: self._notify_i2g_ready())

    def save_project(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save Project", "", "RTG Project (*.rtgproj)")
        if not path:
            return
        profile = self.cam_module.get_profile()
        bundle_name = self.prep_module.current_bundle_name()
        dtm_path = shared_state.dtm_path or self.prep_module.get_dtm_path()
        ortho_path = shared_state.orthophoto_path or ""
        cam_pos = getattr(shared_state, "camera_proj", None)
        try:
            export_project(Path(path), profile, bundle_name, dtm_path, ortho_path,
                           camera_position=cam_pos)
            self.log(f"Project saved -> {path}")
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Save Project", f"Failed: {e}")

    def open_project(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Open Project", "", "RTG Project (*.rtgproj)")
        if not path:
            return
        try:
            data = load_project(Path(path))
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Open Project", f"Failed: {e}")
            return
        # Keep project data globally for per-camera metadata
        proj = types.SimpleNamespace(**data)
        alias = data.get("camera", {}).get("name", "default")
        offsets = {
            k: data.get("camera", {}).get(k)
            for k in ("yaw_offset_deg", "pitch_offset_deg", "roll_offset_deg")
            if data.get("camera", {}).get(k) is not None
        }
        if offsets:
            proj.offset_for_camera = {alias: offsets}
        app_state.project = proj

        cam_pos = data.get("camera_position")
        if cam_pos:
            shared_state.camera_proj = cam_pos

        self.cam_module.apply_profile(data.get("camera", {}))
        bundle = data.get("bundle")
        if bundle:
            self.prep_module.apply_bundle(bundle)
        layers = data.get("layers", {})
        self.prep_module.apply_project_layers(layers.get("dtm"), layers.get("ortho"))
        if cam_pos:
            try:
                layer = getattr(self.prep_module, "_ortho_layer", None)
                if layer is not None:
                    epsg_here = layer.ds.crs.to_epsg()
                    X, Y = float(cam_pos.get("x", 0.0)), float(cam_pos.get("y", 0.0))
                    epsg_cam = cam_pos.get("epsg")
                    if epsg_here and epsg_cam and epsg_cam != epsg_here:
                        from pyproj import Transformer
                        tr = Transformer.from_crs(f"EPSG:{epsg_cam}", f"EPSG:{epsg_here}", always_xy=True)
                        X, Y = tr.transform(X, Y)
                    xs, ys = layer.geo_to_scene(X, Y)
                    self.prep_module.map.set_marker(xs, ys)
            except Exception:
                pass
        self.log(f"Project loaded -> {path}")
        # attempt automatic camera connection using Try Dahua
        QtCore.QTimer.singleShot(0, self.cam_module._auto_try_dahua)

    # ------- global logging -------
    @QtCore.Slot(str)
    def log(self, line: str):
        self.log_view.appendPlainText(line)

    # ------- status updates -------
    @QtCore.Slot(str)
    def _on_rtsp_state(self, state: str):
        self.lbl_rtsp.setText(f"RTSP: {state}")

    @QtCore.Slot(str)
    def _on_ptz_mode(self, mode: str):
        self.lbl_ptz.setText(f"PTZ: {mode or '—'}")

    @QtCore.Slot(str, dict)
    def _on_layers(self, _alias: str, layers: dict):
        """Update layer readiness chip.

        The calibration workflow requires both an orthophoto and a DTM.
        Display a single check mark when both are available to keep the
        indicator compact.
        """
        ready = "✓" if layers.get("ortho") and layers.get("dtm") else "—"
        self.lbl_layers.setText(f"Layers: Ortho/DTM {ready}")

    @QtCore.Slot(object)
    def _on_ptz_meta(self, meta: object):
        pan_ok = "—"
        if isinstance(meta, dict):
            pan_ok = "✓" if meta.get("pan_deg") is not None else "—"
        self.lbl_telemetry.setText(f"Telemetry: pan {pan_ok}")

    def _index_of_settings_tab(self, title: str) -> int:
        for i in range(self.settings_tabs.count()):
            if self.settings_tabs.tabText(i) == title:
                return i
        return -1

    def _ensure_layers_for_current_camera(self) -> None:
        cur = getattr(app_state, "current_camera", None)
        alias = getattr(cur, "alias", None)
        if not alias:
            return
        layers = shared_state.layers_for_camera.get(alias)
        if layers:
            return
        fallback = shared_state.layers_for_camera.get("(default)")
        if fallback:
            shared_state.layers_for_camera[alias] = fallback.copy()
            try:
                cur.layers = shared_state.layers_for_camera[alias]
            except Exception:
                pass
            shared_state.signal_layers_changed.emit(alias, shared_state.layers_for_camera[alias])

    def _notify_i2g_ready(self) -> None:
        cur = getattr(app_state, "current_camera", None)
        alias = getattr(cur, "alias", None) or "(default)"
        layers = shared_state.layers_for_camera.get(alias) or {}
        ready_layers = bool(layers.get("dtm")) and bool(layers.get("ortho"))
        if cur and ready_layers:
            print("[Main] Ready for Image → Ground (no auto-switch)")

def main():
    app = QtWidgets.QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()

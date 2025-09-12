#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Preparation (DTM GeoTIFF + Ortho + Camera pose)

from pathlib import Path
import math
from typing import Optional
from PySide6 import QtCore, QtWidgets, QtGui
import vlc

from camera_models import save_bundle, list_bundles
from geom3d import CameraIntrinsics, CameraPose, GeoRef, geographic_to_local
from app_state import app_state
import shared_state
from event_bus import bus


class PrepModule(QtCore.QObject):
    title = "Preparation (3D Model  Camera)"
    icon = None

    def __init__(self, vlc_instance: vlc.Instance, log_func=print):
        super().__init__()
        self._log = log_func
        self._vlc = vlc_instance
        self._map_layer = None       # RasterLayer (DTM)
        self._ortho_layer = None     # RasterLayer (Ortho)
        self._dtm_pixmap = None
        self._ortho_pixmap = None
        self._root = self._build_ui()

        shared_state.signal_camera_changed.connect(
            lambda ctx: self._apply_layers_for(getattr(ctx, "alias", None))
        )
        shared_state.signal_layers_changed.connect(self._on_layers_changed)
        bus.signal_ortho_changed.connect(self.apply_ortho)

    def widget(self) -> QtWidgets.QWidget:
        return self._root

    # ---------- UI ----------
    def _build_ui(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        lay = QtWidgets.QGridLayout(w)
        r = 0

        # DTM
        lay.addWidget(QtWidgets.QLabel("<b>DTM (GeoTIFF)</b>"), r, 0, 1, 4); r += 1
        self.ed_dtm = QtWidgets.QLineEdit()
        btn_browse = QtWidgets.QPushButton("Browse DTM…")
        btn_browse.clicked.connect(self._browse_dtm)
        self.cmb_axis = QtWidgets.QComboBox(); self.cmb_axis.addItems(["Z-up (default)", "Y-up (Blender style)"])
        self.spn_scale = QtWidgets.QDoubleSpinBox(); self.spn_scale.setRange(0.0001, 10000); self.spn_scale.setDecimals(4); self.spn_scale.setValue(1.0)
        lay.addWidget(QtWidgets.QLabel("Path:"), r, 0)
        lay.addWidget(self.ed_dtm, r, 1, 1, 2)
        lay.addWidget(btn_browse, r, 3); r += 1

        self.ed_ortho = QtWidgets.QLineEdit()
        btn_browse_ortho = QtWidgets.QPushButton("Browse Ortho…")
        btn_browse_ortho.clicked.connect(self._browse_ortho)
        lay.addWidget(QtWidgets.QLabel("Ortho:"), r, 0)
        lay.addWidget(self.ed_ortho, r, 1, 1, 2)
        lay.addWidget(btn_browse_ortho, r, 3); r += 1

        self.btn_read_epsg = QtWidgets.QPushButton("Read EPSG from DTM")
        self.btn_center_origin = QtWidgets.QPushButton("Origin = DEM center")
        self.btn_read_epsg.clicked.connect(self._read_epsg_from_dtm)
        self.btn_center_origin.clicked.connect(self._origin_from_dem_center)
        lay.addWidget(self.btn_read_epsg, r, 2)
        lay.addWidget(self.btn_center_origin, r, 3); r += 1

        # Calibration and lighting files
        lay.addWidget(QtWidgets.QLabel("<b>Calibration</b>"), r, 0, 1, 4); r += 1
        self.ed_calib_images = QtWidgets.QLineEdit()
        btn_calib_imgs = QtWidgets.QPushButton("Browse images…"); btn_calib_imgs.clicked.connect(self._browse_calib_images)
        lay.addWidget(QtWidgets.QLabel("Images folder:"), r, 0)
        lay.addWidget(self.ed_calib_images, r, 1, 1, 2)
        lay.addWidget(btn_calib_imgs, r, 3); r += 1

        self.ed_calib_params = QtWidgets.QLineEdit()
        btn_calib_params = QtWidgets.QPushButton("Browse params…"); btn_calib_params.clicked.connect(self._browse_calib_params)
        lay.addWidget(QtWidgets.QLabel("Params file:"), r, 0)
        lay.addWidget(self.ed_calib_params, r, 1, 1, 2)
        lay.addWidget(btn_calib_params, r, 3); r += 1

        self.ed_lights = QtWidgets.QLineEdit()
        btn_lights = QtWidgets.QPushButton("Browse lights…"); btn_lights.clicked.connect(self._browse_lights)
        lay.addWidget(QtWidgets.QLabel("Lights file:"), r, 0)
        lay.addWidget(self.ed_lights, r, 1, 1, 2)
        lay.addWidget(btn_lights, r, 3); r += 1

        # Intrinsics
        lay.addWidget(QtWidgets.QLabel("<b>Camera intrinsics</b>"), r, 0, 1, 4); r += 1
        self.spn_w = QtWidgets.QSpinBox(); self.spn_w.setRange(16, 7680); self.spn_w.setValue(1280)
        self.spn_h = QtWidgets.QSpinBox(); self.spn_h.setRange(16, 4320); self.spn_h.setValue(720)
        self.spn_fov = QtWidgets.QDoubleSpinBox(); self.spn_fov.setRange(10, 160); self.spn_fov.setValue(90.0)
        lay.addWidget(QtWidgets.QLabel("Width:"), r, 0); lay.addWidget(self.spn_w, r, 1)
        lay.addWidget(QtWidgets.QLabel("Height:"), r, 2); lay.addWidget(self.spn_h, r, 3); r += 1
        lay.addWidget(QtWidgets.QLabel("Horizontal FOV (deg):"), r, 0); lay.addWidget(self.spn_fov, r, 1); r += 1

        # Extrinsics
        lay.addWidget(QtWidgets.QLabel("<b>Camera extrinsics (world pose)</b>"), r, 0, 1, 4); r += 1
        self.spn_x = QtWidgets.QDoubleSpinBox(); self.spn_x.setRange(-10000, 10000); self.spn_x.setDecimals(3)
        self.spn_y = QtWidgets.QDoubleSpinBox(); self.spn_y.setRange(-10000, 10000); self.spn_y.setDecimals(3)
        self.spn_z = QtWidgets.QDoubleSpinBox(); self.spn_z.setRange(-10000, 10000); self.spn_z.setDecimals(3); self.spn_z.setValue(2.5)
        self.spn_yaw = QtWidgets.QDoubleSpinBox(); self.spn_yaw.setRange(-360, 360); self.spn_yaw.setDecimals(3)
        self.spn_pitch = QtWidgets.QDoubleSpinBox(); self.spn_pitch.setRange(-360, 360); self.spn_pitch.setDecimals(3)
        self.spn_roll = QtWidgets.QDoubleSpinBox(); self.spn_roll.setRange(-360, 360); self.spn_roll.setDecimals(3)
        lay.addWidget(QtWidgets.QLabel("X:"), r, 0); lay.addWidget(self.spn_x, r, 1)
        lay.addWidget(QtWidgets.QLabel("Y:"), r, 2); lay.addWidget(self.spn_y, r, 3); r += 1
        lay.addWidget(QtWidgets.QLabel("Z (height):"), r, 0); lay.addWidget(self.spn_z, r, 1)
        lay.addWidget(QtWidgets.QLabel("Yaw:"), r, 2); lay.addWidget(self.spn_yaw, r, 3); r += 1
        lay.addWidget(QtWidgets.QLabel("Pitch:"), r, 0); lay.addWidget(self.spn_pitch, r, 1)
        lay.addWidget(QtWidgets.QLabel("Roll:"), r, 2); lay.addWidget(self.spn_roll, r, 3); r += 1

        # מפה (DTM/Ortho)
        lay.addWidget(QtWidgets.QLabel("<b>Map (DTM / Orthophoto)</b>"), r, 0, 1, 4); r += 1
        from ui_map_tools import MapView
        self.map = MapView(); self.map.setMinimumHeight(360)
        lay.addWidget(self.map, r, 0, 1, 4); r += 1

        row = QtWidgets.QHBoxLayout()
        self.chk_auto_z = QtWidgets.QCheckBox("Auto Z from DTM"); self.chk_auto_z.setChecked(True)
        self.chk_show_dtm = QtWidgets.QCheckBox("Show DTM"); self.chk_show_dtm.setChecked(True); self.chk_show_dtm.toggled.connect(self._update_layer_visibility)
        self.chk_show_ortho = QtWidgets.QCheckBox("Show Ortho"); self.chk_show_ortho.setChecked(True); self.chk_show_ortho.toggled.connect(self._update_layer_visibility)
        row.addStretch(1); row.addWidget(self.chk_auto_z); row.addWidget(self.chk_show_dtm); row.addWidget(self.chk_show_ortho)
        lay.addLayout(row, r, 0, 1, 4); r += 1

        self.map.clicked.connect(self._map_clicked)

        # Georef
        lay.addWidget(QtWidgets.QLabel("<b>Georeferencing</b>"), r, 0, 1, 4); r += 1
        self.ed_lat = QtWidgets.QDoubleSpinBox(); self.ed_lat.setRange(-90, 90); self.ed_lat.setDecimals(7)
        self.ed_lon = QtWidgets.QDoubleSpinBox(); self.ed_lon.setRange(-180, 180); self.ed_lon.setDecimals(7)
        self.ed_alt = QtWidgets.QDoubleSpinBox(); self.ed_alt.setRange(-10000, 10000); self.ed_alt.setDecimals(3)
        self.ed_yawsite = QtWidgets.QDoubleSpinBox(); self.ed_yawsite.setRange(-360, 360); self.ed_yawsite.setDecimals(3)
        self.ed_epsg = QtWidgets.QLineEdit("2039")
        lay.addWidget(QtWidgets.QLabel("Origin lat:"), r, 0); lay.addWidget(self.ed_lat, r, 1)
        lay.addWidget(QtWidgets.QLabel("Origin lon:"), r, 2); lay.addWidget(self.ed_lon, r, 3); r += 1
        lay.addWidget(QtWidgets.QLabel("Origin alt (m):"), r, 0); lay.addWidget(self.ed_alt, r, 1)
        lay.addWidget(QtWidgets.QLabel("Site yaw (° vs East):"), r, 2); lay.addWidget(self.ed_yawsite, r, 3); r += 1
        lay.addWidget(QtWidgets.QLabel("Projected EPSG (optional):"), r, 0); lay.addWidget(self.ed_epsg, r, 1); r += 1

        # Bundle
        lay.addWidget(QtWidgets.QLabel("<b>Bundle</b>"), r, 0, 1, 4); r += 1
        self.ed_name = QtWidgets.QLineEdit("site1_cam1")
        btn_save = QtWidgets.QPushButton("Save bundle"); btn_save.clicked.connect(self._save_bundle)
        self.cmb_existing = QtWidgets.QComboBox(); self._refresh_bundles()
        lay.addWidget(QtWidgets.QLabel("Name:"), r, 0); lay.addWidget(self.ed_name, r, 1)
        lay.addWidget(btn_save, r, 2); lay.addWidget(self.cmb_existing, r, 3); r += 1

        lay.setRowStretch(r, 1)
        return w

    # ---------- עזר ----------
    def _ensure_scene(self) -> QtWidgets.QGraphicsScene:
        sc = self.map.scene()
        if sc is None:
            sc = QtWidgets.QGraphicsScene()
            self.map.setScene(sc)
        return sc

    def _refresh_bundles(self):
        self.cmb_existing.clear()
        self.cmb_existing.addItems(["(existing bundles)"] + list_bundles())

    # ---------- DTM / Ortho ----------
    def _browse_dtm(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(None, "Open DTM (GeoTIFF)", "", "GeoTIFF (*.tif *.tiff);;All files (*.*)")
        if not path:
            return
        self.ed_dtm.setText(path)
        shared_state.dtm_path = path
        self._ensure_base_map()
        self._publish_layers(dtm=path)

    def _browse_ortho(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(None, "Open Ortho (GeoTIFF)", "", "GeoTIFF (*.tif *.tiff);;All files (*.*)")
        if not path:
            return
        self.ed_ortho.setText(path)
        self._load_orthophoto_path(path)

    def _read_epsg_from_dtm(self):
        try:
            from dtm import DTM
            p = self.ed_dtm.text().strip()
            if not p:
                QtWidgets.QMessageBox.information(None, "DTM", "Choose a DTM (GeoTIFF) first."); return
            d = DTM(p)
            epsg = d.info.crs_epsg
            if epsg:
                self.ed_epsg.setText(str(epsg))
                self._log(f"DTM EPSG detected: {epsg}")
            else:
                self._log("DTM EPSG not found in file.")
            d.close()
            self._publish_layers(srs=str(epsg) if epsg else None)
        except Exception as e:
            QtWidgets.QMessageBox.warning(None, "DTM", f"Failed to read EPSG: {e}")
            self._publish_layers()

    def _origin_from_dem_center(self):
        try:
            from dtm import DTM
            from pyproj import Transformer
            p = self.ed_dtm.text().strip()
            if not p:
                QtWidgets.QMessageBox.information(None, "DTM", "Choose a DTM (GeoTIFF) first."); return
            d = DTM(p)
            l,b,r,t = d.info.bounds
            cx = 0.5*(l+r); cy = 0.5*(b+t)
            epsg = d.info.crs_epsg
            if epsg is None:
                QtWidgets.QMessageBox.information(None, "DTM", "GeoTIFF has no CRS; cannot compute lat/lon."); d.close(); return
            tr = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
            lon, lat = tr.transform(cx, cy)
            self.ed_lon.setValue(lon); self.ed_lat.setValue(lat)
            self._log(f"Origin set from DEM center: lat={lat:.7f}, lon={lon:.7f}")
            d.close()
        except Exception as e:
            QtWidgets.QMessageBox.warning(None, "DTM", f"Failed to set origin: {e}")

    def _browse_calib_images(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(None, "Select calibration images", self.ed_calib_images.text() or "")
        if path:
            self.ed_calib_images.setText(path)

    def _browse_calib_params(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(None, "Select calibration params", "", "All files (*.*)")
        if path:
            self.ed_calib_params.setText(path)

    def _browse_lights(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(None, "Select lights file", "", "All files (*.*)")
        if path:
            self.ed_lights.setText(path)

    def _ensure_base_map(self):
        try:
            from raster_layer import RasterLayer
            from ui_map_tools import numpy_to_qimage
        except Exception as e:
            self._log(f"Map tools missing: {e}"); return
        p = self.ed_dtm.text().strip()
        if not p:
            return

        sc = self._ensure_scene()
        if self._map_layer is None:
            try:
                self._map_layer = RasterLayer(p, max_size=2048)
                img = numpy_to_qimage(self._map_layer.downsampled_image())
                self._dtm_pixmap = QtWidgets.QGraphicsPixmapItem(QtGui.QPixmap.fromImage(img))
                self._dtm_pixmap.setZValue(0)
                sc.addItem(self._dtm_pixmap)
                self.map.setSceneRect(self._dtm_pixmap.boundingRect())
                self.map.fit()
            except Exception as e:
                self._log(f"Failed to load base DTM map: {e}")
        self._update_layer_visibility()

    def _update_layer_visibility(self):
        if self._dtm_pixmap is not None:
            self._dtm_pixmap.setVisible(self.chk_show_dtm.isChecked())
        if self._ortho_pixmap is not None:
            self._ortho_pixmap.setVisible(self.chk_show_ortho.isChecked())

    # קליק על המפה → קיבוע XY למצלמה (ושרטוט CAM)
    def _map_clicked(self, xs: float, ys: float):
        self._ensure_base_map()
        layer = self._ortho_layer or self._map_layer
        if layer is None:
            QtWidgets.QMessageBox.information(None, "Map", "Load DTM (Path) first.")
            return
        try:
            X, Y = layer.scene_to_geo(xs, ys)
            epsg_layer = layer.ds.crs.to_epsg()
            # למעבר לערכים מקומיים (לשדות X/Y בטופס)
            from pyproj import Transformer
            tr_to_ll = Transformer.from_crs(f"EPSG:{epsg_layer}", "EPSG:4326", always_xy=True) if epsg_layer else None
            if tr_to_ll is None:
                QtWidgets.QMessageBox.information(None, "CRS", "Layer CRS unknown; cannot map to lat/lon.")
                return
            lon, lat = tr_to_ll.transform(X, Y)

            gr = GeoRef(self.ed_lat.value(), self.ed_lon.value(), self.ed_alt.value(),
                        self.ed_yawsite.value(),
                        int(self.ed_epsg.text().strip()) if self.ed_epsg.text().strip() else None)
            p_local = geographic_to_local(gr, lat, lon, self.ed_alt.value())
            self.spn_x.setValue(float(p_local[0])); self.spn_y.setValue(float(p_local[1]))

            # Auto-Z מה-DTM (אם נטען)
            if self.chk_auto_z.isChecked() and self._map_layer is not None:
                epsg_dtm = self._map_layer.ds.crs.to_epsg()
                if epsg_layer != epsg_dtm:
                    tr_to_dtm = Transformer.from_crs(f"EPSG:{epsg_layer}", f"EPSG:{epsg_dtm}", always_xy=True)
                    Xd, Yd = tr_to_dtm.transform(X, Y)
                else:
                    Xd, Yd = X, Y
                from dtm import DTM
                d = DTM(self._map_layer.path)
                z = d.sample(Xd, Yd); d.close()
                if z is not None:
                    self.spn_z.setValue(z)

            # שרטוט CAM ושמירה ל-shared_state
            self.map.set_marker(xs, ys)
            shared_state.camera_proj = {"x": float(X), "y": float(Y), "epsg": int(epsg_layer) if epsg_layer else None}
            self._log(f"Camera XY set (proj EPSG {epsg_layer}): ({X:.3f},{Y:.3f})")
        except Exception as e:
            QtWidgets.QMessageBox.warning(None, "Map", f"Failed to set camera XY: {e}")

    # ---------- שמירת bundle ----------
    def _save_bundle(self):
        model_path = self.ed_dtm.text().strip()
        if not model_path:
            QtWidgets.QMessageBox.warning(None, "DTM", "Choose a DTM (GeoTIFF) file.")
            return
        scale = self.spn_scale.value()
        intr = CameraIntrinsics.from_fov(self.spn_w.value(), self.spn_h.value(), self.spn_fov.value())
        pose = CameraPose(self.spn_x.value(), self.spn_y.value(), self.spn_z.value(),
                          self.spn_yaw.value(), self.spn_pitch.value(), self.spn_roll.value())
        meta = {"up_axis": self.cmb_axis.currentText(), "scale_to_m": scale}
        epsg_txt = self.ed_epsg.text().strip()
        georef = {
            "origin_lat": self.ed_lat.value(),
            "origin_lon": self.ed_lon.value(),
            "origin_alt": self.ed_alt.value(),
            "yaw_site_deg": self.ed_yawsite.value(),
            "projected_epsg": int(epsg_txt) if epsg_txt else None,
        }
        out = save_bundle(self.ed_name.text().strip(), intr, pose, model_path, meta, georef)
        self._log(f"Saved bundle -> {out}")
        QtWidgets.QMessageBox.information(None, "Saved", f"Saved bundle:\n{out}")
        self._refresh_bundles()

    # ---------- Project IO helpers ----------
    def current_bundle_name(self) -> str:
        return self.ed_name.text().strip()

    def get_dtm_path(self) -> str:
        return self.ed_dtm.text().strip()

    def apply_bundle(self, bundle: dict):
        """Populate UI fields from bundle dict."""
        if not bundle:
            return
        self.ed_name.setText(bundle.get("name", ""))
        intr = bundle.get("intrinsics", {})
        w = int(intr.get("width", 0))
        h = int(intr.get("height", 0))
        fx = float(intr.get("fx", 0.0))
        if w:
            self.spn_w.setValue(w)
        if h:
            self.spn_h.setValue(h)
        if w and fx:
            hfov = math.degrees(2 * math.atan(w / (2 * fx)))
            self.spn_fov.setValue(hfov)
        pose = bundle.get("pose", {})
        self.spn_x.setValue(float(pose.get("x", 0.0)))
        self.spn_y.setValue(float(pose.get("y", 0.0)))
        self.spn_z.setValue(float(pose.get("z", 0.0)))
        self.spn_yaw.setValue(float(pose.get("yaw", 0.0)))
        self.spn_pitch.setValue(float(pose.get("pitch", 0.0)))
        self.spn_roll.setValue(float(pose.get("roll", 0.0)))

    def apply_project_layers(self, dtm_path: str | None, ortho_path: str | None):
        """Load DTM/Ortho layers from project paths."""
        if dtm_path:
            self.ed_dtm.setText(dtm_path)
            shared_state.dtm_path = dtm_path
            self._map_layer = None
            try:
                self._ensure_base_map()
            except Exception as e:
                QtWidgets.QMessageBox.warning(None, "DTM", f"Failed to load DTM: {e}")
            self._publish_layers(dtm=dtm_path)
        if ortho_path:
            self.ed_ortho.setText(ortho_path)
            self._load_orthophoto_path(ortho_path)

    def _load_orthophoto_path(self, path: str, *, broadcast: bool = True):
        try:
            from raster_layer import RasterLayer
            from ui_map_tools import numpy_to_qimage
        except Exception as e:
            QtWidgets.QMessageBox.warning(None, "Orthophoto", f"Missing map tools: {e}")
            return
        try:
            layer = RasterLayer(path, max_size=2048)
            self.apply_ortho(layer)
            if broadcast:
                self._publish_layers(ortho=path)
                bus.signal_ortho_changed.emit(layer)
        except Exception as e:
            QtWidgets.QMessageBox.warning(None, "Orthophoto", f"Failed to load: {e}")

    def apply_ortho(self, layer) -> None:
        try:
            from ui_map_tools import numpy_to_qimage
        except Exception as e:
            QtWidgets.QMessageBox.warning(None, "Orthophoto", f"Missing map tools: {e}")
            return
        try:
            self._ortho_layer = layer
            img = numpy_to_qimage(layer.downsampled_image())
            pix = QtGui.QPixmap.fromImage(img)
            sc = self._ensure_scene()
            if self._ortho_pixmap is None:
                self._ortho_pixmap = QtWidgets.QGraphicsPixmapItem(pix)
                self._ortho_pixmap.setZValue(1)
                sc.addItem(self._ortho_pixmap)
            else:
                self._ortho_pixmap.setPixmap(pix)
            shared_state.orthophoto_path = layer.path
            self._update_layer_visibility()
            self._log(f"Orthophoto loaded (EPSG={layer.ds.crs.to_epsg()})")
        except Exception as e:
            QtWidgets.QMessageBox.warning(None, "Orthophoto", f"Failed to load: {e}")

    def _publish_layers(self, ortho: Optional[str] = None, dtm: Optional[str] = None, srs: Optional[str] = None) -> None:
        alias = getattr(app_state.current_camera, "alias", "default")
        layers = shared_state.layers_for_camera.get(alias, {}).copy()
        if ortho is None:
            ortho = shared_state.orthophoto_path or (self.ed_ortho.text().strip() or None)
        if dtm is None:
            dtm = self.ed_dtm.text().strip() or None
        if srs is None:
            try:
                if self._map_layer and self._map_layer.ds.crs:
                    epsg = self._map_layer.ds.crs.to_epsg()
                    if epsg:
                        srs = f"EPSG:{epsg}"
            except Exception:
                pass
        if ortho is not None:
            layers["ortho"] = ortho
        if dtm is not None:
            layers["dtm"] = dtm
        if srs is not None:
            layers["srs"] = srs
        shared_state.layers_for_camera[alias] = layers
        try:
            if app_state.current_camera:
                app_state.current_camera.layers = layers
            proj = getattr(app_state, "project", None)
            if proj is not None:
                d = getattr(proj, "layers_for_camera", {}) or {}
                d[alias] = layers
                proj.layers_for_camera = d
        except Exception:
            pass
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
        if dtm:
            self.ed_dtm.setText(dtm)
            shared_state.dtm_path = dtm
            self._map_layer = None
            self._ensure_base_map()
        if ortho:
            self.ed_ortho.setText(ortho)
            self._load_orthophoto_path(ortho, broadcast=False)

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

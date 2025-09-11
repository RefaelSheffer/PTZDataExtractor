# ui_settings_module.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Central settings tab for managing camera profiles including
connection parameters, calibration, lighting and DTM paths."""

import json
from pathlib import Path
from typing import List, Callable, Optional

from PySide6 import QtCore, QtWidgets, QtGui

APP_DIR = Path(__file__).resolve().parent
PROFILES_PATH = APP_DIR / "profiles.json"


class SettingsModule(QtCore.QObject):
    """Plug-in style module to edit camera profiles"""
    title = "Profiles"
    icon: Optional[QtGui.QIcon] = None

    def __init__(self, log_func: Callable[[str], None] = print):
        super().__init__()
        self._log = log_func
        self._profiles: List[dict] = self._load_profiles()
        self._root = self._build_ui()

    # ---- public API ----
    def widget(self) -> QtWidgets.QWidget:
        return self._root

    # ---- internal helpers ----
    def _build_ui(self) -> QtWidgets.QWidget:
        root = QtWidgets.QWidget()
        layout = QtWidgets.QGridLayout(root)

        # profile management
        self.profiles_combo = QtWidgets.QComboBox()
        self.profile_name = QtWidgets.QLineEdit()
        self.btn_profile_load = QtWidgets.QPushButton("Load")
        self.btn_profile_saveas = QtWidgets.QPushButton("Save as…")
        self.btn_profile_update = QtWidgets.QPushButton("Update")
        self.btn_profile_delete = QtWidgets.QPushButton("Delete")
        self._refresh_profiles_combo()

        self.btn_profile_load.clicked.connect(self.profile_load)
        self.btn_profile_saveas.clicked.connect(self.profile_saveas)
        self.btn_profile_update.clicked.connect(self.profile_update)
        self.btn_profile_delete.clicked.connect(self.profile_delete)

        row = 0
        layout.addWidget(QtWidgets.QLabel("Profile:"), row, 0)
        layout.addWidget(self.profiles_combo, row, 1)
        layout.addWidget(self.btn_profile_load, row, 2)
        layout.addWidget(self.btn_profile_saveas, row, 3)
        layout.addWidget(self.btn_profile_update, row, 4)
        layout.addWidget(self.btn_profile_delete, row, 5)
        row += 1

        layout.addWidget(QtWidgets.QLabel("Name:"), row, 0)
        layout.addWidget(self.profile_name, row, 1, 1, 5)
        row += 1

        # connection fields
        self.host = QtWidgets.QLineEdit()
        self.user = QtWidgets.QLineEdit()
        self.pwd = QtWidgets.QLineEdit(); self.pwd.setEchoMode(QtWidgets.QLineEdit.Password)
        self.rtsp_port = QtWidgets.QSpinBox(); self.rtsp_port.setRange(1, 65535); self.rtsp_port.setValue(554)
        self.rtsp_path = QtWidgets.QLineEdit("/cam/realmonitor?channel=1&subtype=1")
        self.onvif_port = QtWidgets.QSpinBox(); self.onvif_port.setRange(1, 65535); self.onvif_port.setValue(80)

        layout.addWidget(QtWidgets.QLabel("Host/IP:"), row, 0); layout.addWidget(self.host, row, 1, 1, 2)
        layout.addWidget(QtWidgets.QLabel("User:"), row, 3); layout.addWidget(self.user, row, 4, 1, 2); row += 1
        layout.addWidget(QtWidgets.QLabel("Password:"), row, 0); layout.addWidget(self.pwd, row, 1, 1, 2)
        layout.addWidget(QtWidgets.QLabel("RTSP port:"), row, 3); layout.addWidget(self.rtsp_port, row, 4); row += 1
        layout.addWidget(QtWidgets.QLabel("RTSP path:"), row, 0); layout.addWidget(self.rtsp_path, row, 1, 1, 5); row += 1
        layout.addWidget(QtWidgets.QLabel("ONVIF port:"), row, 0); layout.addWidget(self.onvif_port, row, 1); row += 1

        # calibration and ancillary files
        self.calib_images = QtWidgets.QLineEdit()
        btn_calib_images = QtWidgets.QPushButton("Browse…"); btn_calib_images.clicked.connect(self._choose_calib_images)
        self.calib_params = QtWidgets.QLineEdit()
        btn_calib_params = QtWidgets.QPushButton("Browse…"); btn_calib_params.clicked.connect(self._choose_calib_params)
        self.lights_file = QtWidgets.QLineEdit()
        btn_lights = QtWidgets.QPushButton("Browse…"); btn_lights.clicked.connect(self._choose_lights)
        self.dtm_file = QtWidgets.QLineEdit()
        btn_dtm = QtWidgets.QPushButton("Browse…"); btn_dtm.clicked.connect(self._choose_dtm)

        layout.addWidget(QtWidgets.QLabel("Calibration images:"), row, 0); layout.addWidget(self.calib_images, row, 1, 1, 4); layout.addWidget(btn_calib_images, row, 5); row += 1
        layout.addWidget(QtWidgets.QLabel("Calibration params:"), row, 0); layout.addWidget(self.calib_params, row, 1, 1, 4); layout.addWidget(btn_calib_params, row, 5); row += 1
        layout.addWidget(QtWidgets.QLabel("Lights file:"), row, 0); layout.addWidget(self.lights_file, row, 1, 1, 4); layout.addWidget(btn_lights, row, 5); row += 1
        layout.addWidget(QtWidgets.QLabel("DTM file:"), row, 0); layout.addWidget(self.dtm_file, row, 1, 1, 4); layout.addWidget(btn_dtm, row, 5); row += 1

        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(4, 1)

        return root

    # file dialogs
    def _choose_calib_images(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self._root, "Select calibration images", self.calib_images.text() or str(APP_DIR))
        if d:
            self.calib_images.setText(d)

    def _choose_calib_params(self):
        f,_ = QtWidgets.QFileDialog.getOpenFileName(self._root, "Select calibration parameters", self.calib_params.text() or str(APP_DIR))
        if f:
            self.calib_params.setText(f)

    def _choose_lights(self):
        f,_ = QtWidgets.QFileDialog.getOpenFileName(self._root, "Select lights file", self.lights_file.text() or str(APP_DIR))
        if f:
            self.lights_file.setText(f)

    def _choose_dtm(self):
        f,_ = QtWidgets.QFileDialog.getOpenFileName(self._root, "Select DTM file", self.dtm_file.text() or str(APP_DIR))
        if f:
            self.dtm_file.setText(f)

    # profile helpers
    def _load_profiles(self) -> List[dict]:
        if PROFILES_PATH.exists():
            try:
                return json.loads(PROFILES_PATH.read_text(encoding="utf-8"))
            except Exception:
                return []
        return []

    def _save_profiles(self):
        try:
            PROFILES_PATH.write_text(json.dumps(self._profiles, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            QtWidgets.QMessageBox.warning(self._root, "Profiles", f"Failed to save profiles:\n{e}")

    def _refresh_profiles_combo(self):
        cur = self.profiles_combo.currentText()
        self.profiles_combo.clear()
        self.profiles_combo.addItem("(select profile)")
        for p in self._profiles:
            self.profiles_combo.addItem(p.get("name", "(noname)"))
        if cur:
            idx = self.profiles_combo.findText(cur)
            if idx >= 0:
                self.profiles_combo.setCurrentIndex(idx)

    def _profile_from_ui(self) -> dict:
        return {
            "name": self.profile_name.text().strip() or self.profiles_combo.currentText().strip(),
            "host": self.host.text().strip(),
            "user": self.user.text().strip(),
            "pwd": self.pwd.text(),
            "rtsp_port": self.rtsp_port.value(),
            "rtsp_path": self.rtsp_path.text().strip(),
            "onvif_port": self.onvif_port.value(),
            "calibration_images": self.calib_images.text().strip(),
            "calibration_params": self.calib_params.text().strip(),
            "lights_file": self.lights_file.text().strip(),
            "dtm_file": self.dtm_file.text().strip(),
        }

    def _apply_profile(self, p: dict):
        if not p:
            return
        self.profile_name.setText(p.get("name", ""))
        self.host.setText(p.get("host", ""))
        self.user.setText(p.get("user", ""))
        self.pwd.setText(p.get("pwd", ""))
        self.rtsp_port.setValue(int(p.get("rtsp_port", 554)))
        self.rtsp_path.setText(p.get("rtsp_path", "/cam/realmonitor?channel=1&subtype=1"))
        self.onvif_port.setValue(int(p.get("onvif_port", 80)))
        self.calib_images.setText(p.get("calibration_images", ""))
        self.calib_params.setText(p.get("calibration_params", ""))
        self.lights_file.setText(p.get("lights_file", ""))
        self.dtm_file.setText(p.get("dtm_file", ""))

    # profile actions
    def profile_load(self):
        name = self.profiles_combo.currentText().strip()
        if not name or name == "(select profile)":
            QtWidgets.QMessageBox.information(self._root, "Profiles", "Select a profile to load.")
            return
        p = next((x for x in self._profiles if x.get("name") == name), None)
        if not p:
            QtWidgets.QMessageBox.warning(self._root, "Profiles", "Profile not found.")
            return
        self._apply_profile(p)
        self._log(f"Loaded profile: {name}")

    def profile_saveas(self):
        name, ok = QtWidgets.QInputDialog.getText(None, "Save profile", "Profile name:")
        name = (name or "").strip()
        if not ok or not name:
            return
        p = self._profile_from_ui(); p["name"] = name
        existing = next((x for x in self._profiles if x.get("name") == name), None)
        if existing:
            ans = QtWidgets.QMessageBox.question(self._root, "Save profile", f"Profile '{name}' exists. Overwrite?",
                                                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
            if ans != QtWidgets.QMessageBox.Yes:
                return
            self._profiles = [x for x in self._profiles if x.get("name") != name]
        self._profiles.append(p)
        self._save_profiles()
        self._refresh_profiles_combo()
        self.profiles_combo.setCurrentText(name)
        self._log(f"Saved profile: {name}")

    def profile_update(self):
        name = self.profiles_combo.currentText().strip()
        if not name or name == "(select profile)":
            QtWidgets.QMessageBox.information(self._root, "Profiles", "Select a profile to update (or use Save as…).")
            return
        p = self._profile_from_ui(); p["name"] = name
        found = False
        for i, x in enumerate(self._profiles):
            if x.get("name") == name:
                self._profiles[i] = p
                found = True
                break
        if not found:
            self._profiles.append(p)
        self._save_profiles()
        self._refresh_profiles_combo()
        self.profiles_combo.setCurrentText(name)
        self._log(f"Updated profile: {name}")

    def profile_delete(self):
        name = self.profiles_combo.currentText().strip()
        if not name or name == "(select profile)":
            QtWidgets.QMessageBox.information(self._root, "Profiles", "Select a profile to delete.")
            return
        ans = QtWidgets.QMessageBox.question(self._root, "Delete profile", f"Delete '{name}'?",
                                             QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
        if ans != QtWidgets.QMessageBox.Yes:
            return
        self._profiles = [x for x in self._profiles if x.get("name") != name]
        self._save_profiles()
        self._refresh_profiles_combo()
        self.profile_name.clear()
        self._log(f"Deleted profile: {name}")

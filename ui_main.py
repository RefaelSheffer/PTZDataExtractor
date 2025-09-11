# ui_main.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Generic shell (UI) that hosts pluggable modules (tabs/panels).

import sys
from PySide6 import QtCore, QtGui, QtWidgets
import vlc

from ui_cam_module import CameraModule
from ui_prep_module import PrepModule
from ui_img2ground_module import Img2GroundModule
from ui_monitor_module import MonitorModule

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
        self.tabs.addTab(self.settings_tabs, "Settings")

        # Global log dock (persist + min height)
        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(20000)
        self._dock_logs = QtWidgets.QDockWidget("Logs", self)
        self._dock_logs.setWidget(self.log_view)
        self._dock_logs.setMinimumHeight(180)
        self.addDockWidget(QtCore.Qt.BottomDockWidgetArea, self._dock_logs)
        self._dock_logs.show()

        # Status bar
        self.status = self.statusBar()
        self.status.showMessage("Ready")

        # Route Qt warnings/errors to the log dock
        def _qt_log_handler(mode, ctx, msg):
            self.log(f"Qt: {msg}")
        QtCore.qInstallMessageHandler(_qt_log_handler)

        # Register modules
        self._modules = []

        cam = CameraModule(self.vlc_instance, log_func=self.log)
        self._modules.append(cam)
        self.settings_tabs.addTab(cam.widget(), cam.title)

        prep = PrepModule(self.vlc_instance, log_func=self.log)
        self._modules.append(prep)
        self.settings_tabs.addTab(prep.widget(), prep.title)

        i2g = Img2GroundModule(self.vlc_instance, log_func=self.log)
        self._modules.append(i2g)
        self.settings_tabs.addTab(i2g.widget(), i2g.title)

        monitor = MonitorModule(self.vlc_instance, log_func=self.log)
        self._modules.append(monitor)
        self.tabs.addTab(monitor.widget(), monitor.title)

    # ------- global logging -------
    @QtCore.Slot(str)
    def log(self, line: str):
        self.log_view.appendPlainText(line)

def main():
    app = QtWidgets.QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()

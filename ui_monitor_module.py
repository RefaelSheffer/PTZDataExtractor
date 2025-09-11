# ui_monitor_module.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Module for multi-camera monitoring with optional orthomosaic display."""

from typing import List, Optional
from PySide6 import QtCore, QtGui, QtWidgets
import vlc

from ui_common import VlcVideoWidget
import shared_state


class _ScaledLabel(QtWidgets.QLabel):
    """QLabel that keeps pixmap scaled to current size."""

    def __init__(self):
        super().__init__()
        self.setAlignment(QtCore.Qt.AlignCenter)
        self.setStyleSheet("background:#222; color:#ccc;")
        self._pix: Optional[QtGui.QPixmap] = None

    def setPixmap(self, pix: QtGui.QPixmap):  # type: ignore[override]
        self._pix = pix
        if pix and not pix.isNull():
            super().setPixmap(self._scaled_pix())
        else:
            super().setPixmap(pix)

    def resizeEvent(self, e: QtGui.QResizeEvent):  # type: ignore[override]
        super().resizeEvent(e)
        if self._pix and not self._pix.isNull():
            super().setPixmap(self._scaled_pix())

    def _scaled_pix(self) -> QtGui.QPixmap:
        return self._pix.scaled(
            self.size(),
            QtCore.Qt.KeepAspectRatio,
            QtCore.Qt.SmoothTransformation,
        )


class _VideoFrame(VlcVideoWidget):
    """Clickable video widget that emits on double click."""

    doubleClicked = QtCore.Signal()

    def mouseDoubleClickEvent(self, e: QtGui.QMouseEvent):  # type: ignore[override]
        self.doubleClicked.emit()
        super().mouseDoubleClickEvent(e)


class MonitorModule(QtCore.QObject):
    """Display multiple RTSP cameras and optional orthomosaic for one."""

    title = "Monitor"
    icon: Optional[QtGui.QIcon] = None

    def __init__(self, vlc_instance: vlc.Instance, log_func=lambda msg: None):
        super().__init__()
        self._vlc = vlc_instance
        self._log = log_func
        self._root = self._build_ui()

    # ----- public API -----
    def widget(self) -> QtWidgets.QWidget:
        return self._root

    # ----- internal UI -----
    def _build_ui(self) -> QtWidgets.QWidget:
        root = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(root)

        # camera list for selection
        self.cam_list = QtWidgets.QListWidget()
        self.cam_list.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        layout.addWidget(self.cam_list)

        # placeholder entries
        self.cam_list.addItem("Camera 1")
        self.cam_list.addItem("Camera 2")

        self.stack = QtWidgets.QStackedWidget()
        layout.addWidget(self.stack, 1)

        # --- grid page (cameras only) ---
        grid_page = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(grid_page)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(2)
        self._grid_videos: List[_VideoFrame] = []
        for i in range(2):
            vf = _VideoFrame(self._vlc)
            self._grid_videos.append(vf)
            grid.addWidget(vf, i // 2, i % 2)
            vf.doubleClicked.connect(lambda _, idx=i: self._open_from_grid(idx))
        self.stack.addWidget(grid_page)

        # --- ortho page (selected camera + orthomosaic) ---
        self._ortho_page = QtWidgets.QWidget()
        vb = QtWidgets.QVBoxLayout(self._ortho_page)
        self.btn_back = QtWidgets.QPushButton("Back to grid")
        vb.addWidget(self.btn_back, 0, QtCore.Qt.AlignLeft)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self._single_video = VlcVideoWidget(self._vlc)
        splitter.addWidget(self._single_video)
        self._ortho_label = _ScaledLabel()
        splitter.addWidget(self._ortho_label)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        vb.addWidget(splitter, 1)
        self.stack.addWidget(self._ortho_page)

        # connections
        self.btn_back.clicked.connect(self._show_grid)
        self.cam_list.itemDoubleClicked.connect(self._show_cam_with_ortho)

        self._show_grid()
        return root

    # ----- actions -----
    def _show_grid(self):
        self.stack.setCurrentIndex(0)

    def _show_cam_with_ortho(self, item: QtWidgets.QListWidgetItem):
        self.stack.setCurrentWidget(self._ortho_page)
        self._load_ortho()

    def _open_from_grid(self, idx: int):
        self.cam_list.setCurrentRow(idx)
        self._show_cam_with_ortho(self.cam_list.item(idx))

    def _load_ortho(self):
        path = getattr(shared_state, "orthophoto_path", None)
        if path:
            pix = QtGui.QPixmap(path)
            if not pix.isNull():
                self._ortho_label.setPixmap(pix)
            else:
                self._ortho_label.setText("Failed to load orthophoto")
        else:
            self._ortho_label.setText("No orthophoto loaded")


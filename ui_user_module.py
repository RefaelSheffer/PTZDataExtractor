# ui_user_module.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Simple operational tab for end users.
Currently a placeholder exposing a bare widget; real functionality
can be added later without requiring access to advanced settings."""

from PySide6 import QtCore, QtWidgets, QtGui
import vlc
from typing import Callable


class UserModule(QtCore.QObject):
    """Minimal module for everyday operators."""
    title = "User"
    icon = None

    def __init__(self, vlc_instance: vlc.Instance, log_func: Callable[[str], None] = print):
        super().__init__()
        self._vlc = vlc_instance
        self._log = log_func
        self._root = self._build_ui()

    def widget(self) -> QtWidgets.QWidget:
        return self._root

    def _build_ui(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(w)
        layout.addWidget(QtWidgets.QLabel("User-facing operations go here."))
        layout.addStretch(1)
        return w

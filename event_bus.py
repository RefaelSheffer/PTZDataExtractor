from PySide6 import QtCore

class _Bus(QtCore.QObject):
    signal_camera_changed = QtCore.Signal(object)
    signal_ortho_changed = QtCore.Signal(object)

bus = _Bus()

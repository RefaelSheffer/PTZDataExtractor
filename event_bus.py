from PySide6 import QtCore

class _Bus(QtCore.QObject):
    signal_camera_changed = QtCore.Signal(object)
    # (alias, raster layer)
    signal_ortho_changed = QtCore.Signal(str, object)

bus = _Bus()

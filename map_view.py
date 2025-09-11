from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Signal


class MapView(QtWidgets.QGraphicsView):
    clicked = Signal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRenderHints(QtGui.QPainter.Antialiasing |
                            QtGui.QPainter.SmoothPixmapTransform |
                            QtGui.QPainter.TextAntialiasing)
        self.setTransformationAnchor(QtWidgets.QGraphicsView.AnchorUnderMouse)
        self.setDragMode(QtWidgets.QGraphicsView.NoDrag)
        # --- picking/panning state ---
        self._pick_mode = False
        self._pick_max = 1
        self._pick_cb = None
        self._panning = False
        self._pan_start = QtCore.QPoint()

    def wheelEvent(self, e: QtGui.QWheelEvent):
        angle = e.angleDelta().y()
        if angle == 0:
            super().wheelEvent(e)
            return
        factor = 1.15 if angle > 0 else 1/1.15
        old_pos = self.mapToScene(e.position().toPoint())
        self.scale(factor, factor)
        new_pos = self.mapToScene(e.position().toPoint())
        delta = new_pos - old_pos
        self.translate(delta.x(), delta.y())

    def mousePressEvent(self, e: QtGui.QMouseEvent):
        if e.button() == QtCore.Qt.MiddleButton:
            self._panning = True
            self._pan_start = e.pos()
            self.setCursor(QtCore.Qt.ClosedHandCursor)
            e.accept(); return
        if e.button() == QtCore.Qt.LeftButton and self.scene() is not None:
            p = self.mapToScene(e.position().toPoint())
            if self._pick_mode and self._pick_cb:
                # single-click pick flow
                self._pick_mode = False
                self.setCursor(QtCore.Qt.ArrowCursor)
                self._pick_cb(p.x(), p.y())
                e.accept(); return
            self.clicked.emit(p.x(), p.y())
            e.accept(); return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e: QtGui.QMouseEvent):
        if self._panning:
            d = e.pos() - self._pan_start
            self._pan_start = e.pos()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - d.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - d.y())
            e.accept(); return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e: QtGui.QMouseEvent):
        if e.button() == QtCore.Qt.MiddleButton and self._panning:
            self._panning = False
            self.setCursor(QtCore.Qt.ArrowCursor)
            e.accept(); return
        super().mouseReleaseEvent(e)

    # ---- API for single-pick mode ----
    def enable_single_click(self, callback, max_points: int = 1):
        self._pick_mode = True
        self._pick_max = max_points
        self._pick_cb = callback
        self.setCursor(QtCore.Qt.CrossCursor)

    def disable_single_click(self):
        self._pick_mode = False
        self._pick_cb = None
        self.setCursor(QtCore.Qt.ArrowCursor)

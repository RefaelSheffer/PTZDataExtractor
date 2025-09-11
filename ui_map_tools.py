#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from PySide6 import QtCore, QtWidgets, QtGui
import numpy as np


def numpy_to_qimage(arr: np.ndarray) -> QtGui.QImage:
    """
    המרה של numpy array ל-QImage.
    תומך ב-Gray (HxW) וב-RGB (HxWx3, uint8).
    """
    if arr.ndim == 2:
        h, w = arr.shape
        qimg = QtGui.QImage(arr.data, w, h, w, QtGui.QImage.Format_Grayscale8)
        return qimg.copy()
    if arr.ndim == 3 and arr.shape[2] == 3:
        h, w, _ = arr.shape
        qimg = QtGui.QImage(arr.data, w, h, 3 * w, QtGui.QImage.Format_RGB888)
        return qimg.copy()
    raise ValueError("Unsupported ndarray shape for qimage")


class MapView(QtWidgets.QGraphicsView):
    """
    תצוגת מפה עם:
    - קליק → אות (x,y) בקואורדינטות Scene
    - זום בגלגל עכבר
    - סימון מצלמה קבוע (CAM) שלא נמחק ב-Reset כיול
    - פריטים זמניים לכיול (נקודות ממוספרות)
    """
    clicked = QtCore.Signal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRenderHints(
            QtGui.QPainter.Antialiasing
            | QtGui.QPainter.SmoothPixmapTransform
            | QtGui.QPainter.TextAntialiasing
        )
        self.setDragMode(QtWidgets.QGraphicsView.NoDrag)
        self.setTransformationAnchor(QtWidgets.QGraphicsView.AnchorUnderMouse)
        self._marker_cam = None       # QGraphicsItemGroup
        self._temp_items = []         # calibration-only
        self._panning = False
        self._pan_start = QtCore.QPoint()

    # אינטראקציה
    def mousePressEvent(self, e: QtGui.QMouseEvent):
        if e.button() == QtCore.Qt.MiddleButton:
            self._panning = True
            self._pan_start = e.pos()
            self.setCursor(QtCore.Qt.ClosedHandCursor)
            e.accept()
            return
        if e.button() == QtCore.Qt.LeftButton and self.scene() is not None:
            p = self.mapToScene(e.position().toPoint())
            self.clicked.emit(p.x(), p.y())
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e: QtGui.QMouseEvent):
        if self._panning:
            delta = e.pos() - self._pan_start
            self._pan_start = e.pos()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e: QtGui.QMouseEvent):
        if e.button() == QtCore.Qt.MiddleButton and self._panning:
            self._panning = False
            self.setCursor(QtCore.Qt.ArrowCursor)
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def wheelEvent(self, e: QtGui.QWheelEvent):
        if e.angleDelta().y() == 0:
            return super().wheelEvent(e)
        factor = 1.15 if e.angleDelta().y() > 0 else 1 / 1.15
        old_pos = self.mapToScene(e.position().toPoint())
        self.scale(factor, factor)
        new_pos = self.mapToScene(e.position().toPoint())
        delta = new_pos - old_pos
        self.translate(delta.x(), delta.y())

    # תצוגה
    def fit(self, margin=20):
        if self.scene() is None:
            return
        r = self.sceneRect()
        r = QtCore.QRectF(r.x()-margin, r.y()-margin, r.width()+2*margin, r.height()+2*margin)
        if r.isValid():
            self.fitInView(r, QtCore.Qt.KeepAspectRatio)

    # כיול (פריטים זמניים)
    def clear_temp(self):
        if self.scene() is None:
            return
        for it in self._temp_items:
            if it.scene() is not None:
                self.scene().removeItem(it)
        self._temp_items.clear()

    def add_numbered_marker(self, idx: int, xs: float, ys: float, color=QtGui.QColor(80, 200, 80)):
        if self.scene() is None:
            return
        pen = QtGui.QPen(color, 2)
        brush = QtGui.QBrush(color)
        dot = self.scene().addEllipse(-4, -4, 8, 8, pen, brush)
        dot.setPos(xs, ys)
        lab = QtWidgets.QGraphicsSimpleTextItem(str(idx))
        lab.setBrush(QtGui.QBrush(QtGui.QColor(220, 255, 220)))
        lab.setPos(xs+6, ys-6)
        self.scene().addItem(lab)
        self._temp_items += [dot, lab]
        return dot, lab

    # CAM קבוע
    def set_marker(self, xs: float, ys: float):
        if self.scene() is None:
            self.setScene(QtWidgets.QGraphicsScene())
        pen = QtGui.QPen(QtGui.QColor(255, 80, 80), 2)
        brush = QtGui.QBrush(QtGui.QColor(255, 80, 80))
        if self._marker_cam is None:
            grp = QtWidgets.QGraphicsItemGroup()
            dot = self.scene().addEllipse(-5, -5, 10, 10, pen, brush); grp.addToGroup(dot)
            l1 = self.scene().addLine(-12, 0, 12, 0, pen); grp.addToGroup(l1)
            l2 = self.scene().addLine(0, -12, 0, 12, pen); grp.addToGroup(l2)
            lab = QtWidgets.QGraphicsSimpleTextItem("CAM")
            lab.setBrush(QtGui.QBrush(QtGui.QColor(255, 200, 200)))
            lab.setPos(8, -22); grp.addToGroup(lab)
            self.scene().addItem(grp)
            grp.setZValue(1000)
            grp.setData(0, "cam")
            self._marker_cam = grp
        self._marker_cam.setPos(xs, ys)
        self._marker_cam.setVisible(True)

"""Simple dialog for manual camera calibration.

Provides utilities to estimate roll from the horizon and azimuth from two
points on an orthophoto.
"""

from PySide6 import QtCore, QtWidgets

from calibration_utils import roll_error_from_horizon, azimuth_from_ortho_points


class HorizonAzimuthCalibrationDialog(QtWidgets.QDialog):
    """Dialog collecting samples for roll and azimuth calibration."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Horizon / Azimuth Calibration")
        form = QtWidgets.QFormLayout(self)

        # horizon inputs
        self.spn_y_left = QtWidgets.QDoubleSpinBox(); self.spn_y_left.setRange(-1e6, 1e6)
        self.spn_y_right = QtWidgets.QDoubleSpinBox(); self.spn_y_right.setRange(-1e6, 1e6)
        self.spn_width = QtWidgets.QSpinBox(); self.spn_width.setRange(1, 100000)
        form.addRow("Horizon Y left:", self.spn_y_left)
        form.addRow("Horizon Y right:", self.spn_y_right)
        form.addRow("Sample width:", self.spn_width)

        # orthophoto inputs
        self.spn_x1 = QtWidgets.QDoubleSpinBox(); self.spn_x1.setRange(-1e9, 1e9)
        self.spn_y1 = QtWidgets.QDoubleSpinBox(); self.spn_y1.setRange(-1e9, 1e9)
        self.spn_x2 = QtWidgets.QDoubleSpinBox(); self.spn_x2.setRange(-1e9, 1e9)
        self.spn_y2 = QtWidgets.QDoubleSpinBox(); self.spn_y2.setRange(-1e9, 1e9)
        form.addRow("Ortho X1:", self.spn_x1)
        form.addRow("Ortho Y1:", self.spn_y1)
        form.addRow("Ortho X2:", self.spn_x2)
        form.addRow("Ortho Y2:", self.spn_y2)

        # results
        self.lbl_roll = QtWidgets.QLabel("Roll: –")
        self.lbl_az = QtWidgets.QLabel("Azimuth: –")
        form.addRow(self.lbl_roll)
        form.addRow(self.lbl_az)

        btn = QtWidgets.QPushButton("Compute")
        btn.clicked.connect(self._compute)
        form.addRow(btn)

    # slot
    def _compute(self):
        width = self.spn_width.value()
        y_left = self.spn_y_left.value()
        y_right = self.spn_y_right.value()
        roll = roll_error_from_horizon(y_left, y_right, int(width))
        self.lbl_roll.setText(f"Roll: {roll:.2f}°")

        x1, y1 = self.spn_x1.value(), self.spn_y1.value()
        x2, y2 = self.spn_x2.value(), self.spn_y2.value()
        az = azimuth_from_ortho_points(x1, y1, x2, y2)
        self.lbl_az.setText(f"Azimuth: {az:.2f}°")

import math
import time
import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).resolve().parent.parent))

from onvif_ptz import PTZReading, PtzMetaThread


class DummyClient:
    def __init__(self):
        self.poll_dt = 0.01
        self._last = PTZReading(pan_deg=1.0, tilt_deg=2.0, zoom_mm=3.0, focus_pos=4.0)

    def start(self):
        pass

    def stop(self):
        pass

    def last(self):
        return self._last


def test_ptz_meta_thread_custom_client():
    client = DummyClient()
    th = PtzMetaThread(client=client, sensor_width_mm=6.0)
    th.start()
    time.sleep(0.05)
    meta = th.last()
    th.stop()
    assert meta is not None
    assert meta.pan_deg == 1.0
    assert meta.focus_pos == 4.0
    assert math.isclose(meta.hfov_deg, 90.0, rel_tol=1e-6)

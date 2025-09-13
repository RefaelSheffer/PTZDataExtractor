import time
import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).resolve().parent.parent))

import shared_state
from ptz_cgi import PtzCgiThread


class DummyPtzCgi(PtzCgiThread):
    def __init__(self):
        super().__init__("localhost", 80, "u", "p", poll_hz=100.0)

    def _fetch_text(self):  # override network call
        return "pan=1.0&tilt=2.0&zoom=0.25&focus=3.0", 200


def test_ptz_cgi_thread_shared_state():
    shared_state.update_ptz_meta(None)
    th = DummyPtzCgi()
    th.start()
    time.sleep(0.05)
    th.stop()
    meta = shared_state.ptz_meta
    assert meta is not None
    assert meta["pan_deg"] == 1.0
    assert meta["focus_pos"] == 3.0
    assert meta.get("cgi_last", {}).get("pan_deg") == 1.0
    assert th.last().focus_pos == 3.0


def test_ptz_cgi_urls_try_channel_and_without():
    th = PtzCgiThread("host", 80, "u", "p", channel=1)
    urls = th._urls
    base = "http://host:80/cgi-bin/ptz.cgi?action=getStatus"
    assert base in urls
    assert base + "&channel=1" in urls

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Simple PTZ polling via Dahua-style CGI."""
from __future__ import annotations

import csv
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Optional
from urllib.error import URLError
from urllib.request import (
    HTTPBasicAuthHandler,
    HTTPPasswordMgrWithDefaultRealm,
    Request,
    build_opener,
)

from onvif_ptz import PTZReading


@dataclass
class _Status:
    pan_deg: Optional[float] = None
    tilt_deg: Optional[float] = None
    zoom_norm: Optional[float] = None
    focus_pos: Optional[float] = None


class PtzCgiThread:
    """Polling thread for /cgi-bin/ptz.cgi?action=getStatus."""

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        pwd: str,
        channel: int = 1,
        poll_hz: float = 5.0,
        https: bool = False,
        csv_path: Optional[str] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.pwd = pwd
        self.channel = channel
        self.poll_dt = 1.0 / max(0.5, float(poll_hz))
        self.https = https
        self._csv_path = csv_path

        proto = "https" if https else "http"
        self._url = f"{proto}://{host}:{port}/cgi-bin/ptz.cgi?action=getStatus"
        if channel is not None:
            self._url += f"&channel={int(channel)}"

        self._opener = None
        self._last = PTZReading()
        self._status = _Status()
        self._csv_file = None
        self._csv_writer = None
        self._th: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._has_data = False

    def start(self) -> None:
        self._stop.clear()
        self._build_opener()
        self._th = threading.Thread(target=self._run, daemon=True)
        self._th.start()

    def stop(self) -> None:
        self._stop.set()
        if self._th:
            self._th.join(timeout=2.0)
            self._th = None
        if self._csv_file:
            try:
                self._csv_file.close()
            except Exception:
                pass
            self._csv_file = None
            self._csv_writer = None

    def last(self) -> PTZReading:
        return self._last

    # ----- internal -----
    def _build_opener(self) -> None:
        mgr = HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, self._url, self.user, self.pwd)
        handler = HTTPBasicAuthHandler(mgr)
        self._opener = build_opener(handler)

    def _fetch_text(self) -> Optional[str]:
        if not self._opener:
            return None
        try:
            with self._opener.open(Request(self._url), timeout=2.0) as resp:
                return resp.read().decode("utf-8", errors="ignore")
        except URLError:
            return None
        except Exception:
            return None

    def _normalize_zoom(self, z: Optional[float]) -> Optional[float]:
        if z is None:
            return None
        z = float(z)
        if 0.0 <= z <= 1.0:
            return z
        if 1.0 < z <= 100.0:
            return z / 100.0
        if 100.0 < z <= 255.0:
            return z / 255.0
        if 255.0 < z <= 1023.0:
            return z / 1023.0
        if 1023.0 < z <= 1024.0:
            return z / 1024.0
        return None

    def _parse(self, txt: str) -> _Status:
        txt = txt.strip()
        data = {}
        if txt.startswith("{"):
            try:
                data = json.loads(txt)
            except Exception:
                data = {}
        else:
            for k, v in re.findall(r"(\w+)=([^\s&]+)", txt):
                data[k.lower()] = v
        pan = self._to_float(data.get("pan"))
        tilt = self._to_float(data.get("tilt"))
        zoom_raw = self._to_float(data.get("zoom"))
        zoom = self._normalize_zoom(zoom_raw)
        focus = self._to_float(data.get("focus"))
        return _Status(pan_deg=pan, tilt_deg=tilt, zoom_norm=zoom, focus_pos=focus)

    def _to_float(self, v) -> Optional[float]:
        try:
            return float(v)
        except Exception:
            return None

    def _run(self) -> None:
        if self._csv_path:
            try:
                self._csv_file = open(self._csv_path, "w", newline="", encoding="utf-8")
                self._csv_writer = csv.writer(self._csv_file)
                self._csv_writer.writerow(["ts", "pan_deg", "tilt_deg", "zoom_norm", "focus_pos"])
            except Exception:
                self._csv_file = None
                self._csv_writer = None
        while not self._stop.is_set():
            txt = self._fetch_text()
            if txt:
                if not self._has_data:
                    print(f"PTZ CGI data available: {txt.strip()}")
                    self._has_data = True
                st = self._parse(txt)
                self._status = st
                self._last.pan_deg = st.pan_deg
                self._last.tilt_deg = st.tilt_deg
                self._last.zoom_norm = st.zoom_norm
                self._last.focus_pos = st.focus_pos
                try:
                    import shared_state
                    shared_state.ptz_meta = {
                        "ts": time.time(),
                        "pan_deg": st.pan_deg,
                        "tilt_deg": st.tilt_deg,
                        "zoom_mm": None,
                        "zoom_norm": st.zoom_norm,
                        "pan_dps": None,
                        "tilt_dps": None,
                        "zoom_speed": None,
                        "hfov_deg": None,
                        "focus_pos": st.focus_pos,
                    }
                except Exception:
                    pass
                if self._csv_writer:
                    try:
                        self._csv_writer.writerow([
                            time.time(),
                            st.pan_deg,
                            st.tilt_deg,
                            st.zoom_norm,
                            st.focus_pos,
                        ])
                        self._csv_file.flush()
                    except Exception:
                        pass
            else:
                if self._has_data:
                    print("PTZ CGI data unavailable")
                    self._has_data = False
            time.sleep(self.poll_dt)

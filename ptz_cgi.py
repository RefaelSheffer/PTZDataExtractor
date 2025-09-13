#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Simple PTZ polling via Dahua-style CGI."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPBasicAuthHandler,
    HTTPDigestAuthHandler,
    HTTPPasswordMgrWithDefaultRealm,
    Request,
    build_opener,
)

from parser_dahua import parse_cgi_status
from ptz_csv_logger import log_ptz_row

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
        channel: Optional[int] = 1,
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
        base_urls = [
            f"{proto}://{host}:{port}/cgi-bin/ptz.cgi?action=getStatus",
            f"{proto}://{host}:{port}/ptz.cgi?action=getStatus",
            f"{proto}://{host}:{port}/cgi-bin/ptz?action=getStatus",
            f"{proto}://{host}:{port}/ptz?action=getStatus",
        ]
        urls = base_urls.copy()
        if channel is not None:
            urls = [u + f"&channel={int(channel)}" for u in base_urls] + urls
        self._urls = urls
        self._url_index = 0

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
        for url in self._urls:
            mgr.add_password(None, url, self.user, self.pwd)
        basic = HTTPBasicAuthHandler(mgr)
        digest = HTTPDigestAuthHandler(mgr)
        self._opener = build_opener(basic, digest)

    def _fetch_text(self) -> tuple[Optional[str], int]:
        if not self._opener:
            return None, -1
        for i in range(len(self._urls)):
            idx = (self._url_index + i) % len(self._urls)
            url = self._urls[idx]
            try:
                with self._opener.open(Request(url), timeout=2.0) as resp:
                    self._url_index = idx
                    body = resp.read().decode("utf-8", errors="ignore")
                    code = getattr(resp, "status", 200)
                    return body, code
            except HTTPError as e:
                self._url_index = idx
                try:
                    body = e.read().decode("utf-8", errors="ignore")
                except Exception:
                    body = ""
                return body, getattr(e, "code", -1)
            except URLError:
                continue
            except Exception:
                continue
        return None, -1

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

    def _to_deg(self, v, kind: str = "pan") -> Optional[float]:
        try:
            x = float(v)
        except Exception:
            return None
        if -360.0 <= x <= 360.0:
            return x
        if -1.01 <= x <= 1.01:
            if kind in ("pan", "tilt"):
                return x * 180.0
            return x
        if -0.01 <= x <= 1.01:
            if kind in ("pan", "tilt"):
                return x * 360.0
            return x
        return x

    def _parse(self, txt: str) -> _Status:
        """Parse CGI text using a tolerant Dahua parser."""
        parsed = parse_cgi_status(txt)
        pan = self._to_deg(parsed.get("pan"), "pan")
        tilt = self._to_deg(parsed.get("tilt"), "tilt")
        zoom_raw = self._to_deg(parsed.get("zoom"), "zoom")
        zoom = self._normalize_zoom(zoom_raw)
        # focus is not included in the Dahua status keys; attempt best effort
        focus = self._to_deg(parsed.get("raw", {}).get("status.FocusValue"), "zoom")
        return _Status(pan_deg=pan, tilt_deg=tilt, zoom_norm=zoom, focus_pos=focus)

    def _run(self) -> None:
        while not self._stop.is_set():
            txt, code = self._fetch_text()
            if txt and 200 <= code < 300:
                if not self._has_data:
                    print(f"PTZ CGI data available: {txt.strip()}")
                    self._has_data = True
                parsed = parse_cgi_status(txt)
                err = None
                if parsed.get("pan") is None or parsed.get("tilt") is None:
                    err = "missing pan/tilt (key mismatch?)"
                log_ptz_row(
                    source="CGI",
                    url=self._urls[self._url_index],
                    http_code=code,
                    channel=self.channel,
                    auth="Basic/Digest",
                    body=txt,
                    parsed=parsed,
                    err=err,
                )
                if not err:
                    st = _Status(
                        pan_deg=self._to_deg(parsed.get("pan"), "pan"),
                        tilt_deg=self._to_deg(parsed.get("tilt"), "tilt"),
                        zoom_norm=self._normalize_zoom(
                            self._to_deg(parsed.get("zoom"), "zoom")
                        ),
                        focus_pos=self._to_deg(parsed.get("focus"), "zoom"),
                    )
                    self._status = st
                    self._last.pan_deg = st.pan_deg
                    self._last.tilt_deg = st.tilt_deg
                    self._last.zoom_norm = st.zoom_norm
                    self._last.focus_pos = st.focus_pos
                    row = {
                        "ts": time.time(),
                        "pan_deg": getattr(st, "pan_deg", None),
                        "tilt_deg": getattr(st, "tilt_deg", None),
                        "zoom_norm": getattr(st, "zoom_norm", None),
                        "focus_pos": getattr(st, "focus_pos", None),
                    }
                    try:
                        import shared_state

                        shared_state.update_ptz_meta(
                            {
                                "ts": row["ts"],
                                "pan_deg": row["pan_deg"],
                                "tilt_deg": row["tilt_deg"],
                                "zoom_mm": None,
                                "zoom_norm": row["zoom_norm"],
                                "pan_dps": None,
                                "tilt_dps": None,
                                "zoom_speed": None,
                                "hfov_deg": None,
                                "focus_pos": row["focus_pos"],
                                "cgi_last": {
                                    "pan_deg": row["pan_deg"],
                                    "tilt_deg": row["tilt_deg"],
                                    "zoom": row["zoom_norm"],
                                },
                            }
                        )
                    except Exception:
                        pass
            else:
                log_ptz_row(
                    source="CGI",
                    url=self._urls[self._url_index],
                    http_code=code,
                    channel=self.channel,
                    auth="Basic/Digest",
                    body=txt or "",
                    parsed={},
                    err="no response" if code == -1 else f"http error {code}",
                )
                if self._has_data:
                    print("PTZ CGI data unavailable")
                    self._has_data = False
            time.sleep(self.poll_dt)

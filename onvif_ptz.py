#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Tuple
import threading
import time

# דרוש: pip install onvif-zeep
from onvif import ONVIFCamera


@dataclass
class PTZReading:
    pan_deg: Optional[float] = None    # מעלות (ENU, ימינה חיובי)
    tilt_deg: Optional[float] = None   # מעלות (מעלה חיובי)
    zoom_norm: Optional[float] = None  # 0..1 (אם לא נתמך mm)
    zoom_mm: Optional[float] = None    # focal length במ״מ (אם נתמך)
    focus_pos: Optional[float] = None  # יחסי/דיאופטרים/None (לפי מצלמה)


class OnvifPTZClient:
    """
    Polling קל ל-PTZ+Imaging. מותאם לדגמי Dahua/ONVIF.
    שימוש:
        c = OnvifPTZClient(host, port, user, pwd)
        c.start()
        ...
        r = c.last()
        ...
        c.stop()
    """
    def __init__(self, host: str, port: int, user: str, pwd: str, profile_index: int = 0, poll_hz: float = 5.0):
        self.host = host
        self.port = port
        self.user = user
        self.pwd = pwd
        self.profile_index = profile_index
        self.poll_dt = 1.0 / max(0.5, float(poll_hz))

        self._cam = None
        self._media = None
        self._ptz = None
        self._img = None
        self._prof = None

        self._pan_range_deg: Optional[Tuple[float, float]] = None  # אם נדרש להמרה
        self._tilt_range_deg: Optional[Tuple[float, float]] = None
        self._zoom_mm_range: Optional[Tuple[float, float]] = None  # אם נתמך mm

        self._last = PTZReading()
        self._th: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ---------- lifecycle ----------
    def start(self):
        self._stop.clear()
        self._th = threading.Thread(target=self._run, daemon=True)
        self._th.start()

    def stop(self):
        self._stop.set()
        if self._th:
            self._th.join(timeout=2.0)
            self._th = None

    def last(self) -> PTZReading:
        return self._last

    # ---------- internal ----------
    def _ensure_services(self):
        if self._cam:
            return
        self._cam = ONVIFCamera(self.host, self.port, self.user, self.pwd)
        self._media = self._cam.create_media_service()
        self._ptz = self._cam.create_ptz_service()
        self._img = self._cam.create_imaging_service()
        profiles = self._media.GetProfiles()
        if not profiles:
            raise RuntimeError("No ONVIF media profiles")
        self._prof = profiles[min(self.profile_index, len(profiles)-1)]
        self._detect_position_spaces()

    def _detect_position_spaces(self):
        # מילוי טווחים ויחידות ממסמך ה-Nodes
        nodes = self._ptz.GetNodes()
        pan_min = pan_max = tilt_min = tilt_max = None
        zmm_min = zmm_max = None

        for nd in nodes or []:
            # Pan/Tilt spaces
            for sp in getattr(nd.SupportedPanTiltSpaces, 'AbsolutePanTiltPositionSpace', []):
                rng = getattr(sp, "XRange", None)
                rny = getattr(sp, "YRange", None)
                uri = getattr(sp, "URI", "") or getattr(sp, "SpaceURI", "")
                if rng:
                    pan_min = float(getattr(rng, "Min", -180.0))
                    pan_max = float(getattr(rng, "Max",  180.0))
                if rny:
                    tilt_min = float(getattr(rny, "Min", -90.0))
                    tilt_max = float(getattr(rny, "Max",  90.0))
                # אם זה כבר במעלות – טוב. אם זה 0..1/−1..1 עדיין נקבל טווח
            # Zoom in mm?
            for zsp in getattr(nd.SupportedZoomSpaces, 'AbsoluteZoomPositionSpace', []):
                zuri = getattr(zsp, "URI", "") or getattr(zsp, "SpaceURI", "")
                if "Millimeter" in zuri or "PositionSpaceMillimeter" in zuri:
                    zr = getattr(zsp, "XRange", None)
                    if zr:
                        zmm_min = float(getattr(zr, "Min", 0.0))
                        zmm_max = float(getattr(zr, "Max", 0.0))

        if pan_min is not None and pan_max is not None:
            self._pan_range_deg = (pan_min, pan_max)
        if tilt_min is not None and tilt_max is not None:
            self._tilt_range_deg = (tilt_min, tilt_max)
        if zmm_min is not None and zmm_max is not None and zmm_max > zmm_min:
            self._zoom_mm_range = (zmm_min, zmm_max)

    def _convert_to_deg(self, val: Optional[float], rng: Optional[Tuple[float,float]]) -> Optional[float]:
        if val is None or rng is None:
            return None
        # הסטטוס עשוי להחזיר כבר במעלות – אבל אם לא, נניח שהוא normalized לטווח rng
        v = float(val)
        vmin, vmax = rng
        if vmin <= v <= vmax:
            # אם rng כבר במעלות, זה נחשב 'כבר מומר'
            # אין דרך ודאית לדעת; נבחר ישירות כ-degree
            return v
        # אחרת, אם נראה כמו 0..1:
        if 0.0 <= v <= 1.0:
            return vmin + v*(vmax - vmin)
        # או −1..1:
        if -1.0 <= v <= 1.0:
            return 0.5*(v+1.0)*(vmax - vmin) + vmin
        return v  # fallback

    def _run(self):
        try:
            self._ensure_services()
        except Exception:
            # לא נפליא—האפליקציה תציג חוסר חיבור
            return

        vs_token = getattr(self._prof.VideoSourceConfiguration, "SourceToken", None)

        while not self._stop.is_set():
            try:
                st = self._ptz.GetStatus({'ProfileToken': self._prof.token})
                pan  = getattr(getattr(st.Position, 'PanTilt', None), 'x', None)
                tilt = getattr(getattr(st.Position, 'PanTilt', None), 'y', None)
                zoom = getattr(getattr(st.Position, 'Zoom', None), 'x', None)

                pan_deg  = self._convert_to_deg(pan,  self._pan_range_deg)
                tilt_deg = self._convert_to_deg(tilt, self._tilt_range_deg)

                zoom_mm = None
                if self._zoom_mm_range and zoom is not None:
                    # נניח שהסטטוס מחזיר normalized 0..1
                    zmin, zmax = self._zoom_mm_range
                    zv = float(zoom)
                    if 0.0 <= zv <= 1.0:
                        zoom_mm = zmin + (zmax - zmin)*zv
                    else:
                        # חלק מהדגמים כבר מחזירים במ״מ
                        zoom_mm = zv

                focus_pos = None
                if vs_token:
                    try:
                        ist = self._img.GetStatus({'VideoSourceToken': vs_token})
                        focus_pos = getattr(getattr(ist, 'FocusStatus', None), 'Position', None) \
                                    or getattr(getattr(ist, 'FocusStatus20', None), 'Position', None)
                    except Exception:
                        pass

                self._last = PTZReading(
                    pan_deg=pan_deg, tilt_deg=tilt_deg, zoom_norm=float(zoom) if zoom is not None else None,
                    zoom_mm=zoom_mm, focus_pos=focus_pos
                )
            except Exception:
                pass

            time.sleep(self.poll_dt)

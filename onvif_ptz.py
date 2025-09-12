#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Tuple
import threading
import time
import csv
import math

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


@dataclass
class PTZMeta(PTZReading):
    """מצב PTZ + נגזרות וחישובי HFOV."""
    ts: float = 0.0                       # זמן UNIX
    pan_dps: Optional[float] = None       # מעלות/שניה
    tilt_dps: Optional[float] = None      # מעלות/שניה
    zoom_speed: Optional[float] = None    # mm/s או norm/s
    hfov_deg: Optional[float] = None      # שדה ראיה אופקי מחושב


class PtzMetaThread:
    """
    Thread ייעודי שמבצע polling ל-PTZ ומחשב נגזרות + HFOV.
    יכול גם לכתוב לקובץ CSV.
    """
    def __init__(self, host: Optional[str] = None, port: Optional[int] = None,
                 user: Optional[str] = None, pwd: Optional[str] = None,
                 profile_index: int = 0, poll_hz: float = 5.0,
                 sensor_width_mm: float = 6.4, csv_path: Optional[str] = None,
                 client: Optional[object] = None):
        """
        יצירת שרשור מטה ל-PTZ.

        ניתן לספק לקוח PTZ חלופי שאינו מבוסס ONVIF (למשל CGI או טלמטריה
        חיצונית). הלקוח צריך לספק מתודות start/stop/last ולקבוע poll_dt.
        אם לא סופק לקוח כזה, ישמש OnvifPTZClient הרגיל.
        """
        if client is None:
            if host is None or port is None or user is None or pwd is None:
                raise ValueError("host/port/user/pwd required when client not provided")
            self._client = OnvifPTZClient(host, port, user, pwd,
                                          profile_index=profile_index, poll_hz=poll_hz)
        else:
            self._client = client
        self._sensor_width_mm = sensor_width_mm
        self._csv_path = csv_path
        self._csv_file = None
        self._csv_writer = None
        self._last: Optional[PTZMeta] = None
        self._th: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self):
        self._client.start()
        self._stop.clear()
        self._th = threading.Thread(target=self._run, daemon=True)
        self._th.start()

    def stop(self):
        self._stop.set()
        if self._th:
            self._th.join(timeout=2.0)
            self._th = None
        self._client.stop()
        if self._csv_file:
            try:
                self._csv_file.close()
            except Exception:
                pass
            self._csv_file = None
            self._csv_writer = None

    def last(self) -> Optional[PTZMeta]:
        return self._last

    # ----- internal -----
    def _run(self):
        prev: Optional[PTZMeta] = None
        if self._csv_path:
            try:
                self._csv_file = open(self._csv_path, 'w', newline='', encoding='utf-8')
                self._csv_writer = csv.writer(self._csv_file)
                self._csv_writer.writerow([
                    'ts', 'pan_deg', 'tilt_deg', 'zoom',
                    'pan_dps', 'tilt_dps', 'zoom_speed', 'hfov_deg'
                ])
            except Exception:
                self._csv_file = None
                self._csv_writer = None

        while not self._stop.is_set():
            r = self._client.last()
            ts = time.time()

            pan_deg = r.pan_deg
            tilt_deg = r.tilt_deg
            zoom_mm = r.zoom_mm
            zoom_norm = r.zoom_norm

            pan_dps = tilt_dps = zoom_speed = hfov_deg = None
            if prev is not None:
                dt = ts - prev.ts
                if dt > 0:
                    if pan_deg is not None and prev.pan_deg is not None:
                        pan_dps = (pan_deg - prev.pan_deg) / dt
                    if tilt_deg is not None and prev.tilt_deg is not None:
                        tilt_dps = (tilt_deg - prev.tilt_deg) / dt
                    if zoom_mm is not None and prev.zoom_mm is not None:
                        zoom_speed = (zoom_mm - prev.zoom_mm) / dt
                    elif zoom_norm is not None and prev.zoom_norm is not None:
                        zoom_speed = (zoom_norm - prev.zoom_norm) / dt

            if zoom_mm is not None and self._sensor_width_mm > 0:
                try:
                    hfov_deg = math.degrees(2.0 * math.atan(self._sensor_width_mm / (2.0 * zoom_mm)))
                except Exception:
                    hfov_deg = None

            meta = PTZMeta(ts=ts, pan_deg=pan_deg, tilt_deg=tilt_deg,
                           zoom_norm=zoom_norm, zoom_mm=zoom_mm,
                           pan_dps=pan_dps, tilt_dps=tilt_dps,
                           zoom_speed=zoom_speed, hfov_deg=hfov_deg,
                           focus_pos=r.focus_pos)
            self._last = meta

            try:
                import shared_state
                shared_state.update_ptz_meta({
                    'ts': meta.ts,
                    'pan_deg': meta.pan_deg,
                    'tilt_deg': meta.tilt_deg,
                    'zoom_mm': meta.zoom_mm,
                    'zoom_norm': meta.zoom_norm,
                    'pan_dps': meta.pan_dps,
                    'tilt_dps': meta.tilt_dps,
                    'zoom_speed': meta.zoom_speed,
                    'hfov_deg': meta.hfov_deg,
                    'focus_pos': meta.focus_pos,
                })
            except Exception:
                pass

            if self._csv_writer:
                try:
                    self._csv_writer.writerow([
                        ts,
                        pan_deg,
                        tilt_deg,
                        zoom_mm if zoom_mm is not None else zoom_norm,
                        pan_dps,
                        tilt_dps,
                        zoom_speed,
                        hfov_deg,
                    ])
                    self._csv_file.flush()
                except Exception:
                    pass

            prev = meta
            time.sleep(getattr(self._client, 'poll_dt', 1.0))

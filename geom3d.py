#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
from dataclasses import dataclass
import math
from typing import Optional, Dict, Tuple, Any  # הוספתי Any עבור to_dict/from_dict

import numpy as np
from pyproj import Transformer


# ---------------- Camera models ----------------
@dataclass
class CameraIntrinsics:
    width: int
    height: int
    fx: float
    fy: float
    cx: float
    cy: float

    @staticmethod
    def from_fov(width: int, height: int, hfov_deg: float) -> "CameraIntrinsics":
        """בונה fx/fy מ-FOV אופקי, מניח פיקסלים ריבועיים."""
        hfov = math.radians(hfov_deg)
        fx = (width / 2.0) / math.tan(hfov / 2.0)
        fy = fx
        cx = width / 2.0
        cy = height / 2.0
        return CameraIntrinsics(width, height, fx, fy, cx, cy)

    # ---------- IO helpers ----------
    def to_dict(self) -> Dict[str, Any]:
        # כולל skew=0.0 לשמירה על תאימות לקבצים קיימים
        return {
            "width": int(self.width),
            "height": int(self.height),
            "fx": float(self.fx),
            "fy": float(self.fy),
            "cx": float(self.cx),
            "cy": float(self.cy),
            "skew": 0.0,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "CameraIntrinsics":
        return CameraIntrinsics(
            int(d.get("width", 0)),
            int(d.get("height", 0)),
            float(d.get("fx", 0.0)),
            float(d.get("fy", d.get("fx", 0.0))),  # fallback אם fy חסר
            float(d.get("cx", 0.0)),
            float(d.get("cy", 0.0)),
        )


@dataclass
class CameraPose:
    """מיקום (x,y,z) ומסוב יאו/פיץ'/רול במעלות. מערכת מקומית (ENU באתר)."""
    x: float
    y: float
    z: float
    yaw: float   # סביב Z (מעלות)
    pitch: float # סביב Y (מעלות, חיובי למטה/למעלה לפי קונבנציה שלך)
    roll: float  # סביב X (מעלות)

    def R_wc(self) -> np.ndarray:
        """Rotation world←camera. שימוש בסדר Rz(yaw)*Ry(pitch)*Rx(roll)"""
        rz = _Rz(math.radians(self.yaw))
        ry = _Ry(math.radians(self.pitch))
        rx = _Rx(math.radians(self.roll))
        return rz @ ry @ rx

    def t_w(self) -> np.ndarray:
        return np.array([self.x, self.y, self.z], dtype=float)

    # ---------- IO helpers ----------
    def to_dict(self) -> Dict[str, Any]:
        # סכימה: yaw_deg/pitch_deg/roll_deg כדי להישאר עקבי
        return {
            "x": float(self.x),
            "y": float(self.y),
            "z": float(self.z),
            "yaw_deg": float(self.yaw),
            "pitch_deg": float(self.pitch),
            "roll_deg": float(self.roll),
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "CameraPose":
        # תומך גם במפתחות ישנים yaw/pitch/roll
        yaw = float(d.get("yaw_deg", d.get("yaw", 0.0)))
        pitch = float(d.get("pitch_deg", d.get("pitch", 0.0)))
        roll = float(d.get("roll_deg", d.get("roll", 0.0)))
        return CameraPose(
            float(d.get("x", 0.0)),
            float(d.get("y", 0.0)),
            float(d.get("z", 0.0)),
            yaw, pitch, roll
        )


def _Rx(a):  # noqa
    c, s = math.cos(a), math.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], dtype=float)


def _Ry(a):  # noqa
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], dtype=float)


def _Rz(a):  # noqa
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], dtype=float)


def camera_ray_in_world(px: int, py: int, intr: CameraIntrinsics, pose: CameraPose) -> Tuple[np.ndarray, np.ndarray]:
    """
    קרן מהפיקסל (px,py) לעולם.
    origin = מיקום המצלמה, direction = היחידה בכיוון היעד.
    """
    x_cam = (px - intr.cx) / intr.fx
    y_cam = (py - intr.cy) / intr.fy
    d_cam = np.array([x_cam, y_cam, 1.0], dtype=float)
    d_cam = d_cam / np.linalg.norm(d_cam)
    R = pose.R_wc()
    d_w = R @ d_cam
    o_w = pose.t_w()
    return o_w, d_w / np.linalg.norm(d_w)


# ---------------- Geo reference ----------------
@dataclass
class GeoRef:
    origin_lat: float
    origin_lon: float
    origin_alt: float
    yaw_site_deg: float = 0.0    # סיבוב מערכת מקומית לעומת ENU (מעלות, חיובי נגד כיוון השעון)
    projected_epsg: Optional[int] = None

    def to_dict(self) -> Dict:
        return {
            "origin_lat": self.origin_lat,
            "origin_lon": self.origin_lon,
            "origin_alt": self.origin_alt,
            "yaw_site_deg": self.yaw_site_deg,
            "projected_epsg": self.projected_epsg,
        }

    @staticmethod
    def from_dict(d: Dict) -> "GeoRef":
        return GeoRef(
            d.get("origin_lat", 0.0),
            d.get("origin_lon", 0.0),
            d.get("origin_alt", 0.0),
            d.get("yaw_site_deg", 0.0),
            d.get("projected_epsg"),
        )

    # --- המרות ---
    def _proj(self) -> Optional[Transformer]:
        if self.projected_epsg:
            return Transformer.from_crs("EPSG:4326", f"EPSG:{self.projected_epsg}", always_xy=True)
        return None

    def _proj_inv(self) -> Optional[Transformer]:
        if self.projected_epsg:
            return Transformer.from_crs(f"EPSG:{self.projected_epsg}", "EPSG:4326", always_xy=True)
        return None

    def geographic_to_local(self, lat: float, lon: float, alt: float) -> np.ndarray:
        """
        LLA → קואורדינטות מקומיות (x,y,z) באתר.
        אם מוגדר EPSG פרויקטד – נשתמש בו; אחרת נחושב ENU בקירוב קטן.
        """
        if self.projected_epsg:
            tr = self._proj()
            X0, Y0 = tr.transform(self.origin_lon, self.origin_lat)
            X, Y = tr.transform(lon, lat)
            dx, dy = X - X0, Y - Y0
        else:
            # ENU בקירוב קטן (לא לשטחים עצומים)
            dx, dy = _geodetic_dxdy(self.origin_lat, self.origin_lon, lat, lon)
        # סיבוב אתר
        a = math.radians(self.yaw_site_deg)
        c, s = math.cos(a), math.sin(a)
        x = c * dx + s * dy
        y = -s * dx + c * dy
        z = alt - self.origin_alt
        return np.array([x, y, z], dtype=float)

    def local_to_geographic(self, p: np.ndarray) -> Dict:
        """
        (x,y,z) מקומי → LLA וגם החזרה לקואורדינטות פרויקטד (אם קיימות).
        """
        x, y, z = float(p[0]), float(p[1]), float(p[2])
        # להחזיר מסיבוב האתר
        a = math.radians(self.yaw_site_deg)
        c, s = math.cos(a), math.sin(a)
        dx = c * x - s * y
        dy = s * x + c * y

        if self.projected_epsg:
            tr_inv = self._proj_inv()
            X0, Y0 = Transformer.from_crs("EPSG:4326", f"EPSG:{self.projected_epsg}", always_xy=True)\
                                .transform(self.origin_lon, self.origin_lat)
            X = X0 + dx; Y = Y0 + dy
            lon, lat = tr_inv.transform(X, Y)
            prj = {"x": X, "y": Y, "epsg": self.projected_epsg}
        else:
            lat, lon = _geodetic_dxdy_inv(self.origin_lat, self.origin_lon, dx, dy)
            prj = None

        alt = self.origin_alt + z
        return {"lla": {"lat": lat, "lon": lon, "alt": alt}, "projected": prj}


# פונקציות עזר פומביות לשימוש חיצוני
def geographic_to_local(georef: GeoRef, lat: float, lon: float, alt: float) -> np.ndarray:
    return georef.geographic_to_local(lat, lon, alt)


def llh_to_enu(lat0, lon0, h0, lat, lon, h):
    """עזר כללי: LLA יחסית ל-LLA0 → ENU (בקירוב קטן)."""
    dx, dy = _geodetic_dxdy(lat0, lon0, lat, lon)
    dz = h - h0
    return np.array([dx, dy, dz], dtype=float)


# ---------------- Ray ∩ DTM ----------------
def intersect_ray_with_dtm(o: np.ndarray, d: np.ndarray, dtm, georef: GeoRef,
                           t_min: float = 0.0, t_max: float = 5000.0, step: float = 2.0) -> Optional[np.ndarray]:
    """
    אינטרסקציה גסה (צעד/חיפוש) של קרן עם פני ה-DTM.
    - o,d במערכת המקומית (אתר) [מטר].
    - דוגמים גובה DTM לפי הקרנה למערכת הפרויקטד של ה-DTM (EPSG).
    """
    assert hasattr(dtm, "info") and hasattr(dtm, "sample")

    # טרנספורמר: מקומי→פרויקטד (אם קיים)
    if georef.projected_epsg is None:
        # אם אין EPSG בפרויקט – לא יודעים לדגום DTM (שהוא GeoTIFF). נוודא קיום EPSG:
        epsg = dtm.info.crs_epsg
        if epsg:
            georef = GeoRef(georef.origin_lat, georef.origin_lon, georef.origin_alt, georef.yaw_site_deg, epsg)
        else:
            return None

    tr_inv = Transformer.from_crs("EPSG:4326", f"EPSG:{georef.projected_epsg}", always_xy=True)
    X0, Y0 = tr_inv.transform(georef.origin_lon, georef.origin_lat)

    def local_to_proj(x, y):
        # לבטל סיבוב אתר
        a = math.radians(georef.yaw_site_deg); c, s = math.cos(a), math.sin(a)
        dx = c * x - s * y
        dy = s * x + c * y
        return X0 + dx, Y0 + dy

    # דגימת צעד קדימה עד חציית פני השטח, ואז עידון בינארי
    prev_t = t_min
    prev_p = o + d * prev_t
    Xp, Yp = local_to_proj(prev_p[0], prev_p[1])
    z_prev = dtm.sample(Xp, Yp)
    if z_prev is None:
        z_prev = -1e9  # מחוץ לטווח – נכריח לבדיקה המשכית

    t = t_min + step
    while t <= t_max:
        p = o + d * t
        Xp, Yp = local_to_proj(p[0], p[1])
        z_ground = dtm.sample(Xp, Yp)
        if z_ground is None:
            t += step
            continue
        if (prev_p[2] - z_prev) * (p[2] - z_ground) <= 0:
            # חצייה – עדין בינארית
            lo, hi = prev_t, t
            for _ in range(25):
                mid = 0.5 * (lo + hi)
                pm = o + d * mid
                Xm, Ym = local_to_proj(pm[0], pm[1])
                zg = dtm.sample(Xm, Ym)
                if zg is None:
                    lo = mid
                    continue
                if (prev_p[2] - z_prev) * (pm[2] - zg) <= 0:
                    hi = mid
                else:
                    prev_t, prev_p, z_prev = mid, pm, zg
                    lo = mid
            return o + d * 0.5 * (lo + hi)
        prev_t, prev_p, z_prev = t, p, z_ground
        t += step

    return None


# ---------------- internal small-geo helpers ----------------
def _geodetic_dxdy(lat0, lon0, lat, lon):
    # קירוב מטרי קטן: Δx≈R*Δlon*cos(lat0), Δy≈R*Δlat
    R = 6378137.0
    dlon = math.radians(lon - lon0)
    dlat = math.radians(lat - lat0)
    x = R * dlon * math.cos(math.radians(lat0))
    y = R * dlat
    return x, y


def _geodetic_dxdy_inv(lat0, lon0, dx, dy):
    R = 6378137.0
    dlat = dy / R
    lat = lat0 + math.degrees(dlat)
    dlon = dx / (R * math.cos(math.radians(lat0)))
    lon = lon0 + math.degrees(dlon)
    return lat, lon

from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Literal, Tuple
import json

@dataclass
class Intrinsics:
    fx: float
    fy: float
    cx: float
    cy: float

@dataclass
class Distortion:
    k1: float
    k2: float
    p1: float
    p2: float
    k3: float | None = None

@dataclass
class LiveCameraContext:
    online: bool
    brand: str | None
    host: str
    port: int
    rtsp_url: str
    user: str | None
    transport: Literal["tcp", "udp"]
    codec: str | None
    width: int | None
    height: int | None
    fps: float | None
    serial: str | None
    model: str | None
    intrinsics: Optional[Intrinsics]
    distortion: Optional[Distortion]


class AppState:
    def __init__(self) -> None:
        self.current_camera: Optional[LiveCameraContext] = None
        self.stream_mode: str = "online"


app_state = AppState()


def load_calibration(serial: str | None, model: str | None, width: int | None, height: int | None) -> Tuple[Optional[Intrinsics], Optional[Distortion]]:
    """Attempt to load calibration JSON from the calibrations folder."""
    if width is None or height is None:
        return (None, None)
    calib_dir = Path(__file__).resolve().parent / "calibrations"
    candidates = []
    if serial:
        candidates.append(f"{serial}_{width}x{height}.json")
    if model:
        candidates.append(f"{model}_{width}x{height}.json")
    for name in candidates:
        p = calib_dir / name
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                intr_d = data.get("intrinsics") or {}
                intr = Intrinsics(
                    fx=float(intr_d.get("fx", 0.0)),
                    fy=float(intr_d.get("fy", 0.0)),
                    cx=float(intr_d.get("cx", 0.0)),
                    cy=float(intr_d.get("cy", 0.0)),
                )
                dist = None
                dist_d = data.get("distortion") or {}
                if dist_d:
                    dist = Distortion(
                        k1=float(dist_d.get("k1", 0.0)),
                        k2=float(dist_d.get("k2", 0.0)),
                        p1=float(dist_d.get("p1", 0.0)),
                        p2=float(dist_d.get("p2", 0.0)),
                        k3=float(dist_d["k3"]) if dist_d.get("k3") is not None else None,
                    )
                return intr, dist
            except Exception:
                continue
    return (None, None)

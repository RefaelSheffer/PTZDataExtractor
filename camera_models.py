# camera_models.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# IO helpers for camera models and preparation bundles.

from dataclasses import asdict, is_dataclass
from typing import Dict, Any
from pathlib import Path
import json

from geom3d import CameraIntrinsics, CameraPose, GeoRef  # noqa: F401 (GeoRef used by others)

CALIB_DIR = Path.cwd() / "calibrations"
CALIB_DIR.mkdir(exist_ok=True)

def _obj_to_dict(obj):
    if hasattr(obj, "to_dict"):
        try:
            return obj.to_dict()
        except Exception:
            pass
    if is_dataclass(obj):
        try:
            return asdict(obj)
        except Exception:
            pass
    # אחרון חביב: __dict__ (עלול להכיל גם שדות פנימיים)
    try:
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    except Exception:
        return {}

def save_bundle(
    name: str,
    intr: CameraIntrinsics,
    pose: CameraPose,
    mesh_path: str,
    meta: Dict[str, Any] = None,
    georef: Dict[str, Any] = None,
    yaw_offset_deg: float = 0.0,
    pitch_offset_deg: float = 0.0,
    roll_offset_deg: float = 0.0,
):
    """Persist a calibration bundle to disk.

    Orientation offsets are stored alongside other bundle data so that
    subsequent sessions can restore them without relying solely on RAM.
    """
    CALIB_DIR.mkdir(exist_ok=True, parents=True)
    data = {
        "name": name,
        "intrinsics": _obj_to_dict(intr),
        "pose": _obj_to_dict(pose),
        "terrain_path": mesh_path,
        "terrain_type": "mesh",
        "meta": meta or {},
        "georef": georef or {},
        "yaw_offset_deg": float(yaw_offset_deg),
        "pitch_offset_deg": float(pitch_offset_deg),
        "roll_offset_deg": float(roll_offset_deg),
    }
    out = CALIB_DIR / f"{name}.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(out)

def load_bundle(name: str):
    p = CALIB_DIR / f"{name}.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    # שימוש ב-from_dict החדשות; אם חסרות, ניפול לגרסאות dataclass
    intr = CameraIntrinsics.from_dict(d["intrinsics"]) if hasattr(CameraIntrinsics, "from_dict") else CameraIntrinsics(**d["intrinsics"])
    pose = CameraPose.from_dict(d["pose"]) if hasattr(CameraPose, "from_dict") else CameraPose(**{
        "x": d["pose"].get("x", 0.0),
        "y": d["pose"].get("y", 0.0),
        "z": d["pose"].get("z", 0.0),
        "yaw": d["pose"].get("yaw_deg", d["pose"].get("yaw", 0.0)),
        "pitch": d["pose"].get("pitch_deg", d["pose"].get("pitch", 0.0)),
        "roll": d["pose"].get("roll_deg", d["pose"].get("roll", 0.0)),
    })
    mesh_path = d.get("mesh_path")
    terrain_path = d.get("terrain_path", mesh_path)
    meta = d.get("meta", {})
    # Expose persisted orientation offsets via meta as well
    meta.setdefault("yaw_offset_deg", float(d.get("yaw_offset_deg", 0.0)))
    meta.setdefault("pitch_offset_deg", float(d.get("pitch_offset_deg", 0.0)))
    meta.setdefault("roll_offset_deg", float(d.get("roll_offset_deg", 0.0)))
    georef = d.get("georef", {})
    return intr, pose, terrain_path, meta, georef

def list_bundles():
    return [p.stem for p in CALIB_DIR.glob("*.json")]

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

def save_bundle(name: str, intr: CameraIntrinsics, pose: CameraPose, mesh_path: str,
                meta: Dict[str, Any] = None, georef: Dict[str, Any] = None):
    CALIB_DIR.mkdir(exist_ok=True, parents=True)
    data = {
        "name": name,
        "intrinsics": _obj_to_dict(intr),
        "pose": _obj_to_dict(pose),
        "terrain_path": mesh_path,
        "terrain_type": "mesh",
        "meta": meta or {},
        "georef": georef or {},
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
    georef = d.get("georef", {})
    return intr, pose, terrain_path, meta, georef

def list_bundles():
    return [p.stem for p in CALIB_DIR.glob("*.json")]

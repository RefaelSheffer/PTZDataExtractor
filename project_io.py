#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Helpers for loading and saving unified project files (.rtgproj).

A project file bundles camera connection settings, calibration bundle and
referenced layers into a single JSON document. The format is intentionally
simple and versioned so that future migrations can be handled.

Only a subset of the proposed schema is implemented here. The file currently
contains:

    {
        "schemaVersion": 1,
        "name": "MyScene",
        "camera": { ... profile dictionary ... },
        "bundle": {
            "name": "bundle_name",
            "intrinsics": { ... },
            "pose": { ... },
            "terrain_path": "${PROJECT_DIR}/relative/path"
        },
        "layers": {
            "dtm": "${PROJECT_DIR}/relative/path",
            "ortho": "${PROJECT_DIR}/relative/path",
            "srs": "EPSG:XXXX"
        }
    }

The helper functions below allow exporting such a project from existing
profiles/bundles and loading it back with path token expansion.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List
import json

from camera_models import load_bundle

SCHEMA_VERSION = 1
APP_DIR = Path(__file__).resolve().parent
PROFILES_PATH = APP_DIR / "profiles.json"

# ---------------- internal helpers ----------------

def _load_profiles(path: Path = PROFILES_PATH) -> List[dict]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []

def _tokenize_path(p: Path, base: Path) -> str:
    p = Path(p)
    try:
        rel = p.resolve().relative_to(base.resolve())
        return f"${{PROJECT_DIR}}/{rel.as_posix()}"
    except Exception:
        return str(p)

def _expand_path(s: str, base: Path) -> str:
    s = s.replace("${PROJECT_DIR}", str(base))
    s = s.replace("${THIS_FILE_DIR}", str(base))
    return s

# ---------------- public API ----------------

def export_project(out_path: Path, profile: Dict[str, Any] | str, bundle_name: str,
                   dtm_path: str, ortho_path: str, *,
                   profiles_path: Path = PROFILES_PATH,
                   srs: str = "EPSG:4326", project_name: str | None = None) -> Path:
    """Create a .rtgproj file that unifies profile, bundle and layers.

    Parameters
    ----------
    out_path : Path
        Where to write the project file.
    profile : dict or str
        Either a profile dictionary to embed directly, or the name of a profile
        that should be looked up in ``profiles.json``.
    bundle_name : str
        Name of the calibration bundle to embed (via camera_models).
    dtm_path, ortho_path : str
        Paths to raster layers referenced by the project.
    profiles_path : Path, optional
        Location of the profiles JSON when ``profile`` is a string. Defaults to
        the repository-level ``profiles.json``.
    srs : str, optional
        Spatial reference system code for the layers.
    project_name : str, optional
        Human friendly name for the project. Defaults to the profile's name.
    """
    out_path = Path(out_path)
    base = out_path.parent

    # Resolve profile: allow passing a dict directly or a profile name
    if isinstance(profile, str):
        profiles = _load_profiles(profiles_path)
        profile_dict = next((p for p in profiles if p.get("name") == profile), None)
        if profile_dict is None:
            raise ValueError(f"Profile '{profile}' not found in {profiles_path}")
    else:
        profile_dict = profile
        profile = profile_dict.get("name", "profile")

    intr, pose, terrain_path, meta, georef = load_bundle(bundle_name)

    data: Dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "name": project_name or profile,
        "camera": profile_dict,
        "bundle": {
            "name": bundle_name,
            "intrinsics": getattr(intr, "to_dict", lambda: intr)(),
            "pose": getattr(pose, "to_dict", lambda: pose)(),
            "terrain_path": _tokenize_path(Path(terrain_path), base),
            "meta": meta,
            "georef": georef,
        },
        "layers": {
            "dtm": _tokenize_path(Path(dtm_path), base),
            "ortho": _tokenize_path(Path(ortho_path), base),
            "srs": srs,
        },
    }

    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path

def load_project(path: Path) -> Dict[str, Any]:
    """Load a project file and expand path tokens.

    Returns a dictionary representing the JSON content with ``dtm``, ``ortho``
    and ``terrain_path`` fields expanded to absolute paths.
    """
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    base = path.parent

    layers = data.get("layers", {})
    for k in ["dtm", "ortho"]:
        if k in layers:
            layers[k] = _expand_path(layers[k], base)
    bundle = data.get("bundle", {})
    if "terrain_path" in bundle:
        bundle["terrain_path"] = _expand_path(bundle["terrain_path"], base)
    return data


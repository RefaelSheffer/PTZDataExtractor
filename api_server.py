# api_server.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# FastAPI skeleton exposing: upload model & prepare bundle, project pixel->world.

from typing import Optional, Dict, Any
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import JSONResponse
import uvicorn

from camera_models import save_bundle, load_bundle, list_bundles
from geom3d import CameraIntrinsics, CameraPose, camera_ray_in_world, intersect_ray_with_plane, intersect_ray_with_dtm, GeoRef
from dtm import DTM

app = FastAPI(title="Image→Ground API")

MODELS_DIR = Path.cwd() / "models"
MODELS_DIR.mkdir(exist_ok=True)

@app.post("/upload_model")
async def upload_model(file: UploadFile = File(...)) -> Dict[str, str]:
    dst = MODELS_DIR / file.filename
    with open(dst, "wb") as f:
        f.write(await file.read())
    return {"path": str(dst)}

@app.post("/prepare_bundle")
async def prepare_bundle(
    name: str = Form(...),
    model_path: str = Form(...),
    width: int = Form(...),
    height: int = Form(...),
    hfov_deg: float = Form(...),
    x: float = Form(...),
    y: float = Form(...),
    z: float = Form(...),
    yaw_deg: float = Form(...),
    pitch_deg: float = Form(...),
    roll_deg: float = Form(...),
    up_axis: str = Form("Z-up"),
    scale_to_m: float = Form(1.0),
    origin_lat: float = Form(...),
    origin_lon: float = Form(...),
    origin_alt: float = Form(0.0),
    yaw_site_deg: float = Form(0.0),
    projected_epsg: int = Form(None),
):
    intr = CameraIntrinsics.from_fov(width, height, hfov_deg)
    pose = CameraPose(x, y, z, yaw_deg, pitch_deg, roll_deg)
    meta = {"up_axis": up_axis, "scale_to_m": scale_to_m}
    georef = {"origin_lat": origin_lat, "origin_lon": origin_lon, "origin_alt": origin_alt,
              "yaw_site_deg": yaw_site_deg, "projected_epsg": projected_epsg}
    out = save_bundle(name, intr, pose, model_path, meta, georef)
    return {"bundle": name, "path": out}

@app.post("/project_pixel")
async def project_pixel(
    bundle: str = Form(...),
    u: float = Form(...),
    v: float = Form(...),
):
    intr, pose, mesh_path, meta, georef_dict = load_bundle(bundle)
    o, d = camera_ray_in_world(u, v, intr, pose)
    # Try DTM if bundle has terrain path
    pt = None
    try:
        dtm = DTM(mesh_path)
        pt = intersect_ray_with_dtm(o, d, dtm, GeoRef.from_dict(georef_dict) if georef_dict else None)
        dtm.close()
    except Exception:
        pass
    if pt is None:
        pt = intersect_ray_with_plane(o, d, 0.0)
    if pt is None:
        return JSONResponse({"ok": False, "message": "No intersection"}, status_code=400)
    out = {"ok": True, "world_local": {"x": float(pt[0]), "y": float(pt[1]), "z": float(pt[2])}}
    try:
        if georef_dict:
            gr = GeoRef.from_dict(georef_dict)
            geo = gr.local_to_geographic(pt)
            out.update(geo)
    except Exception as e:
        out["warning"] = f"geo transform failed: {e}"
    return out

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)

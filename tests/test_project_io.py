import json
from pathlib import Path

from camera_models import save_bundle
from geom3d import CameraIntrinsics, CameraPose

from project_io import export_project, load_project


def test_export_and_load_project(tmp_path, monkeypatch):
    # Prepare fake profiles.json
    profiles_path = tmp_path / "profiles.json"
    profiles = [{"name": "cam1", "rtsp": {"url": "rtsp://example"}}]
    profiles_path.write_text(json.dumps(profiles), encoding="utf-8")

    # Prepare bundle in temporary calibration directory
    cal_dir = tmp_path / "calibrations"
    cal_dir.mkdir()
    monkeypatch.setattr("camera_models.CALIB_DIR", cal_dir)

    intr = CameraIntrinsics(1920, 1080, 1000.0, 1000.0, 960.0, 540.0)
    pose = CameraPose(1.0, 2.0, 3.0, 4.0, 5.0, 6.0)
    save_bundle("b1", intr, pose, str(tmp_path / "mesh.obj"))

    # Dummy layer files
    dtm = tmp_path / "dtm.tif"; dtm.write_text("dtm")
    ortho = tmp_path / "ortho.tif"; ortho.write_text("ortho")

    project_path = tmp_path / "scene.rtgproj"
    export_project(project_path, "cam1", "b1", str(dtm), str(ortho), profiles_path=profiles_path)

    data = load_project(project_path)
    assert data["camera"]["name"] == "cam1"
    assert data["bundle"]["name"] == "b1"
    assert Path(data["layers"]["dtm"]) == dtm
    assert Path(data["bundle"]["terrain_path"]).name == "mesh.obj"

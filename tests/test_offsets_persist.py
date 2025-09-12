import json
import types

import ui_img2ground_module as mod
from app_state import app_state
import camera_models as cm
from geom3d import CameraIntrinsics, CameraPose


def test_persist_ctx_offsets_writes_profile(tmp_path, monkeypatch):
    profiles_path = tmp_path / "profiles.json"
    profiles_path.write_text('[{"name": "cam1"}]', encoding="utf-8")
    monkeypatch.setattr(mod, "PROFILES_PATH", profiles_path)

    proj = types.SimpleNamespace(offset_for_camera={})
    app_state.project = proj

    ctx = types.SimpleNamespace(
        alias="cam1", yaw_offset_deg=1.2, pitch_offset_deg=3.4, roll_offset_deg=5.6
    )

    mod.Img2GroundModule._persist_ctx_offsets(object(), ctx)

    data = json.loads(profiles_path.read_text(encoding="utf-8"))
    assert data[0]["yaw_offset_deg"] == 1.2
    assert data[0]["pitch_offset_deg"] == 3.4
    assert data[0]["roll_offset_deg"] == 5.6
    assert proj.offset_for_camera["cam1"]["yaw_offset_deg"] == 1.2

    app_state.project = None


def test_persist_ctx_offsets_writes_bundle(tmp_path):
    bundle_path = tmp_path / "b1.json"
    bundle_path.write_text(
        json.dumps(
            {
                "name": "b1",
                "intrinsics": {},
                "pose": {},
                "terrain_path": "mesh.obj",
            }
        ),
        encoding="utf-8",
    )
    ctx = types.SimpleNamespace(
        alias="cam1", yaw_offset_deg=1.2, pitch_offset_deg=3.4, roll_offset_deg=5.6
    )
    obj = types.SimpleNamespace(_bundle_path=bundle_path)

    mod.Img2GroundModule._persist_ctx_offsets(obj, ctx)

    data = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert data["yaw_offset_deg"] == 1.2
    assert data["pitch_offset_deg"] == 3.4
    assert data["roll_offset_deg"] == 5.6


def test_load_bundle_reads_offsets(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "CALIB_DIR", tmp_path)
    intr = CameraIntrinsics(1920, 1080, 1000.0, 1000.0, 960.0, 540.0)
    pose = CameraPose(1.0, 2.0, 3.0, 4.0, 5.0, 6.0)
    cm.save_bundle(
        "b1",
        intr,
        pose,
        str(tmp_path / "mesh.obj"),
        yaw_offset_deg=1.2,
        roll_offset_deg=5.6,
        pitch_offset_deg=3.4,
    )

    intr2, pose2, terrain, meta, georef = cm.load_bundle("b1")
    assert meta["yaw_offset_deg"] == 1.2
    assert meta["pitch_offset_deg"] == 3.4
    assert meta["roll_offset_deg"] == 5.6

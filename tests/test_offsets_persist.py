import json
import types

import ui_img2ground_module as mod
from app_state import app_state


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

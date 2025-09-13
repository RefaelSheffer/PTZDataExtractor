from __future__ import annotations

def parse_cgi_status(text: str):
    """Parse Dahua CGI status text into a dict.

    Returns a dict with pan/tilt/zoom values and a raw mapping of all keys.
    Accepts various key spellings such as Position/Positon/AbsPosition and
    ZoomValue/ZoomMapValue.
    """
    raw: dict[str, str] = {}
    for part in text.replace("&", "\n").splitlines():
        line = part.strip()
        if not line or "=" not in line:
            continue
        k, v = line.split("=", 1)
        raw[k.strip()] = v.strip()

    def pick_float(keys):
        for k in keys:
            if k in raw:
                try:
                    return float(raw[k])
                except Exception:
                    pass
        return None

    pan = pick_float(["status.Position[0]", "status.Positon[0]", "status.AbsPosition[0]", "pan"])
    tilt = pick_float(["status.Position[1]", "status.Positon[1]", "status.AbsPosition[1]", "tilt"])
    zoom = pick_float([
        "status.ZoomValue",
        "status.Position[2]",
        "status.Positon[2]",
        "status.AbsPosition[2]",
        "status.ZoomMapValue",
        "zoom",
    ])
    focus = pick_float(["status.FocusValue", "focus"])
    return {"pan": pan, "tilt": tilt, "zoom": zoom, "focus": focus, "raw": raw}

# camera_io.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Backend for ONVIF/RTSP app: camera IO, recording, probing, MediaMTX/FFmpeg push
# No UI elements here. Safe to import from a PySide6 GUI.

import os, platform, subprocess, time, socket, threading, textwrap, re
from pathlib import Path
from typing import Optional, Tuple, List

from PySide6 import QtCore

try:
    from onvif import ONVIFCamera  # optional
except Exception:
    ONVIFCamera = None


# ------------------- Utils -------------------
def which(cmd: str) -> Optional[str]:
    exts = [''] if platform.system() != 'Windows' else os.environ.get('PATHEXT','').split(';')
    for path in os.environ.get('PATH','').split(os.pathsep):
        p = Path(path)
        if not p.exists(): 
            continue
        for name in (cmd, *(cmd + e for e in exts)):
            cand = p / name
            if cand.exists():
                return str(cand)
    return None


def port_is_free_tcp(addr: str, port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((addr, port))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def wait_listening(host: str, port: int, timeout_s: float = 8.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.35):
                return True
        except Exception:
            time.sleep(0.2)
    return False


def find_free_rtsp_port(start_port: int = 8554, max_tries: int = 10) -> int:
    port = start_port
    for _ in range(max_tries):
        if port_is_free_tcp("127.0.0.1", port):
            return port
        port += 1
    return port


def ensure_mediamtx_config(mediamtx_exe: str, rtsp_port: int) -> Path:
    exe_path = Path(mediamtx_exe).resolve()
    cfg = exe_path.parent / "mediamtx.yml"
    content = textwrap.dedent(f"""# auto-generated
rtspEncryption: "no"
rtspAddress: 127.0.0.1:{rtsp_port}
rtspTransports: [tcp]
paths:
  all:
    source: publisher
""")
    try:
        if (not cfg.exists()) or (cfg.read_text(encoding="utf-8", errors="ignore") != content):
            cfg.write_text(content, encoding="utf-8")
    except Exception:
        pass
    return cfg


def kill_existing_mediamtx():
    """Try to terminate any leftover MediaMTX processes quietly."""
    try:
        if platform.system() == "Windows":
            subprocess.run(
                ["taskkill", "/F", "/IM", "mediamtx.exe"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            # Best effort; ignore return codes if process not found
            subprocess.run(
                ["pkill", "-f", "mediamtx"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except Exception:
        pass


# ------------------- Base process -------------------
class BaseProc(QtCore.QObject):
    log = QtCore.Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._stop_reader = False

    def _start_reader(self):
        if not self.proc or (self.proc.stdout is None):
            return
        self._stop_reader = False

        def run():
            try:
                for line in self.proc.stdout:
                    if self._stop_reader:
                        break
                    self.log.emit(line.rstrip())
            except Exception:
                pass

        self._reader = threading.Thread(target=run, daemon=True)
        self._reader.start()

    def stop(self):
        if self.proc is not None:
            try:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except Exception:
                    self.proc.kill()
            except Exception:
                pass
            self.proc = None
        self._stop_reader = True
        self.log.emit("process stopped")


class BaseRtspServer(BaseProc):
    started = QtCore.Signal(str)
    failed  = QtCore.Signal(str)
    stopped = QtCore.Signal()
    def stop(self):
        super().stop()
        self.stopped.emit()


# ------------------- MediaMTX + FFmpeg push -------------------
class MediaMtxServer(BaseRtspServer):
    def __init__(self, mediamtx_path: str, parent=None):
        super().__init__(parent)
        self.mediamtx_path = mediamtx_path
        self.port = 8554

    def start(self, desired_port: int = 8554) -> Tuple[bool, int]:
        self.stop()
        for attempt in range(3):
            kill_existing_mediamtx()
            self.port = find_free_rtsp_port(desired_port + attempt)
            ensure_mediamtx_config(self.mediamtx_path, self.port)
            try:
                self.proc = subprocess.Popen(
                    [self.mediamtx_path],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    cwd=str(Path(self.mediamtx_path).parent),
                    creationflags=(
                        getattr(subprocess, "CREATE_NO_WINDOW", 0)
                        if platform.system() == "Windows"
                        else 0
                    ),
                )
                self.log.emit("Launching MediaMTX:")
                self.log.emit(self.mediamtx_path)
                self._start_reader()
            except Exception as e:
                self.failed.emit(f"Failed to start MediaMTX: {e}")
                return False, self.port

            if wait_listening("127.0.0.1", self.port, timeout_s=8.0):
                self.log.emit(f"MediaMTX listening on 127.0.0.1:{self.port}")
                self.started.emit(f"rtsp://127.0.0.1:{self.port}/")
                return True, self.port
            self.stop()

        self.failed.emit("MediaMTX didn't open RTSP port (Firewall/AV/VPN?)")
        return False, self.port


class PushStreamer(BaseProc):
    started = QtCore.Signal(str)
    failed  = QtCore.Signal(str)
    stopped = QtCore.Signal()

    def __init__(self, ffmpeg_path: str, parent=None):
        super().__init__(parent)
        self.ffmpeg_path = ffmpeg_path

    def start(self, file_path: str, url: str) -> bool:
        self.stop()
        cmd = [
            self.ffmpeg_path, "-hide_banner", "-loglevel", "info",
            "-re", "-stream_loop", "-1", "-fflags", "+genpts", "-i", file_path,
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-tune", "zerolatency",
            "-g", "50", "-keyint_min", "50", "-x264-params", "scenecut=0:open_gop=0:bframes=0:ref=1",
            "-an", "-f", "rtsp", "-rtsp_transport", "tcp", url
        ]
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if platform.system()=="Windows" else 0)
            )
            self.log.emit("Launching FFmpeg push:")
            self.log.emit(" ".join(cmd))
            self._start_reader()
            self.started.emit(url)
            return True
        except Exception as e:
            self.failed.emit(f"Failed to start FFmpeg push: {e}")
            return False

    def stop(self):
        super().stop()
        self.stopped.emit()


# ------------------- FFprobe helpers -------------------
def ffprobe_supports(ffprobe_path: str, token: str) -> bool:
    try:
        out = subprocess.run([ffprobe_path, "-hide_banner", "-h", "protocol=rtsp"],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=5)
        return token in out.stdout
    except Exception:
        return False


def build_probe_cmd(ffprobe_path: str, url: str, prefer_tcp: bool=True, timeout_ms: int=4000) -> List[str]:
    args = [ffprobe_path, "-hide_banner", "-loglevel", "error"]
    if prefer_tcp:
        args += ["-rtsp_transport","tcp"]
    if ffprobe_supports(ffprobe_path, "rw_timeout"):
        args += ["-rw_timeout", str(timeout_ms*1000)]
    elif ffprobe_supports(ffprobe_path, "stimeout"):
        args += ["-stimeout", str(timeout_ms*1000)]
    args += ["-select_streams","v","-show_entries","stream=codec_name","-of","default=nk=1:nw=1", url]
    return args


def probe_rtsp(ffprobe_path: str, url: str, user: str="", pwd: str="", prefer_tcp: bool=True, timeout_ms: int=4000) -> Tuple[bool,str]:
    if user and "://" in url and "@" not in url:
        sch, rest = url.split("://",1)
        url = f"{sch}://{user}:{pwd}@{rest}"
    try:
        cmd = build_probe_cmd(ffprobe_path, url, prefer_tcp, timeout_ms)
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=(timeout_ms/1000+4))
        out = (r.stdout or "").strip()
        if r.returncode==0 and out:
            return True, f"OK ({out})"
        low = out.lower()
        if "401" in out or "unauthorized" in low: return False, "401 Unauthorized (משתמש/סיסמה?)"
        if "403" in out or "forbidden" in low:   return False, "403 Forbidden (הרשאות/מדיניות/ACL)"
        if "404" in out or "not found" in low:   return False, "404 Not Found (נתיב RTSP?)"
        if "timed out" in low or "timeout" in low: return False, "Timeout (פורט פתוח? חומת אש?)"
        return False, out or "ffprobe failed"
    except subprocess.TimeoutExpired:
        return False, "Timeout"
    except FileNotFoundError:
        return False, "ffprobe not found"
    except Exception as e:
        return False, str(e)


# ------------------- Helpers -------------------
def sanitize_host(host: str) -> Tuple[str, Optional[int], Optional[str]]:
    host = host.strip()
    if host.startswith("rtsp://"):
        try:
            m = re.match(r"rtsp://(?:[^@/]+@)?([^/:]+)(?::(\d+))?(/.*)?", host)
            if m:
                h = m.group(1)
                p = int(m.group(2)) if m.group(2) else None
                path = m.group(3) or "/"
                return h, p, path
        except Exception:
            pass
    return host, None, None


def parse_host_from_rtsp(url: str) -> str:
    try:
        m = re.match(r"rtsp://(?:[^@/]+@)?([^/:]+)", url)
        return m.group(1) if m else "camera"
    except Exception:
        return "camera"


# ------------------- Recorder -------------------
class RecorderProc(BaseProc):
    started = QtCore.Signal(str)   # dst path
    stopped = QtCore.Signal(str)   # dst path
    failed  = QtCore.Signal(str)   # message

    def __init__(self, parent=None):
        super().__init__(parent)
        self.dst: Optional[Path] = None

    def is_active(self) -> bool:
        return self.proc is not None

    def start_record(self, ffmpeg_path: str, url_with_auth: str, dst_path: Path, force_tcp: bool, fmt: str):
        self.stop()
        self.dst = dst_path
        dst_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = [ffmpeg_path, "-hide_banner", "-loglevel", "info"]
        if force_tcp:
            cmd += ["-rtsp_transport","tcp"]
        cmd += ["-i", url_with_auth, "-c", "copy"]

        fmt = (fmt or "mp4").lower()
        if fmt == "mp4":
            # fragmented mp4 — מאפשר פתיחה בזמן הקלטה
            cmd += ["-movflags", "+faststart+frag_keyframe+empty_moov", "-f", "mp4", str(dst_path)]
        elif fmt == "mkv":
            cmd += ["-f", "matroska", str(dst_path)]
        else:  # ts
            cmd += ["-f", "mpegts", str(dst_path)]

        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if platform.system()=="Windows" else 0)
            )
            self._start_reader()
            self.log.emit("Launching FFmpeg recorder:")
            self.log.emit(" ".join(cmd))
            self.started.emit(str(dst_path))
        except Exception as e:
            self.failed.emit(f"Failed to start recorder: {e}")

    def stop(self):
        dst = str(self.dst) if self.dst else ""
        super().stop()
        if dst:
            self.stopped.emit(dst)


# ------------------- ONVIF helper -------------------
def onvif_get_rtsp_uri(host: str, onvif_port: int, user: str, pwd: str) -> Tuple[bool, str]:
    """Return (ok, uri_or_error)."""
    if ONVIFCamera is None:
        return False, "ONVIF missing (pip install onvif-zeep)"
    try:
        cam = ONVIFCamera(host, int(onvif_port), user, pwd)
        media = cam.create_media_service()
        profiles = media.GetProfiles()
        if not profiles:
            return False, "No ONVIF profiles"
        prof = profiles[0]
        params = media.create_type('GetStreamUri')
        params.StreamSetup = {'Stream':'RTP-Unicast','Transport':{'Protocol':'RTSP'}}
        params.ProfileToken = prof.token
        uri = media.GetStreamUri(params).Uri
        return True, uri
    except Exception as e:
        return False, str(e)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ONVIF/RTSP — Simple App v6 (secure + retry, full)
#
# Adds:
# • Global FFmpeg/FFprobe path pickers (saved to app_config.json)
# • Stubborn Auto-retry for Preview (VLC) using main-thread timers
# • Stubborn Auto-retry for Recording (FFmpeg) in QThread
# • Tunables: Probe timeout (ms), Network caching (ms), Max retries
# • Redacted logging (no passwords / no raw IPs in logs)
#
# Keeps all v5 features:
# • Mock (MediaMTX+FFmpeg push), Real (RTSP/ONVIF), Dahua auto-try
# • Profiles load/save/update/delete
# • Quick check, Open in VLC, Recording mp4/mkv/ts (copy)
# • Metrics & logs, Export logs

import sys, os, platform, subprocess, time, socket, threading, textwrap, re, json, datetime, signal
from pathlib import Path
from typing import Optional, Tuple, List

from PySide6 import QtCore, QtGui, QtWidgets
try:
    import vlc
except Exception:
    vlc = None

try:
    from onvif import ONVIFCamera
except Exception:
    ONVIFCamera = None

APP_DIR = Path(__file__).resolve().parent
PROFILES_PATH = APP_DIR / "profiles.json"
APP_CFG = APP_DIR / "app_config.json"

# ------------------- Redaction (privacy) -------------------
def redact(text: str) -> str:
    """Mask credentials and IPv4 addresses in any log line."""
    if not text:
        return text
    # Mask user:pass in rtsp url
    text = re.sub(r'rtsp://([^:@/\s]+):([^@/\s]+)@', r'rtsp://[USER]:[***]@', text)
    # Mask IPv4 addresses
    text = re.sub(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', '[IP]', text)
    return text

# ------------------- Utils -------------------
def which(cmd: str) -> Optional[str]:
    exts = [''] if platform.system() != 'Windows' else os.environ.get('PATHEXT','').split(';')
    for path in os.environ.get('PATH','').split(os.pathsep):
        p = Path(path)
        if not p.exists(): continue
        for name in (cmd, *(cmd + e for e in exts)):
            cand = p / name
            if cand.exists():
                return str(cand)
    return None

def load_cfg() -> dict:
    if APP_CFG.exists():
        try:
            return json.loads(APP_CFG.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def save_cfg(cfg: dict):
    try:
        APP_CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

def default_vlc_path() -> str:
    if platform.system() == 'Windows':
        p = r"C:\Program Files\VideoLAN\VLC\vlc.exe"
        return p if Path(p).exists() else "vlc"
    return "vlc"

def open_folder(folder: Path):
    try:
        if platform.system()=="Windows":
            os.startfile(str(folder))
        elif platform.system()=="Darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
    except Exception:
        pass

def port_is_free_tcp(addr: str, port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((addr, port)); return True
    except OSError:
        return False
    finally:
        try: s.close()
        except: pass

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
    if platform.system() == "Windows":
        try:
            subprocess.run(["taskkill", "/F", "/IM", "mediamtx.exe"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        if not self.proc or (self.proc.stdout is None): return
        self._stop_reader = False
        def run():
            try:
                for line in self.proc.stdout:
                    if self._stop_reader: break
                    self.log.emit(redact(line.rstrip()))
            except Exception: pass
        self._reader = threading.Thread(target=run, daemon=True)
        self._reader.start()

    def stop(self):
        if self.proc is not None:
            try:
                if platform.system() == "Windows":
                    self.proc.terminate()
                else:
                    self.proc.terminate()
                try: self.proc.wait(timeout=3)
                except Exception: self.proc.kill()
            except Exception: pass
            self.proc = None
        self._stop_reader = True
        self.log.emit("process stopped")

class BaseRtspServer(BaseProc):
    started = QtCore.Signal(str); failed  = QtCore.Signal(str); stopped = QtCore.Signal()
    def stop(self): super().stop(); self.stopped.emit()

# ------------------- MediaMTX + FFmpeg push -------------------
class MediaMtxServer(BaseRtspServer):
    def __init__(self, mediamtx_path: str, parent=None):
        super().__init__(parent); self.mediamtx_path = mediamtx_path; self.port = 8554

    def start(self, desired_port: int = 8554) -> Tuple[bool, int]:
        self.stop(); kill_existing_mediamtx()
        for attempt in range(3):
            self.port = find_free_rtsp_port(desired_port + attempt, max_tries=1)
            ensure_mediamtx_config(self.mediamtx_path, self.port)
            try:
                self.proc = subprocess.Popen(
                    [self.mediamtx_path],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    cwd=str(Path(self.mediamtx_path).parent),
                    creationflags=(subprocess.CREATE_NO_WINDOW if platform.system()=="Windows" else 0)
                )
                self.log.emit("Launching MediaMTX:"); self.log.emit(self.mediamtx_path)
                self._start_reader()
            except Exception as e:
                self.failed.emit(f"Failed to start MediaMTX: {e}"); return False, self.port
            if wait_listening("127.0.0.1", self.port, timeout_s=8.0):
                self.log.emit(f"MediaMTX listening on 127.0.0.1:{self.port}")
                self.started.emit(f"rtsp://127.0.0.1:{self.port}/"); return True, self.port
            self.stop()
        self.failed.emit("MediaMTX didn't open RTSP port (Firewall/AV/VPN?)"); return False, self.port

class PushStreamer(BaseProc):
    started = QtCore.Signal(str); failed = QtCore.Signal(str); stopped = QtCore.Signal()
    def __init__(self, ffmpeg_path: str, parent=None):
        super().__init__(parent); self.ffmpeg_path = ffmpeg_path
    def start(self, file_path: str, url: str) -> bool:
        self.stop()
        cmd = [self.ffmpeg_path,"-hide_banner","-loglevel","info","-re","-stream_loop","-1","-fflags","+genpts","-i",file_path,
               "-c:v","libx264","-pix_fmt","yuv420p","-preset","veryfast","-tune","zerolatency","-g","50","-keyint_min","50",
               "-x264-params","scenecut=0:open_gop=0:bframes=0:ref=1","-an","-f","rtsp","-rtsp_transport","tcp",url]
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                         creationflags=(subprocess.CREATE_NO_WINDOW if platform.system()=="Windows" else 0))
            self.log.emit("Launching FFmpeg push:")
            self.log.emit(redact(" ".join(cmd)))
            self._start_reader(); self.started.emit(url); return True
        except Exception as e:
            self.failed.emit(f"Failed to start FFmpeg push: {e}"); return False
    def stop(self): super().stop(); self.stopped.emit()

# ------------------- FFprobe helpers -------------------
def ffprobe_supports(ffprobe_path: str, token: str) -> bool:
    try:
        out = subprocess.run([ffprobe_path, "-hide_banner", "-h", "protocol=rtsp"],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=5)
        return token in (out.stdout or "")
    except Exception: return False

def build_probe_cmd(ffprobe_path: str, url: str, prefer_tcp: bool=True, timeout_ms: int=4000) -> List[str]:
    args = [ffprobe_path, "-hide_banner", "-loglevel", "error"]
    if prefer_tcp: args += ["-rtsp_transport","tcp"]
    to_us = max(1_000_000, int(timeout_ms)*1000)  # microseconds
    if ffprobe_supports(ffprobe_path, "rw_timeout"):
        args += ["-rw_timeout", str(to_us)]
    elif ffprobe_supports(ffprobe_path, "stimeout"):
        args += ["-stimeout", str(to_us)]
    args += ["-select_streams","v","-show_entries","stream=codec_name","-of","default=nk=1:nw=1", url]
    return args

def probe_rtsp(ffprobe_path: str, url: str, user: str="", pwd: str="", prefer_tcp: bool=True, timeout_ms: int=4000) -> Tuple[bool,str]:
    if user and "://" in url and "@" not in url:
        sch, rest = url.split("://",1); url = f"{sch}://{user}:{pwd}@{rest}"
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

# ------------------- VLC surface -------------------
class VlcVideoWidget(QtWidgets.QFrame):
    def __init__(self, instance: vlc.Instance):
        super().__init__()
        self.setAttribute(QtCore.Qt.WA_DontCreateNativeAncestors, True)
        self.setAttribute(QtCore.Qt.WA_NativeWindow, True)
        self.setStyleSheet("background:#111; border:1px solid #333;")
        self._instance = instance
        self._player = self._instance.media_player_new()
    def player(self) -> 'vlc.MediaPlayer': return self._player
    def ensure_video_out(self):
        wid = int(self.winId())
        if platform.system()=='Windows': self._player.set_hwnd(wid)
        elif platform.system()=='Darwin': self._player.set_nsobject(wid)
        else: self._player.set_xwindow(wid)
    def showEvent(self, e): super().showEvent(e); self.ensure_video_out()
    def resizeEvent(self, e): super().resizeEvent(e); self.ensure_video_out()

# ------------------- Helpers -------------------
def sanitize_host(host: str) -> Tuple[str, Optional[int], Optional[str]]:
    host = host.strip()
    if host.startswith("rtsp://"):
        try:
            m = re.match(r"rtsp://(?:[^@/]+@)?([^/:]+)(?::(\d+))?(/.*)?", host)
            if m:
                h = m.group(1); p = int(m.group(2)) if m.group(2) else None; path = m.group(3) or "/"
                return h, p, path
        except Exception: pass
    return host, None, None

def parse_host_from_rtsp(url: str) -> str:
    try:
        m = re.match(r"rtsp://(?:[^@/]+@)?([^/:]+)", url)
        return m.group(1) if m else "camera"
    except Exception:
        return "camera"

# ------------------- Recording thread (auto-retry) -------------------
class RecorderThread(QtCore.QThread):
    log = QtCore.Signal(str)
    finished_ok = QtCore.Signal()
    exited_with_err = QtCore.Signal(int)

    def __init__(self, ffmpeg_path: str, url: str, out_path: Path,
                 force_tcp: bool = True, probe_timeout_ms: int = 7000,
                 max_retries: int = 0, backoff_cap_s: int = 30, parent=None):
        super().__init__(parent)
        self.ffmpeg_path = ffmpeg_path
        self.url = url
        self.out_path = out_path
        self.force_tcp = force_tcp
        self.probe_timeout_ms = probe_timeout_ms
        self.max_retries = max_retries  # 0 => infinite
        self.backoff_cap_s = backoff_cap_s
        self._stop = threading.Event()
        self._proc = None

    def stop(self):
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            try:
                if os.name == "nt":
                    self._proc.send_signal(signal.CTRL_BREAK_EVENT)
                    time.sleep(0.2)
                self._proc.terminate()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=2.5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass

    def _ffmpeg_supports(self, token: str) -> bool:
        try:
            p = subprocess.run([self.ffmpeg_path, "-hide_banner", "-h", "protocol=rtsp"],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=5)
            out = (p.stdout or "")
            return token in out
        except Exception:
            return False

    def _build_cmd(self) -> list[str]:
        cmd = [self.ffmpeg_path, "-hide_banner", "-loglevel", "warning"]
        if self.force_tcp:
            cmd += ["-rtsp_transport", "tcp"]

        to_us = max(1_000_000, int(self.probe_timeout_ms) * 1000)
        if self._ffmpeg_supports("rw_timeout"):
            cmd += ["-rw_timeout", str(to_us)]
        elif self._ffmpeg_supports("stimeout"):
            cmd += ["-stimeout", str(to_us)]

        cmd += ["-i", self.url, "-c", "copy"]
        suf = self.out_path.suffix.lower()
        if suf == ".mp4":
            cmd += ["-movflags", "+faststart+frag_keyframe+empty_moov"]
        if suf == ".mkv":
            cmd += ["-f", "matroska"]
        elif suf == ".ts":
            cmd += ["-f", "mpegts"]
        cmd += [str(self.out_path)]
        return cmd

    def run(self):
        tries = 0
        self.out_path.parent.mkdir(parents=True, exist_ok=True)

        while not self._stop.is_set():
            tries += 1
            cmd = self._build_cmd()
            self.log.emit(redact(f"[rec] starting ffmpeg (try {tries}): {' '.join(cmd)}"))

            try:
                self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            except FileNotFoundError:
                self.log.emit("[rec] ffmpeg not found. Set correct path.")
                self.exited_with_err.emit(-1)
                return
            except Exception as e:
                self.log.emit(f"[rec] failed to start ffmpeg: {e}")
                self.exited_with_err.emit(-2)
                return

            while not self._stop.is_set():
                line = self._proc.stdout.readline() if self._proc.stdout else ""
                if line:
                    self.log.emit(redact("[ffmpeg] " + line.strip()))
                if self._proc.poll() is not None:
                    break

            rc = self._proc.poll()
            if rc == 0:
                self.log.emit("[rec] ffmpeg exited ok")
                self.finished_ok.emit()
                return

            self.log.emit(f"[rec] ffmpeg exited with rc={rc}")
            self.exited_with_err.emit(rc)

            if self._stop.is_set():
                return
            if self.max_retries and tries >= self.max_retries:
                self.log.emit("[rec] reached max retries – giving up")
                return

            backoff = min(self.backoff_cap_s, 2 * tries)  # 2,4,6.. up to cap
            self.log.emit(f"[rec] retrying in {backoff}s…")
            for _ in range(backoff * 10):
                if self._stop.is_set():
                    return
                time.sleep(0.1)

# ------------------- Main Window -------------------
class MainWindow(QtWidgets.QMainWindow):
    # Signal used to schedule preview retry on the main thread
    request_preview_retry = QtCore.Signal(int)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("ONVIF/RTSP — Simple App v6 (secure+retry, full)"); self.resize(1300,900)

        self.cfg = load_cfg()

        # VLC instance
        if not vlc:
            QtWidgets.QMessageBox.critical(self, "VLC missing", "python-vlc not installed.")
            raise SystemExit(1)
        self.vlc_instance = vlc.Instance('--no-plugins-cache', '--vout=opengl')

        central = QtWidgets.QWidget(); self.setCentralWidget(central)
        vbox = QtWidgets.QVBoxLayout(central)

        # ---- Global Executables & Tunables ----
        exec_g = QtWidgets.QGroupBox("Executables & Tunables")
        eg = QtWidgets.QGridLayout(exec_g)

        self.ffmpeg_path = QtWidgets.QLineEdit(self.cfg.get("ffmpeg_path", which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"))
        self.ffprobe_path = QtWidgets.QLineEdit(self.cfg.get("ffprobe_path", which("ffprobe") or r"C:\ffmpeg\bin\ffprobe.exe"))
        btn_ffmpeg = QtWidgets.QPushButton("Browse…"); btn_ffmpeg.clicked.connect(lambda: self._browse_exe(self.ffmpeg_path, "Select ffmpeg"))
        btn_ffprobe = QtWidgets.QPushButton("Browse…"); btn_ffprobe.clicked.connect(lambda: self._browse_exe(self.ffprobe_path, "Select ffprobe"))

        self.network_caching_sp = QtWidgets.QSpinBox(); self.network_caching_sp.setRange(200, 10000)
        self.network_caching_sp.setValue(int(self.cfg.get("network_caching", 1000)))
        self.probe_timeout_sp = QtWidgets.QSpinBox(); self.probe_timeout_sp.setRange(1000, 60000)
        self.probe_timeout_sp.setValue(int(self.cfg.get("probe_timeout_ms", 7000)))

        self.auto_retry_preview_cb = QtWidgets.QCheckBox("Auto-retry Preview"); self.auto_retry_preview_cb.setChecked(bool(self.cfg.get("auto_retry_preview", True)))
        self.max_preview_retries_sp = QtWidgets.QSpinBox(); self.max_preview_retries_sp.setRange(0, 1000); self.max_preview_retries_sp.setValue(int(self.cfg.get("max_preview_retries", 0)))  # 0 = ∞

        self.auto_retry_rec_cb = QtWidgets.QCheckBox("Auto-retry Record"); self.auto_retry_rec_cb.setChecked(bool(self.cfg.get("auto_retry_record", True)))
        self.max_record_retries_sp = QtWidgets.QSpinBox(); self.max_record_retries_sp.setRange(0, 1000); self.max_record_retries_sp.setValue(int(self.cfg.get("max_record_retries", 0)))  # 0 = ∞

        r=0
        eg.addWidget(QtWidgets.QLabel("FFmpeg path:"), r,0); eg.addWidget(self.ffmpeg_path, r,1); eg.addWidget(btn_ffmpeg, r,2); r+=1
        eg.addWidget(QtWidgets.QLabel("FFprobe path:"), r,0); eg.addWidget(self.ffprobe_path, r,1); eg.addWidget(btn_ffprobe, r,2); r+=1
        eg.addWidget(QtWidgets.QLabel("Network caching (ms):"), r,0); eg.addWidget(self.network_caching_sp, r,1); r+=1
        eg.addWidget(QtWidgets.QLabel("Probe timeout (ms):"), r,0); eg.addWidget(self.probe_timeout_sp, r,1); r+=1
        eg.addWidget(self.auto_retry_preview_cb, r,0); eg.addWidget(QtWidgets.QLabel("Max preview retries (0=∞):"), r,1); eg.addWidget(self.max_preview_retries_sp, r,2); r+=1
        eg.addWidget(self.auto_retry_rec_cb, r,0); eg.addWidget(QtWidgets.QLabel("Max record retries (0=∞):"), r,1); eg.addWidget(self.max_record_retries_sp, r,2); r+=1
        vbox.addWidget(exec_g)

        # ---- Mode selector + stack ----
        self.mode = QtWidgets.QComboBox(); self.mode.addItems(["Mockup (local RTSP)", "Real camera (RTSP/ONVIF)"])
        vbox.addWidget(self.mode)
        self.stack = QtWidgets.QStackedWidget(); vbox.addWidget(self.stack,1)

        # ---------- Mock page ----------
        mock = QtWidgets.QWidget(); ml = QtWidgets.QGridLayout(mock)
        self.mediamtx_path = QtWidgets.QLineEdit(str(self.cfg.get("mediamtx_path", Path.cwd()/ "mediamtx.exe")))
        self.mock_file  = QtWidgets.QLineEdit(self.cfg.get("mock_file",""))
        btn_browse = QtWidgets.QPushButton("Browse MP4…"); btn_browse.clicked.connect(self._choose_mp4)
        self.mock_port  = QtWidgets.QSpinBox(); self.mock_port.setRange(1024,65535); self.mock_port.setValue(int(self.cfg.get("mock_port", 8554)))
        self.mock_mount = QtWidgets.QLineEdit(self.cfg.get("mock_mount","/cam"))
        self.auto_connect = QtWidgets.QCheckBox("Connect automatically"); self.auto_connect.setChecked(bool(self.cfg.get("mock_auto_connect", True)))
        self.btn_start_srv = QtWidgets.QPushButton("Start Mock Server")
        self.btn_stop_srv  = QtWidgets.QPushButton("Stop Mock Server"); self.btn_stop_srv.setEnabled(False)
        self.mock_url      = QtWidgets.QLineEdit(); self.mock_url.setReadOnly(True)
        self.btn_connect_mock = QtWidgets.QPushButton("Connect Preview")

        row=0
        ml.addWidget(QtWidgets.QLabel("MediaMTX path:"),row,0); ml.addWidget(self.mediamtx_path,row,1,1,3); row+=1
        ml.addWidget(QtWidgets.QLabel("MP4 file:"),row,0); ml.addWidget(self.mock_file,row,1,1,2); ml.addWidget(btn_browse,row,3); row+=1
        ml.addWidget(QtWidgets.QLabel("RTSP port:"),row,0); ml.addWidget(self.mock_port,row,1)
        ml.addWidget(QtWidgets.QLabel("Mount:"),row,2); ml.addWidget(self.mock_mount,row,3); row+=1
        ml.addWidget(self.auto_connect,row,0,1,4); row+=1
        ml.addWidget(self.btn_start_srv,row,0); ml.addWidget(self.btn_stop_srv,row,1)
        ml.addWidget(QtWidgets.QLabel("RTSP URL:"),row,2); ml.addWidget(self.mock_url,row,3); row+=1
        ml.addWidget(self.btn_connect_mock,row,0); row+=1
        self.stack.addWidget(mock)

        # ---------- Real page ----------
        real = QtWidgets.QWidget(); rl = QtWidgets.QGridLayout(real)

        # פרופילים
        self.profiles_combo = QtWidgets.QComboBox()
        self.profile_name   = QtWidgets.QLineEdit()
        self.btn_profile_load   = QtWidgets.QPushButton("Load")
        self.btn_profile_saveas = QtWidgets.QPushButton("Save as…")
        self.btn_profile_update = QtWidgets.QPushButton("Update")
        self.btn_profile_delete = QtWidgets.QPushButton("Delete")
        self._profiles = self._load_profiles()
        self._refresh_profiles_combo()

        # פרטי חיבור
        self.real_mode = QtWidgets.QComboBox(); self.real_mode.addItems(["RTSP", "ONVIF → RTSP"])
        self.host = QtWidgets.QLineEdit(self.cfg.get("host","192.168.1.100"))
        self.user = QtWidgets.QLineEdit(self.cfg.get("user",""))
        self.pwd  = QtWidgets.QLineEdit(self.cfg.get("pwd","")); self.pwd.setEchoMode(QtWidgets.QLineEdit.Password)
        self.rtsp_port = QtWidgets.QSpinBox(); self.rtsp_port.setRange(1,65535); self.rtsp_port.setValue(int(self.cfg.get("rtsp_port",554)))
        self.rtsp_path = QtWidgets.QLineEdit(self.cfg.get("rtsp_path","/cam/realmonitor?channel=1&subtype=1"))
        self.onvif_port = QtWidgets.QSpinBox(); self.onvif_port.setRange(1,65535); self.onvif_port.setValue(int(self.cfg.get("onvif_port",80)))
        self.force_tcp = QtWidgets.QCheckBox("Force RTSP over TCP (client)"); self.force_tcp.setChecked(bool(self.cfg.get("force_tcp", True)))

        # כפתורי חיבור וכלים
        self.btn_try_dahua = QtWidgets.QPushButton("Try Dahua (auto)")
        self.btn_connect_real = QtWidgets.QPushButton("Connect Camera")
        self.btn_netcheck = QtWidgets.QPushButton("Quick check")
        self.btn_open_ext = QtWidgets.QPushButton("Open in VLC")
        self.btn_stop_conn = QtWidgets.QPushButton("Stop Connection")

        # הקלטה
        self.rec_group = QtWidgets.QGroupBox("Recording (FFmpeg)")
        rec_layout = QtWidgets.QGridLayout(self.rec_group)
        self.chk_record_auto = QtWidgets.QCheckBox("Auto-start record on connect")
        self.chk_record_auto.setChecked(bool(self.cfg.get("record_auto", False)))
        self.rec_format = QtWidgets.QComboBox(); self.rec_format.addItems(["mp4","mkv","ts"])
        self.rec_path   = QtWidgets.QLineEdit(self.cfg.get("out_dir", str(Path.cwd() / "recordings")))
        self.btn_rec_start = QtWidgets.QPushButton("Start Record")
        self.btn_rec_stop  = QtWidgets.QPushButton("Stop Record"); self.btn_rec_stop.setEnabled(False)
        self.btn_rec_folder = QtWidgets.QPushButton("Open recordings folder")
        self.rec_indicator = QtWidgets.QLabel("REC: OFF"); self.rec_indicator.setStyleSheet("color:#aaa; font-weight:bold;")
        r=0
        rec_layout.addWidget(self.chk_record_auto, r,0,1,2); r+=1
        rec_layout.addWidget(QtWidgets.QLabel("Format:"), r,0); rec_layout.addWidget(self.rec_format, r,1); r+=1
        rec_layout.addWidget(QtWidgets.QLabel("Output (folder or file):"), r,0); rec_layout.addWidget(self.rec_path, r,1,1,2); r+=1
        rec_layout.addWidget(self.btn_rec_start, r,0); rec_layout.addWidget(self.btn_rec_stop, r,1); rec_layout.addWidget(self.btn_rec_folder, r,2); r+=1
        rec_layout.addWidget(self.rec_indicator, r,0); r+=1

        # סידור גריד
        row=0
        rl.addWidget(QtWidgets.QLabel("Profiles:"),row,0); rl.addWidget(self.profiles_combo,row,1)
        rl.addWidget(self.profile_name,row,2)
        rl.addWidget(self.btn_profile_load,row,3); row+=1
        rl.addWidget(self.btn_profile_saveas,row,0); rl.addWidget(self.btn_profile_update,row,1)
        rl.addWidget(self.btn_profile_delete,row,2); row+=1

        rl.addWidget(QtWidgets.QLabel("Mode:"),row,0); rl.addWidget(self.real_mode,row,1); row+=1
        rl.addWidget(QtWidgets.QLabel("Host/IP:"),row,0); rl.addWidget(self.host,row,1,1,3); row+=1
        rl.addWidget(QtWidgets.QLabel("Username:"),row,0); rl.addWidget(self.user,row,1,1,3); row+=1
        rl.addWidget(QtWidgets.QLabel("Password:"),row,0); rl.addWidget(self.pwd,row,1,1,3); row+=1
        rl.addWidget(QtWidgets.QLabel("RTSP port:"),row,0); rl.addWidget(self.rtsp_port,row,1)
        rl.addWidget(QtWidgets.QLabel("RTSP path:"),row,2); rl.addWidget(self.rtsp_path,row,3); row+=1
        rl.addWidget(QtWidgets.QLabel("ONVIF port:"),row,0); rl.addWidget(self.onvif_port,row,1)
        rl.addWidget(self.force_tcp,row,2,1,2); row+=1

        rl.addWidget(self.btn_try_dahua,row,0)
        rl.addWidget(self.btn_connect_real,row,1)
        rl.addWidget(self.btn_netcheck,row,2)
        rl.addWidget(self.btn_open_ext,row,3); rl.addWidget(self.btn_stop_conn,row,4); row+=1

        rl.addWidget(self.rec_group, row,0,1,5); row+=1

        self.stack.addWidget(real)

        # ---------- Video + controls ----------
        self.video = VlcVideoWidget(self.vlc_instance); vbox.addWidget(self.video,2)
        h = QtWidgets.QHBoxLayout()
        self.btn_stop_view = QtWidgets.QPushButton("Stop Player")
        self.btn_export_logs = QtWidgets.QPushButton("Export logs…")
        h.addWidget(self.btn_stop_view); h.addWidget(self.btn_export_logs)
        vbox.addLayout(h)

        # ---------- Metrics + logs ----------
        self.metrics = QtWidgets.QLabel("State: idle | 0x0 | FPS:? | in:? kbps | demux:? kbps"); vbox.addWidget(self.metrics)
        self.logs = QtWidgets.QPlainTextEdit(); self.logs.setReadOnly(True); self.logs.setMaximumBlockCount(10000); vbox.addWidget(self.logs)

        # ---------- Backends ----------
        self.mediamtx = MediaMtxServer(self.cfg.get("mediamtx_path", str(Path.cwd()/ "mediamtx.exe")))
        self.pusher   = PushStreamer(self.ffmpeg_path.text().strip())
        for srv in (self.mediamtx, self.pusher): srv.log.connect(self._log)
        self.mediamtx.started.connect(lambda _: self._log("MediaMTX started"))
        self.pusher.started.connect(lambda url: self._log("FFmpeg push started"))

        self._rec_thread: Optional[RecorderThread] = None

        # ---------- Signals ----------
        self.mode.currentIndexChanged.connect(self.stack.setCurrentIndex)
        self.btn_start_srv.clicked.connect(self._start_mock_server)
        self.btn_stop_srv .clicked.connect(self._stop_mock_server)
        self.btn_connect_mock.clicked.connect(self._connect_mock)

        self.btn_connect_real.clicked.connect(self._connect_real)
        self.btn_try_dahua.clicked.connect(self._auto_try_dahua)

        self.btn_netcheck.clicked.connect(self._quick_check)
        self.btn_open_ext.clicked.connect(self._open_external_vlc)

        
        self.btn_stop_conn.clicked.connect(self._stop_player)
self.btn_stop_view.clicked.connect(self._stop_player)
        self.btn_export_logs.clicked.connect(self._export_logs)

        self.btn_rec_start.clicked.connect(self._start_record)
        self.btn_rec_stop.clicked.connect(self._stop_record)
        self.btn_rec_folder.clicked.connect(lambda: open_folder(Path(self.rec_path.text().strip() or ".")))

        # פרופילים
        self.btn_profile_load.clicked.connect(self._profile_load_clicked)
        self.btn_profile_saveas.clicked.connect(self._profile_saveas_clicked)
        self.btn_profile_update.clicked.connect(self._profile_update_clicked)
        self.btn_profile_delete.clicked.connect(self._profile_delete_clicked)

        # ---------- Timer ----------
        self.t = QtCore.QTimer(self); self.t.timeout.connect(self._update_metrics); self.t.start(800)

        # ---------- VLC events & main-thread retry ----------
        ev = self.video.player().event_manager()
        ev.event_attach(vlc.EventType.MediaPlayerPlaying, lambda e: self._log("VLC: Playing"))
        ev.event_attach(vlc.EventType.MediaPlayerEncounteredError, self._on_vlc_error)
        ev.event_attach(vlc.EventType.MediaPlayerEndReached, self._on_vlc_end)
        ev.event_attach(vlc.EventType.MediaPlayerStopped, self._on_vlc_stopped)

        self._retry_timer = QtCore.QTimer(self); self._retry_timer.setInterval(3500)
        self._retry_timer.timeout.connect(self._maybe_retry_preview)
        self._last_url = ""; self._last_user = ""; self._last_pwd=""; self._last_force_tcp=True
        self._preview_retries = 0

        self.request_preview_retry.connect(self._schedule_retry_preview_main)

        # Restore a couple of fields
        if self.cfg.get("profiles_combo"):
            self.profiles_combo.setCurrentText(self.cfg["profiles_combo"])

    # ===== Mock =====
    def _choose_mp4(self):
        dialog = QtWidgets.QFileDialog(self, "Choose MP4")
        dialog.setNameFilters(["Video (*.mp4 *.mov *.mkv)","All files (*.*)"])
        dialog.setFileMode(QtWidgets.QFileDialog.ExistingFile)
        if not dialog.exec(): return
        files = dialog.selectedFiles()
        if files: self.mock_file.setText(files[0])

    def _start_mock_server(self):
        p = self.mock_file.text().strip()
        if not p:
            QtWidgets.QMessageBox.warning(self, "Missing file", "Please choose an MP4."); return

        med = self.mediamtx_path.text().strip()
        if not Path(med).exists():
            QtWidgets.QMessageBox.critical(self, "MediaMTX not found", "שים נתיב מלא ל-mediamtx.exe"); return
        self.mediamtx.mediamtx_path = med

        ok, chosen_port = self.mediamtx.start(int(self.mock_port.value()))
        if not ok:
            QtWidgets.QMessageBox.critical(self, "MediaMTX error", "MediaMTX לא מאזין (Firewall/AV/VPN?)"); return
        self.mock_port.setValue(chosen_port)
        url = f"rtsp://127.0.0.1:{chosen_port}{self.mock_mount.text().strip() or '/cam'}"
        self.mock_url.setText(url)

        ff = self.ffmpeg_path.text().strip()
        if (which(Path(ff).name) is None) and (not Path(ff).exists()):
            QtWidgets.QMessageBox.critical(self, "FFmpeg not found", "הגדר נתיב ל-ffmpeg.exe"); return
        self.pusher.ffmpeg_path = ff
        if not self.pusher.start(p, url):
            QtWidgets.QMessageBox.critical(self, "FFmpeg error", "FFmpeg push נכשל"); return

        if self.auto_connect.isChecked():
            QtCore.QTimer.singleShot(600, lambda: self._start_player(url))
        self.btn_start_srv.setEnabled(False); self.btn_stop_srv.setEnabled(True)
        self._log("Mock RTSP started")

    def _stop_mock_server(self):
        self.pusher.stop(); self.mediamtx.stop()
        self.btn_start_srv.setEnabled(True); self.btn_stop_srv.setEnabled(False)
        self._log("All mock servers stopped")

    def _connect_mock(self):
        url = self.mock_url.text().strip()
        if not url:
            QtWidgets.QMessageBox.warning(self, "No RTSP URL", "Start mock server first."); return
        self._start_player(url)

    # ===== Real =====
    def _auto_try_dahua(self):
        host, hp, pp = sanitize_host(self.host.text())
        if hp: self.rtsp_port.setValue(hp)
        if pp: self.rtsp_path.setText(pp)
        host = host.strip(); user = self.user.text().strip(); pwd = self.pwd.text().strip()
        ports = [self.rtsp_port.value()] + [p for p in (554,5544,8554) if p!=self.rtsp_port.value()]
        paths = [
            "/cam/realmonitor?channel={ch}&subtype={st}",
            "/Streaming/Channels/10{ch-1}",
            "/Streaming/Channels/101"
        ]
        chans = [1,2,3,4]; subs = [1,0]
        ffprobe = self.ffprobe_path.text().strip() or which("ffprobe") or "ffprobe"
        for port in ports:
            for ch in chans:
                for st in subs:
                    for pat in paths:
                        path = pat.replace("{ch-1}", str(ch-1)).format(ch=ch, st=st)
                        url = f"rtsp://{host}:{port}{path}"
                        ok, msg = probe_rtsp(ffprobe, url, user, pwd, prefer_tcp=True, timeout_ms=3500)
                        self._log(f"Probe → {msg}")
                        if ok:
                            self.rtsp_port.setValue(port); self.rtsp_path.setText(path)
                            self._start_player(url, force_tcp=True, user=user, pwd=pwd)
                            QtWidgets.QMessageBox.information(self,"Auto-try", f"Connected.")
                            return
        QtWidgets.QMessageBox.information(self,"Auto-try","No RTSP URL worked (check permissions/ONVIF/stream settings).")

    def _compose_rtsp_url(self) -> str:
        host_in = self.host.text().strip()
        h, hp, pp = sanitize_host(host_in)
        port = self.rtsp_port.value() if not hp else hp
        path = self.rtsp_path.text().strip() if not pp else pp
        if not path.startswith("/"): path = "/" + path
        return f"rtsp://{h}:{port}{path}"

    def _connect_real(self):
        mode = self.real_mode.currentText()
        host_in = self.host.text().strip()
        h, hp, pp = sanitize_host(host_in)
        if hp: self.rtsp_port.setValue(hp)
        if pp: self.rtsp_path.setText(pp)
        user = self.user.text().strip(); pwd = self.pwd.text().strip()

        if mode.startswith("RTSP"):
            url = self._compose_rtsp_url()
            ffprobe = self.ffprobe_path.text().strip() or which("ffprobe") or "ffprobe"
            ok, msg = probe_rtsp(ffprobe, url, user, pwd, prefer_tcp=self.force_tcp.isChecked(), timeout_ms=self.probe_timeout_sp.value())
            self._log(f"Probe → {msg}")
            # גם אם נכשל — ננסה VLC (ייתכן ש-ffprobe חסר/חסום)
            self._start_player(url, force_tcp=self.force_tcp.isChecked(), user=user, pwd=pwd)
            return

        if ONVIFCamera is None:
            QtWidgets.QMessageBox.critical(self, "ONVIF missing", "pip install onvif-zeep"); return
        try:
            cam = ONVIFCamera(h, int(self.onvif_port.value()), user, pwd)
            media = cam.create_media_service()
            prof = media.GetProfiles()[0]
            params = media.create_type('GetStreamUri')
            params.StreamSetup = {'Stream':'RTP-Unicast','Transport':{'Protocol':'RTSP'}}
            params.ProfileToken = prof.token
            uri = media.GetStreamUri(params).Uri
            self._log("ONVIF URI obtained")
            self._start_player(uri, force_tcp=self.force_tcp.isChecked(), user=user, pwd=pwd)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "ONVIF error", str(e))

    # ===== Quick tools =====
    def _quick_check(self):
        host_in = self.host.text().strip()
        h, _, _ = sanitize_host(host_in)
        onvif_port = int(self.onvif_port.value())
        rtsp_port  = int(self.rtsp_port.value())

        def port_ok(host, port):
            try:
                with socket.create_connection((host, port), timeout=1.8):
                    return True
            except Exception:
                return False

        ok_rtsp  = port_ok(h, rtsp_port)
        ok_onvif = port_ok(h, onvif_port)

        url_noauth = self._compose_rtsp_url()
        ffprobe = self.ffprobe_path.text().strip() or which("ffprobe") or "ffprobe"
        ok1, msg1 = probe_rtsp(ffprobe, url_noauth, self.user.text().strip(), self.pwd.text().strip(),
                               prefer_tcp=self.force_tcp.isChecked(), timeout_ms=self.probe_timeout_sp.value())

        txt = []
        txt.append(f"RTSP port {rtsp_port}: {'OPEN' if ok_rtsp else 'CLOSED'}")
        txt.append(f"ONVIF port {onvif_port}: {'OPEN' if ok_onvif else 'CLOSED'}")
        txt.append(f"ffprobe: {msg1}")
        QtWidgets.QMessageBox.information(self, "Quick check", "\n".join(txt))

    def _open_external_vlc(self):
        url = self._compose_rtsp_url()  # בלי קרדנציאלס
        exe = default_vlc_path()
        cmd = [exe, "--rtsp-tcp", url, f"--rtsp-user={self.user.text().strip()}", f"--rtsp-pwd={self.pwd.text().strip()}"]
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=(subprocess.CREATE_NO_WINDOW if platform.system()=="Windows" else 0))
            self._log("Opened external VLC")
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "External VLC", f"Failed to launch VLC:\n{e}")

    # ===== Player / Preview with main-thread Auto-retry =====
    def _start_player(self, url: str, force_tcp: bool=True, user: str="", pwd: str=""):
        try: self.video.player().stop()
        except Exception: pass

        self.video.ensure_video_out()
        opts = []
        if force_tcp:
            opts.append(':rtsp-tcp')
        nc = int(self.network_caching_sp.value())
        opts += [':avcodec-hw=none', f':network-caching={nc}']

        # השארת ה-URL ללא קרדנציאלס; העברת user/pwd כאופציות
        media = self.vlc_instance.media_new(url, *opts)
        if user:
            media.add_option(f":rtsp-user={user}")
        if pwd:
            media.add_option(f":rtsp-pwd={pwd}")

        self.video.player().set_media(media)
        self.video.player().play()
        self._log("Player started")

        self._last_url, self._last_user, self._last_pwd, self._last_force_tcp = url, user, pwd, force_tcp
        self._preview_retries = 0

        if self.auto_retry_preview_cb.isChecked():
            self._retry_timer.start()
        else:
            self._retry_timer.stop()

        if self.chk_record_auto.isChecked():
            QtCore.QTimer.singleShot(300, self._start_record)

        self._save_prefs_runtime()

    def _on_vlc_error(self, e):
        self._log("VLC: EncounteredError")
        self.request_preview_retry.emit(-1)

    def _on_vlc_end(self, e):
        self._log("VLC: EndReached")
        self.request_preview_retry.emit(-1)

    def _on_vlc_stopped(self, e):
        self._log("VLC: Stopped")
        self.request_preview_retry.emit(-1)

    def _maybe_retry_preview(self):
        p = self.video.player()
        if not p:
            return
        st = p.get_state()
        bad_states = {getattr(vlc.State, "Error", None),
                      getattr(vlc.State, "Ended", None),
                      getattr(vlc.State, "Stopped", None)}
        if st in bad_states:
            self.request_preview_retry.emit(-1)

    @QtCore.Slot(int)
    def _schedule_retry_preview_main(self, delay_ms: int):
        if not self.auto_retry_preview_cb.isChecked() or not self._last_url:
            return
        maxr = self.max_preview_retries_sp.value()
        if maxr and self._preview_retries >= maxr:
            self._log("[preview] reached max retries – stop retrying")
            return
        self._preview_retries += 1
        if delay_ms < 0:
            delay_ms = min(30000, 2000 * self._preview_retries)
        self._log(f"[preview] retry {self._preview_retries} in {delay_ms/1000:.1f}s")
        QtCore.QTimer.singleShot(delay_ms, self._restart_vlc)

    def _restart_vlc(self):
        try: self.video.player().stop()
        except Exception: pass
        self._start_player(self._last_url, self._last_force_tcp, self._last_user, self._last_pwd)

    def _stop_player(self):
        self._retry_timer.stop()
        try: self.video.player().stop()
        except Exception: pass
        self._log("Player stopped")

    # ===== Recording (auto-retry) =====
    def _start_record(self):
        if self._rec_thread and self._rec_thread.isRunning():
            QtWidgets.QMessageBox.information(self, "Record", "Recorder already running."); return

        # ל-FFmpeg אין אופציות user/pwd נפרדות — לכן ה-URL יכלול קרדנציאלס, אבל כל הלוגים ממוסכים.
        url = self._compose_rtsp_url()
        user = self.user.text().strip(); pwd = self.pwd.text().strip()
        if user and "@" not in url and "://" in url:
            sch, rest = url.split("://",1)
            url = f"{sch}://{user}:{pwd}@{rest}"

        ffmpeg = self.ffmpeg_path.text().strip() or which("ffmpeg") or "ffmpeg"
        fmt = self.rec_format.currentText().lower().strip()

        out = self.rec_path.text().strip()
        if not out:
            out = str(Path.cwd() / "recordings")
        out_path = Path(out)
        if out_path.is_dir() or (not out_path.suffix and not out_path.exists()):
            folder = out_path if out_path.suffix=="" else out_path
            folder.mkdir(parents=True, exist_ok=True)
            host = parse_host_from_rtsp(url)
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            ext = ".mp4" if fmt=="mp4" else (".mkv" if fmt=="mkv" else ".ts")
            out_path = folder / f"{host}_{ts}{ext}"
        else:
            out_path.parent.mkdir(parents=True, exist_ok=True)

        max_retries = self.max_record_retries_sp.value() if self.auto_retry_rec_cb.isChecked() else 1

        self._rec_thread = RecorderThread(ffmpeg, url, out_path,
                                          force_tcp=self.force_tcp.isChecked(),
                                          probe_timeout_ms=self.probe_timeout_sp.value(),
                                          max_retries= (0 if max_retries==0 else max_retries))
        self._rec_thread.log.connect(self._log)
        self._rec_thread.exited_with_err.connect(lambda rc: self._log(f"[rec] exited rc={rc}"))
        self._rec_thread.finished_ok.connect(lambda: self._log("[rec] finished ok"))
        self._rec_thread.start()

        self.btn_rec_start.setEnabled(False); self.btn_rec_stop.setEnabled(True)
        self.rec_indicator.setText("REC: ON"); self.rec_indicator.setStyleSheet("color:#e33; font-weight:bold;")
        self._log(f"[rec] recording started")

        self._save_prefs_runtime()

    def _stop_record(self):
        if self._rec_thread:
            self._rec_thread.stop()
            self._rec_thread.wait(3000)
            self._rec_thread = None
        self.btn_rec_start.setEnabled(True); self.btn_rec_stop.setEnabled(False)
        self.rec_indicator.setText("REC: OFF"); self.rec_indicator.setStyleSheet("color:#aaa; font-weight:bold;")
        self._log("[rec] stopped")

    # ===== Metrics & Logs =====
    def _update_metrics(self):
        p = self.video.player()
        if p is None: return
        try: w,h = p.video_get_size(0)
        except Exception: w,h = 0,0
        try: fps = p.get_fps() or 0.0
        except Exception: fps = 0.0
        st = p.get_state()
        in_kbps = demux_kbps = "?"
        m = p.get_media()
        if m is not None:
            try:
                stats = m.get_stats()
                if stats:
                    ib = stats.get('input_bitrate',0.0) or 0.0
                    db = stats.get('demux_bitrate',0.0) or 0.0
                    in_kbps = f"{ib*8/1000:.1f}"; demux_kbps = f"{db*8/1000:.1f}"
            except Exception: pass
        self.metrics.setText(f"State: {st} | {w}x{h} | FPS:{fps:.1f} | in:{in_kbps} kbps | demux:{demux_kbps} kbps")

    def _export_logs(self):
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        dst, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save logs", f"logs_{ts}.txt", "Text (*.txt)")
        if not dst: return
        try:
            Path(dst).write_text(self.logs.toPlainText(), encoding="utf-8")
            QtWidgets.QMessageBox.information(self, "Export logs", f"Saved to:\n{dst}")
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Export logs", f"Failed:\n{e}")

    def _log(self, s: str):
        self.logs.appendPlainText(f"{time.strftime('%H:%M:%S')}  {redact(s)}")

    # ===== Profiles =====
    def _load_profiles(self) -> List[dict]:
        if not PROFILES_PATH.exists():
            return []
        try:
            return json.loads(PROFILES_PATH.read_text(encoding="utf-8"))
        except Exception:
            return []

    def _save_profiles(self):
        try:
            PROFILES_PATH.write_text(json.dumps(self._profiles, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Profiles", f"Failed to save profiles:\n{e}")

    def _refresh_profiles_combo(self):
        cur = self.profiles_combo.currentText()
        self.profiles_combo.clear()
        self.profiles_combo.addItem("(select profile)")
        for p in self._profiles:
            self.profiles_combo.addItem(p.get("name","(noname)"))
        if cur:
            idx = self.profiles_combo.findText(cur)
            if idx >= 0: self.profiles_combo.setCurrentIndex(idx)

    def _profile_from_ui(self) -> dict:
        return {
            "name": self.profile_name.text().strip() or self.profiles_combo.currentText().strip(),
            "mode": self.real_mode.currentText(),
            "host": self.host.text().strip(),
            "user": self.user.text().strip(),
            "pwd":  self.pwd.text(),
            "rtsp_port": self.rtsp_port.value(),
            "rtsp_path": self.rtsp_path.text().strip(),
            "onvif_port": self.onvif_port.value(),
            "force_tcp": self.force_tcp.isChecked(),
        }

    def _apply_profile(self, p: dict):
        if not p: return
        self.profile_name.setText(p.get("name",""))
        m = p.get("mode","RTSP")
        i = self.real_mode.findText(m)
        if i>=0: self.real_mode.setCurrentIndex(i)
        self.host.setText(p.get("host",""))
        self.user.setText(p.get("user",""))
        self.pwd.setText(p.get("pwd",""))
        self.rtsp_port.setValue(int(p.get("rtsp_port",554)))
        self.rtsp_path.setText(p.get("rtsp_path","/cam/realmonitor?channel=1&subtype=1"))
        self.onvif_port.setValue(int(p.get("onvif_port",80)))
        self.force_tcp.setChecked(bool(p.get("force_tcp",True)))

    def _profile_load_clicked(self):
        name = self.profiles_combo.currentText().strip()
        if not name or name == "(select profile)":
            QtWidgets.QMessageBox.information(self, "Profiles", "Select a profile to load."); return
        p = next((x for x in self._profiles if x.get("name")==name), None)
        if not p:
            QtWidgets.QMessageBox.warning(self,"Profiles","Profile not found."); return
        self._apply_profile(p)
        self._log(f"Loaded profile: {name}")

    def _profile_saveas_clicked(self):
        name = (self.profile_name.text().strip() or
                QtWidgets.QInputDialog.getText(self,"Save profile","Profile name:")[0].strip())
        if not name:
            return
        p = self._profile_from_ui(); p["name"]=name
        existing = next((x for x in self._profiles if x.get("name")==name), None)
        if existing:
            ans = QtWidgets.QMessageBox.question(self,"Save profile", f"Profile '{name}' exists. Overwrite?",
                                                 QtWidgets.QMessageBox.Yes|QtWidgets.QMessageBox.No)
            if ans != QtWidgets.QMessageBox.Yes: return
            self._profiles = [x for x in self._profiles if x.get("name")!=name]
        self._profiles.append(p); self._save_profiles(); self._refresh_profiles_combo()
        self.profiles_combo.setCurrentText(name)
        self._log(f"Saved profile: {name}")

    def _profile_update_clicked(self):
        name = self.profiles_combo.currentText().strip()
        if not name or name == "(select profile)":
            QtWidgets.QMessageBox.information(self, "Profiles", "Select a profile to update (or use Save as…)."); return
        p = self._profile_from_ui(); p["name"]=name
        found = False
        for i,x in enumerate(self._profiles):
            if x.get("name")==name:
                self._profiles[i]=p; found=True; break
        if not found:
            self._profiles.append(p)
        self._save_profiles(); self._refresh_profiles_combo()
        self.profiles_combo.setCurrentText(name)
        self._log(f"Updated profile: {name}")

    def _profile_delete_clicked(self):
        name = self.profiles_combo.currentText().strip()
        if not name or name == "(select profile)":
            QtWidgets.QMessageBox.information(self, "Profiles", "Select a profile to delete."); return
        ans = QtWidgets.QMessageBox.question(self,"Delete profile", f"Delete '{name}'?",
                                             QtWidgets.QMessageBox.Yes|QtWidgets.QMessageBox.No)
        if ans != QtWidgets.QMessageBox.Yes: return
        self._profiles = [x for x in self._profiles if x.get("name")!=name]
        self._save_profiles(); self._refresh_profiles_combo()
        self.profile_name.clear()
        self._log(f"Deleted profile: {name}")

    # ===== Misc =====
    def _browse_exe(self, edit: QtWidgets.QLineEdit, title: str):
        init = edit.text().strip() or str(Path.home())
        filt = "Executable (*.exe *.bin *);;All files (*.*)"
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, title, init, filt)
        if path: edit.setText(path)

    def _export_logs(self):
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        dst, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save logs", f"logs_{ts}.txt", "Text (*.txt)")
        if not dst: return
        try:
            Path(dst).write_text(self.logs.toPlainText(), encoding="utf-8")
            QtWidgets.QMessageBox.information(self, "Export logs", f"Saved to:\n{dst}")
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Export logs", f"Failed:\n{e}")

    def _save_prefs_runtime(self):
        self.cfg["ffmpeg_path"] = self.ffmpeg_path.text().strip()
        self.cfg["ffprobe_path"] = self.ffprobe_path.text().strip()
        self.cfg["network_caching"] = self.network_caching_sp.value()
        self.cfg["probe_timeout_ms"] = self.probe_timeout_sp.value()
        self.cfg["auto_retry_preview"] = self.auto_retry_preview_cb.isChecked()
        self.cfg["auto_retry_record"] = self.auto_retry_rec_cb.isChecked()
        self.cfg["max_preview_retries"] = self.max_preview_retries_sp.value()
        self.cfg["max_record_retries"] = self.max_record_retries_sp.value()
        self.cfg["mediamtx_path"] = self.mediamtx_path.text().strip()
        self.cfg["mock_file"] = self.mock_file.text().strip()
        self.cfg["mock_port"] = self.mock_port.value()
        self.cfg["mock_mount"] = self.mock_mount.text().strip()
        self.cfg["mock_auto_connect"] = self.auto_connect.isChecked()
        self.cfg["host"] = self.host.text().strip()
        self.cfg["user"] = self.user.text().strip()
        self.cfg["pwd"] = self.pwd.text()
        self.cfg["rtsp_port"] = self.rtsp_port.value()
        self.cfg["rtsp_path"] = self.rtsp_path.text().strip()
        self.cfg["onvif_port"] = self.onvif_port.value()
        self.cfg["force_tcp"] = self.force_tcp.isChecked()
        self.cfg["record_auto"] = self.chk_record_auto.isChecked()
        self.cfg["out_dir"] = self.rec_path.text().strip()
        self.cfg["profiles_combo"] = self.profiles_combo.currentText()
        save_cfg(self.cfg)

    def closeEvent(self, e: QtGui.QCloseEvent) -> None:
        try:
            self._save_prefs_runtime()
            self._retry_timer.stop()
            try: self.video.player().stop()
            except Exception: pass
            if self._rec_thread:
                self._rec_thread.stop()
                self._rec_thread.wait(1500)
        except Exception:
            pass
        return super().closeEvent(e)

# ------------------- main -------------------
def main():
    app = QtWidgets.QApplication(sys.argv)
    w = MainWindow(); w.show()
    sys.exit(app.exec())

if __name__ == '__main__':
    main()

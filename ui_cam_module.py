# ui_cam_module.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Camera connection module (UI) that plugs into the generic shell.
# Relies on camera_io backend and ui_common helpers.
# Mock RTSP relies on a hardened MediaMTX server that auto-picks a free
# port, writes a matching config and loops FFmpeg pushes for low latency.

import platform, subprocess, time, json, datetime, socket, re
from pathlib import Path
from typing import List, Callable, Optional

from PySide6 import QtCore, QtGui, QtWidgets
import vlc

from camera_io import (
    which,
    MediaMtxServer, PushStreamer, RecorderProc,
    probe_rtsp, sanitize_host, parse_host_from_rtsp,
    onvif_get_rtsp_uri, ONVIFCamera
)
from onvif_ptz import PtzMetaThread
from any_ptz_client import AnyPTZClient

from ui_common import VlcVideoWidget, default_vlc_path, open_folder, redact
import shared_state  # <<< לשיתוף הגדרות ה-PTZ עם מודולים אחרים
from app_state import (
    app_state,
    LiveCameraContext,
    load_calibration,
    Intrinsics,
    Distortion,
)
from event_bus import bus

APP_DIR = Path(__file__).resolve().parent
PROFILES_PATH = APP_DIR / "profiles.json"  # module-specific profiles
APP_CFG = APP_DIR / "app_config.json"


def load_cfg() -> dict:
    if APP_CFG.exists():
        try:
            return json.loads(APP_CFG.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_cfg(cfg: dict) -> None:
    try:
        APP_CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


class CameraModule(QtCore.QObject):
    """
    Plug-in style module. Exposes:
      - title: display name in the shell
      - icon:  optional QIcon
      - widget(): returns the root QWidget to embed in the shell
    """
    title = "Cameras (RTSP/ONVIF)"
    icon: Optional[QtGui.QIcon] = None

    def __init__(self, vlc_instance: vlc.Instance, log_func: Callable[[str], None] = print):
        super().__init__()
        self._vlc = vlc_instance
        self._log_func = log_func
        self._cfg = load_cfg()
        self.hevc_guard_ms = int(self._cfg.get("hevc_guard_ms", 4000))
        self.prefer_h264 = bool(self._cfg.get("prefer_h264", True))
        self.suppress_stderr = bool(
            self._cfg.get("silence_native_stderr", self._cfg.get("suppress_stderr", False))
        )
        self._root = self._build_ui()
        # Default to online mode unless overridden by config
        self.radio_online.setChecked(True)
        self.radio_mockup.setChecked(False)
        mode = self._cfg.get("camera_mode")
        if mode is not None:
            self.radio_online.setChecked(mode == "online")
            self.radio_mockup.setChecked(mode == "mockup")
        self._on_mode_changed()
        self._hevc_guard_tried = False
        self._last_codec = ""
        self._ptz_meta: Optional[PtzMetaThread] = None
        self._ptz_client: Optional[AnyPTZClient] = None

        # Auto-retry state for preview
        self._retry_url = ""
        self._retry_force_tcp = True
        self._retry_user = ""
        self._retry_pwd = ""
        self._retry_attempts = 0
        self._retry_pending = False

        # Auto-retry state for recorder
        self._rec_params: Optional[tuple] = None
        self._rec_retry_attempts = 0
        self._rec_retry_pending = False

        # Track last successful connection transport
        self._last_conn_force_tcp = True

    # ---- public API for the shell ----
    def widget(self) -> QtWidgets.QWidget:
        return self._root

    # ---- internal UI ----
    def _build_ui(self) -> QtWidgets.QWidget:
        root = QtWidgets.QWidget()
        vbox = QtWidgets.QVBoxLayout(root)

        # Mode selector (pages)
        mode_box = QtWidgets.QHBoxLayout()
        self.radio_mockup = QtWidgets.QRadioButton("Mockup (local RTSP)")
        self.radio_online = QtWidgets.QRadioButton("Real camera (RTSP/ONVIF)")
        mode_box.addWidget(self.radio_mockup)
        mode_box.addWidget(self.radio_online)
        vbox.addLayout(mode_box)
        self.stack = QtWidgets.QStackedWidget(); vbox.addWidget(self.stack, 1)

        # ---------- Mock page ----------
        mock = QtWidgets.QWidget(); ml = QtWidgets.QGridLayout(mock)
        self.mediamtx_path = QtWidgets.QLineEdit(
            self._cfg.get("mediamtx_path", str(Path.cwd() / "mediamtx.exe"))
        )
        ffmpeg_path = self._cfg.get("ffmpeg_path") or which("ffmpeg")
        if not ffmpeg_path:
            QtWidgets.QMessageBox.warning(
                root, "FFmpeg not found", "ffmpeg executable was not located; please set its path."
            )
        self.ffmpeg_path = QtWidgets.QLineEdit(
            ffmpeg_path or r"C:\\ffmpeg\\bin\\ffmpeg.exe"
        )

        ffprobe_path = self._cfg.get("ffprobe_path") or which("ffprobe")
        if not ffprobe_path:
            QtWidgets.QMessageBox.warning(
                root, "FFprobe not found", "ffprobe executable was not located; please set its path."
            )
        self.ffprobe_path = QtWidgets.QLineEdit(
            ffprobe_path or r"C:\\ffmpeg\\bin\\ffprobe.exe"
        )
        self.mock_file  = QtWidgets.QLineEdit()
        btn_browse = QtWidgets.QPushButton("Browse MP4…"); btn_browse.clicked.connect(self._choose_mp4)
        self.mock_port  = QtWidgets.QSpinBox(); self.mock_port.setRange(1024,65535); self.mock_port.setValue(8554)
        self.mock_mount = QtWidgets.QLineEdit("/cam")
        self.auto_connect = QtWidgets.QCheckBox("Connect automatically"); self.auto_connect.setChecked(True)
        self.btn_start_srv = QtWidgets.QPushButton("Start Mock Server")
        self.btn_stop_srv  = QtWidgets.QPushButton("Stop Mock Server"); self.btn_stop_srv.setEnabled(False)
        self.mock_url      = QtWidgets.QLineEdit(); self.mock_url.setReadOnly(True)
        self.btn_connect_mock = QtWidgets.QPushButton("Connect Preview")

        row=0
        ml.addWidget(QtWidgets.QLabel("MediaMTX path:"),row,0); ml.addWidget(self.mediamtx_path,row,1,1,3); row+=1
        ml.addWidget(QtWidgets.QLabel("FFmpeg path:"),row,0); ml.addWidget(self.ffmpeg_path,row,1,1,3); row+=1
        ml.addWidget(QtWidgets.QLabel("FFprobe path:"),row,0); ml.addWidget(self.ffprobe_path,row,1,1,3); row+=1
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
        # connect profile actions
        self.btn_profile_load.clicked.connect(self.profile_load)
        self.btn_profile_saveas.clicked.connect(self.profile_saveas)
        self.btn_profile_update.clicked.connect(self.profile_update)
        self.btn_profile_delete.clicked.connect(self.profile_delete)

        # פרטי חיבור
        self.real_mode = QtWidgets.QComboBox(); self.real_mode.addItems(["RTSP", "ONVIF → RTSP"])
        self.host = QtWidgets.QLineEdit("192.168.1.100")
        self.user = QtWidgets.QLineEdit("")
        self.pwd  = QtWidgets.QLineEdit(""); self.pwd.setEchoMode(QtWidgets.QLineEdit.Password)
        self.rtsp_port = QtWidgets.QSpinBox(); self.rtsp_port.setRange(1,65535); self.rtsp_port.setValue(554)
        self.rtsp_path = QtWidgets.QLineEdit("/cam/realmonitor?channel=1&subtype=1")
        self.onvif_port = QtWidgets.QSpinBox(); self.onvif_port.setRange(1,65535); self.onvif_port.setValue(80)
        self.force_tcp = QtWidgets.QCheckBox("Force RTSP over TCP (client)"); self.force_tcp.setChecked(True)

        # כפתורי חיבור וכלים
        self.btn_try_dahua = QtWidgets.QPushButton("Try Dahua (auto)")
        self.btn_connect_real = QtWidgets.QPushButton("Connect Camera")
        self.btn_netcheck = QtWidgets.QPushButton("Quick check")
        self.btn_open_ext = QtWidgets.QPushButton("Open in VLC")

        # הקלטה
        self.rec_group = QtWidgets.QGroupBox("Recording (FFmpeg)")
        rec_layout = QtWidgets.QGridLayout(self.rec_group)
        self.chk_record_auto = QtWidgets.QCheckBox("Auto-start record on connect")
        self.rec_format = QtWidgets.QComboBox(); self.rec_format.addItems(["mp4","mkv","ts"])
        self.rec_path = QtWidgets.QLineEdit(str(Path.cwd() / "recordings"))  # תיקייה או קובץ
        self.btn_rec_start = QtWidgets.QPushButton("Start Record")
        self.btn_rec_stop = QtWidgets.QPushButton("Stop Record"); self.btn_rec_stop.setEnabled(False)
        self.btn_rec_folder = QtWidgets.QPushButton("Open recordings folder")
        self.rec_indicator = QtWidgets.QLabel("REC: OFF"); self.rec_indicator.setStyleSheet("color:#aaa; font-weight:bold;")
        r = 0
        rec_layout.addWidget(self.chk_record_auto, r, 0, 1, 2); r += 1
        rec_layout.addWidget(QtWidgets.QLabel("Format:"), r, 0); rec_layout.addWidget(self.rec_format, r, 1); r += 1
        rec_layout.addWidget(QtWidgets.QLabel("Output (folder or file):"), r, 0); rec_layout.addWidget(self.rec_path, r, 1, 1, 2); r += 1
        rec_layout.addWidget(self.btn_rec_start, r, 0); rec_layout.addWidget(self.btn_rec_stop, r, 1); rec_layout.addWidget(self.btn_rec_folder, r, 2); r += 1
        rec_layout.addWidget(self.rec_indicator, r, 0); r += 1

        # PTZ CGI group
        self.ptz_group = QtWidgets.QGroupBox("PTZ CGI")
        ptz_layout = QtWidgets.QGridLayout(self.ptz_group)
        self.ptz_cgi_port = QtWidgets.QSpinBox(); self.ptz_cgi_port.setRange(1, 65535); self.ptz_cgi_port.setValue(int(self._cfg.get("ptz_cgi_port", 80)))
        self.ptz_cgi_channel = QtWidgets.QSpinBox(); self.ptz_cgi_channel.setRange(1, 16); self.ptz_cgi_channel.setValue(int(self._cfg.get("ptz_cgi_channel", 1)))
        self.ptz_cgi_poll = QtWidgets.QDoubleSpinBox(); self.ptz_cgi_poll.setRange(0.1, 30.0); self.ptz_cgi_poll.setSingleStep(0.5); self.ptz_cgi_poll.setValue(float(self._cfg.get("ptz_cgi_poll_hz", 5.0)))
        self.ptz_cgi_https = QtWidgets.QCheckBox("HTTPS"); self.ptz_cgi_https.setChecked(bool(self._cfg.get("ptz_cgi_https", False)))
        r = 0
        ptz_layout.addWidget(QtWidgets.QLabel("Port:"), r, 0); ptz_layout.addWidget(self.ptz_cgi_port, r, 1); r += 1
        ptz_layout.addWidget(QtWidgets.QLabel("Channel:"), r, 0); ptz_layout.addWidget(self.ptz_cgi_channel, r, 1); r += 1
        ptz_layout.addWidget(QtWidgets.QLabel("Poll Hz:"), r, 0); ptz_layout.addWidget(self.ptz_cgi_poll, r, 1); r += 1
        ptz_layout.addWidget(self.ptz_cgi_https, r, 0, 1, 2); r += 1

        # Advanced group
        self.adv_group = QtWidgets.QGroupBox("Advanced")
        adv_layout = QtWidgets.QGridLayout(self.adv_group)
        self.hevc_guard = QtWidgets.QSpinBox(); self.hevc_guard.setRange(0, 10000); self.hevc_guard.setValue(self.hevc_guard_ms)
        self.prefer_h264_chk = QtWidgets.QCheckBox("Prefer H.264"); self.prefer_h264_chk.setChecked(self.prefer_h264)
        self.suppress_stderr_chk = QtWidgets.QCheckBox("Suppress stderr"); self.suppress_stderr_chk.setChecked(self.suppress_stderr)
        r = 0
        adv_layout.addWidget(QtWidgets.QLabel("HEVC guard (ms):"), r, 0); adv_layout.addWidget(self.hevc_guard, r, 1); r += 1
        adv_layout.addWidget(self.prefer_h264_chk, r, 0, 1, 2); r += 1
        adv_layout.addWidget(self.suppress_stderr_chk, r, 0, 1, 2); r += 1

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
        rl.addWidget(self.btn_open_ext,row,3); row+=1

        rl.addWidget(self.ptz_group, row,0,1,4); row+=1
        rl.addWidget(self.rec_group, row,0,1,4); row+=1
        rl.addWidget(self.adv_group, row,0,1,4); row+=1

        self.stack.addWidget(real)

        # ---------- Video + controls ----------
        self.vlc_instance = self._vlc  # alias
        self.video = VlcVideoWidget(self.vlc_instance)
        self.video.setMinimumHeight(360)
        vbox.addWidget(self.video,2)
        h = QtWidgets.QHBoxLayout()
        self.btn_stop_view = QtWidgets.QPushButton("Stop Player")
        self.btn_export_logs = QtWidgets.QPushButton("Export logs…")
        h.addWidget(self.btn_stop_view); h.addWidget(self.btn_export_logs)
        vbox.addLayout(h)

        # ---------- Metrics + logs ----------
        self.metrics = QtWidgets.QLabel("State: idle | 0x0 | FPS:? | in:? kbps | demux:? kbps"); vbox.addWidget(self.metrics)
        self.logs = QtWidgets.QPlainTextEdit(); self.logs.setReadOnly(True); self.logs.setMaximumBlockCount(10000); vbox.addWidget(self.logs)

        # ---------- Backends ----------
        self.mediamtx = MediaMtxServer(self.mediamtx_path.text(), suppress=self.suppress_stderr)
        self.pusher = PushStreamer(self.ffmpeg_path.text(), suppress=self.suppress_stderr)
        for srv in (self.mediamtx, self.pusher):
            srv.log.connect(self._log)

        self.mediamtx.started.connect(lambda _: self._log("MediaMTX started"))
        self.pusher.started.connect(lambda url: self._log("FFmpeg push started to: "+url))

        self.recorder = RecorderProc(suppress=self.suppress_stderr)
        self.recorder.log.connect(self._log)
        self.recorder.started.connect(lambda dst: (self._log(f"Recorder started -> {dst}"), self._set_rec_indicator(True)))
        self.recorder.stopped.connect(lambda dst: (self._log(f"Recorder stopped -> {dst}"), self._set_rec_indicator(False)))
        self.recorder.failed.connect(lambda msg: (self._log(f"Recorder failed: {msg}"), self._set_rec_indicator(False)))

        # ---------- Signals ----------
        self.radio_mockup.toggled.connect(self._on_mode_changed)
        self.radio_online.toggled.connect(self._on_mode_changed)
        self.btn_start_srv.clicked.connect(self._start_mock_server)
        self.btn_stop_srv .clicked.connect(self._stop_mock_server)
        self.btn_connect_mock.clicked.connect(self._connect_mock)

        self.btn_connect_real.clicked.connect(self._connect_real)
        self.btn_try_dahua.clicked.connect(self._auto_try_dahua)

        self.btn_netcheck.clicked.connect(self._quick_check)
        self.btn_open_ext.clicked.connect(self._open_external_vlc)

        self.btn_stop_view.clicked.connect(self._stop_player)
        self.btn_export_logs.clicked.connect(self._export_logs)

        self.btn_rec_start.clicked.connect(self._start_manual_record)
        self.btn_rec_stop.clicked.connect(self._stop_manual_record)
        self.btn_rec_folder.clicked.connect(lambda: open_folder(Path(self.rec_path.text().strip() or ".")))

        # config changes
        self.mediamtx_path.editingFinished.connect(self._save_app_cfg)
        self.ffmpeg_path.editingFinished.connect(self._save_app_cfg)
        self.ffprobe_path.editingFinished.connect(self._save_app_cfg)
        self.ptz_cgi_port.valueChanged.connect(lambda _: self._save_app_cfg())
        self.ptz_cgi_channel.valueChanged.connect(lambda _: self._save_app_cfg())
        self.ptz_cgi_poll.valueChanged.connect(lambda _: self._save_app_cfg())
        self.ptz_cgi_https.stateChanged.connect(lambda _: self._save_app_cfg())
        self.hevc_guard.valueChanged.connect(lambda v: (setattr(self, 'hevc_guard_ms', v), self._save_app_cfg()))
        self.prefer_h264_chk.stateChanged.connect(self._on_prefer_h264_changed)
        self.suppress_stderr_chk.stateChanged.connect(lambda _: (setattr(self, 'suppress_stderr', self.suppress_stderr_chk.isChecked()), self._update_suppress(), self._save_app_cfg()))

        # ---------- Timer ----------
        self.t = QtCore.QTimer(self); self.t.timeout.connect(self._update_metrics); self.t.start(800)

        # ---------- VLC events ----------
        ev = self.video.player().event_manager()
        ev.event_attach(vlc.EventType.MediaPlayerPlaying, lambda e: self._log("VLC: Playing"))
        ev.event_attach(vlc.EventType.MediaPlayerEncounteredError, lambda e: self._log("VLC: EncounteredError"))
        ev.event_attach(vlc.EventType.MediaPlayerEndReached, lambda e: self._log("VLC: EndReached"))

        return root

    def _on_mode_changed(self):
        self.stack.setCurrentIndex(0 if self.radio_mockup.isChecked() else 1)
        app_state.stream_mode = "online" if self.radio_online.isChecked() else "mockup"
        shared_state.signal_stream_mode_changed.emit(app_state.stream_mode)
        self._save_app_cfg()

    # ===== Mock =====
    def _choose_mp4(self):
        dialog = QtWidgets.QFileDialog(self._root, "Choose MP4")
        dialog.setNameFilters(["Video (*.mp4 *.mov *.mkv)","All files (*.*)"])
        dialog.setFileMode(QtWidgets.QFileDialog.ExistingFile)
        if not dialog.exec(): return
        files = dialog.selectedFiles()
        if files: self.mock_file.setText(files[0])

    def _start_mock_server(self):
        p = self.mock_file.text().strip()
        if not p:
            QtWidgets.QMessageBox.warning(self._root, "Missing file", "Please choose an MP4.")
            return

        med = self.mediamtx_path.text().strip()
        if not Path(med).exists():
            QtWidgets.QMessageBox.critical(self._root, "MediaMTX not found", "שים נתיב מלא ל-mediamtx.exe")
            return
        self.mediamtx.mediamtx_path = med

        ok, chosen_port = self.mediamtx.start(int(self.mock_port.value()))
        if not ok:
            QtWidgets.QMessageBox.critical(self._root, "MediaMTX error", "MediaMTX לא מאזין (Firewall/AV/VPN?)")
            return

        self.mock_port.setValue(chosen_port)
        url = f"rtsp://127.0.0.1:{chosen_port}{self.mock_mount.text().strip() or '/cam'}"
        self.mock_url.setText(url)

        ff = self.ffmpeg_path.text().strip()
        if (which(Path(ff).name) is None) and (not Path(ff).exists()):
            QtWidgets.QMessageBox.critical(self._root, "FFmpeg not found", "הגדר נתיב ל-ffmpeg.exe")
            return
        self.pusher.ffmpeg_path = ff

        if not self.pusher.start(p, url):
            QtWidgets.QMessageBox.critical(self._root, "FFmpeg error", "FFmpeg push נכשל")
            return

        if self.auto_connect.isChecked():
            QtCore.QTimer.singleShot(600, lambda: self._start_player(url))
        self.btn_start_srv.setEnabled(False); self.btn_stop_srv.setEnabled(True)
        self._log("Mock RTSP started: " + url)

    def _stop_mock_server(self):
        self.pusher.stop(); self.mediamtx.stop()
        self.btn_start_srv.setEnabled(True); self.btn_stop_srv.setEnabled(False)
        self._log("All mock servers stopped")

    def _connect_mock(self):
        url = self.mock_url.text().strip()
        if not url:
            QtWidgets.QMessageBox.warning(self._root, "No RTSP URL", "Start mock server first.")
            return
        self._start_player(url)

        # --- publish + set Active Camera ---
        h = self.host.text().strip() or parse_host_from_rtsp(url) or ""
        user = self.user.text().strip()
        pwd = self.pwd.text().strip()
        tcp = True

        width = height = fps = None
        codec = ""
        ok, codec, msg = self._probe_codec(url, user, pwd, tcp)
        if ok:
            m = re.search(r"(\d+)x(\d+)", msg)
            if m:
                width, height = int(m.group(1)), int(m.group(2))
            m = re.search(r"(\d+(?:\.\d+)?)\s*fps", msg, re.IGNORECASE)
            if m:
                try:
                    fps = float(m.group(1))
                except Exception:
                    pass

        self._publish_ptz_cfg()
        self._start_ptz_meta(h, user, pwd)
        self._update_live_context(url, h, tcp, codec, width, height, fps, user, pwd)
        self._log("Active camera set from Cameras tab")

    # ===== Real =====
    def _auto_try_dahua(self):
        host, hp, pp = sanitize_host(self.host.text())
        if hp:
            self.rtsp_port.setValue(hp)
        if pp:
            self.rtsp_path.setText(pp)
        host = host.strip(); user = self.user.text().strip(); pwd = self.pwd.text().strip()
        ffprobe = self._get_ffprobe()
        forbidden = 0

        urls: List[str] = []
        ok, uri = onvif_get_rtsp_uri(host, int(self.onvif_port.value()), user, pwd)
        if ok and uri:
            urls.append(uri)

        ports = [self.rtsp_port.value()] + [p for p in (554, 5544, 8554, 10554, 7070) if p != self.rtsp_port.value()]
        chans = list(range(1, 9))
        subs = [0, 1, 2]
        for port in ports:
            for ch in chans:
                for st in subs:
                    paths = [
                        f"/cam/realmonitor?channel={ch}&subtype={st}",
                        f"/cam/preview?channel={ch}&subtype={st}",
                        f"/Streaming/Channels/{ch}0{st+1}",
                        f"/Streaming/Channels/10{ch-1}",
                    ]
                    for path in paths:
                        urls.append(f"rtsp://{host}:{port}{path}")

        for url in urls:
            tcp = True
            ok, msg = probe_rtsp(ffprobe, url, user, pwd, prefer_tcp=True, timeout_ms=3500)
            if not ok and "Timeout" in msg:
                ok, msg = probe_rtsp(ffprobe, url, user, pwd, prefer_tcp=False, timeout_ms=3500)
                tcp = False
            self._log(f"Probe {url} -> {msg}")
            if ok:
                h, p, pa = sanitize_host(url)
                if p:
                    self.rtsp_port.setValue(p)
                if pa:
                    self.rtsp_path.setText(pa)
                self._start_player(url, force_tcp=tcp, user=user, pwd=pwd)
                # פרסום הגדרות PTZ ללשונית Image→Ground
                self._publish_ptz_cfg()
                QtWidgets.QMessageBox.information(self._root, "Auto-try", f"Connected: {url}")
                return
            low = msg.lower()
            if "403" in msg or "forbidden" in low:
                forbidden += 1
                if forbidden >= 2:
                    QtWidgets.QMessageBox.warning(self._root, "Auto-try", "Received multiple 403 Forbidden responses. Aborting to avoid lockout.")
                    return

        QtWidgets.QMessageBox.information(self._root, "Auto-try", "No RTSP URL worked (check permissions/ONVIF/stream settings).")


    def _compose_rtsp_url(self) -> str:
        host_in = self.host.text().strip()
        h, hp, pp = sanitize_host(host_in)
        port = self.rtsp_port.value() if not hp else hp
        path = self.rtsp_path.text().strip() if not pp else pp
        if not path.startswith("/"): path = "/" + path
        return f"rtsp://{h}:{port}{path}"

    def _compose_rtsp_url_with_auth(self) -> str:
        url = self._compose_rtsp_url()
        user = self.user.text().strip(); pwd  = self.pwd.text().strip()
        if user and "@" not in url and "://" in url:
            sch, rest = url.split("://",1)
            return f"{sch}://{user}:{pwd}@{rest}"
        return url

    def _get_ffprobe(self) -> str:
        fp = self.ffprobe_path.text().strip()
        if fp and not Path(fp).exists():
            self.ffprobe_path.setText("")
            fp = ""
        return fp or which("ffprobe") or "ffprobe"
    def _probe_codec(self, url: str, user: str, pwd: str, prefer_tcp: bool) -> tuple:
        ffprobe = self._get_ffprobe()
        ok, msg = probe_rtsp(ffprobe, url, user, pwd,
                             prefer_tcp=prefer_tcp, timeout_ms=3500)
        self._log(f"Probe {url} -> {msg}")
        codec = ""
        if ok:
            m = re.search(r"\(([^)]+)\)", msg)
            if m:
                codec = m.group(1).strip().lower()
        return ok, codec, msg

    def _guess_h264_alt_rtsp(self, path: str) -> Optional[str]:
        if "subtype=" in path and "subtype=1" not in path:
            return re.sub(r"subtype=\d", "subtype=1", path, count=1)
        m = re.search(r"/Streaming/Channels/\d{3}", path, re.IGNORECASE)
        if m and not m.group(0).endswith("101"):
            return re.sub(r"/Streaming/Channels/\d{3}", "/Streaming/Channels/101", path, count=1, flags=re.IGNORECASE)
        return None

    def _guess_h264_alt_onvif(self, host: str, port: int, user: str, pwd: str) -> Optional[str]:
        if ONVIFCamera is None:
            return None
        try:
            cam = ONVIFCamera(host, port, user, pwd)
            media = cam.create_media_service()
            for prof in media.GetProfiles():
                try:
                    enc = getattr(prof.VideoEncoderConfiguration, "Encoding", "")
                    if str(enc).upper() == "H264":
                        params = media.create_type('GetStreamUri')
                        params.StreamSetup = {'Stream':'RTP-Unicast','Transport':{'Protocol':'RTSP'}}
                        params.ProfileToken = prof.token
                        return media.GetStreamUri(params).Uri
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def _connect_real(self):
        mode = self.real_mode.currentText()
        host_in = self.host.text().strip()
        h, hp, pp = sanitize_host(host_in)
        if hp: self.rtsp_port.setValue(hp)
        if pp: self.rtsp_path.setText(pp)
        user = self.user.text().strip(); pwd = self.pwd.text().strip()

        tcp = self.force_tcp.isChecked()

        if mode.startswith("RTSP"):
            url = self._compose_rtsp_url()
            ok, codec, msg = self._probe_codec(url, user, pwd, tcp)
            if not ok and tcp and "Timeout" in msg:
                ok, codec, msg = self._probe_codec(url, user, pwd, False)
                if ok:
                    tcp = False
            width = height = fps = None
            m = re.search(r"(\d+)x(\d+)", msg)
            if m:
                width, height = int(m.group(1)), int(m.group(2))
            m = re.search(r"(\d+(?:\.\d+)?)\s*fps", msg, re.IGNORECASE)
            if m:
                try:
                    fps = float(m.group(1))
                except Exception:
                    fps = None
            self._last_codec = codec
            if self.prefer_h264 and codec in ("hevc", "h265"):
                alt_path = self._guess_h264_alt_rtsp(self.rtsp_path.text().strip())
                if alt_path:
                    alt_url = f"rtsp://{h}:{self.rtsp_port.value()}{alt_path}"
                    ok2, codec2, msg2 = self._probe_codec(alt_url, user, pwd, tcp)
                    if not ok2 and tcp and "Timeout" in msg2:
                        ok2, codec2, msg2 = self._probe_codec(alt_url, user, pwd, False)
                        if ok2:
                            tcp = False
                    if ok2 and codec2 == "h264":
                        self.rtsp_path.setText(alt_path)
                        url = alt_url
                        codec = codec2
                        m = re.search(r"(\d+)x(\d+)", msg2)
                        if m:
                            width, height = int(m.group(1)), int(m.group(2))
                        m = re.search(r"(\d+(?:\.\d+)?)\s*fps", msg2, re.IGNORECASE)
                        if m:
                            try:
                                fps = float(m.group(1))
                            except Exception:
                                fps = None
            self._hevc_guard_tried = False
            self._start_player(url, force_tcp=tcp, user=user, pwd=pwd)
            # פרסום הגדרות PTZ
            self._publish_ptz_cfg()
            self._start_ptz_meta(h, user, pwd)
            self._update_live_context(url, h, tcp, codec, width, height, fps, user, pwd)
            self._log("Active camera set from Cameras tab")
            return

        # ONVIF → RTSP
        port = int(self.onvif_port.value())
        ok, res = onvif_get_rtsp_uri(h, port, user, pwd)
        if not ok and port != 80:
            ok, res = onvif_get_rtsp_uri(h, 80, user, pwd)
        if not ok:
            QtWidgets.QMessageBox.critical(self._root, "ONVIF error", res)
            return
        url = res
        ok, codec, msg = self._probe_codec(url, user, pwd, tcp)
        if not ok and tcp and "Timeout" in msg:
            ok, codec, msg = self._probe_codec(url, user, pwd, False)
            if ok:
                tcp = False
        width = height = fps = None
        m = re.search(r"(\d+)x(\d+)", msg)
        if m:
            width, height = int(m.group(1)), int(m.group(2))
        m = re.search(r"(\d+(?:\.\d+)?)\s*fps", msg, re.IGNORECASE)
        if m:
            try:
                fps = float(m.group(1))
            except Exception:
                fps = None
        self._last_codec = codec
        if self.prefer_h264 and codec in ("hevc", "h265"):
            alt = self._guess_h264_alt_onvif(h, int(self.onvif_port.value()), user, pwd)
            if alt:
                ok2, codec2, msg2 = self._probe_codec(alt, user, pwd, tcp)
                if not ok2 and tcp and "Timeout" in msg2:
                    ok2, codec2, msg2 = self._probe_codec(alt, user, pwd, False)
                    if ok2:
                        tcp = False
                if ok2 and codec2 == "h264":
                    url = alt
                    codec = codec2
                    m = re.search(r"(\d+)x(\d+)", msg2)
                    if m:
                        width, height = int(m.group(1)), int(m.group(2))
                    m = re.search(r"(\d+(?:\.\d+)?)\s*fps", msg2, re.IGNORECASE)
                    if m:
                        try:
                            fps = float(m.group(1))
                        except Exception:
                            fps = None
        self._hevc_guard_tried = False
        self._start_player(url, force_tcp=tcp, user=user, pwd=pwd)
        # פרסום הגדרות PTZ
        self._publish_ptz_cfg()
        self._start_ptz_meta(h, user, pwd)
        self._update_live_context(url, h, tcp, codec, width, height, fps, user, pwd)
        self._log("Active camera set from Cameras tab")

    # ===== Quick tools =====
    def _quick_check(self):
        host_in = self.host.text().strip()
        h, _, _ = sanitize_host(host_in)
        onvif_port = int(self.onvif_port.value())
        rtsp_port  = int(self.rtsp_port.value())

        def port_ok(host, port):
            try:
                with socket.create_connection((host, port), timeout=1.2):
                    return True
            except Exception:
                return False

        ok_rtsp  = port_ok(h, rtsp_port)
        ok_onvif = port_ok(h, onvif_port)

        url_noauth = self._compose_rtsp_url()
        url_auth   = self._compose_rtsp_url_with_auth()

        ffprobe = self._get_ffprobe()
        ok1, msg1 = probe_rtsp(ffprobe, url_noauth, self.user.text().strip(), self.pwd.text().strip(),
                               prefer_tcp=self.force_tcp.isChecked(), timeout_ms=2500)
        if not ok1 and (url_auth != url_noauth):
            ok2, msg2 = probe_rtsp(ffprobe, url_auth, "", "", prefer_tcp=self.force_tcp.isChecked(), timeout_ms=2500)
        else:
            ok2, msg2 = ok1, msg1

        txt = []
        txt.append(f"RTSP port {rtsp_port}: {'OPEN' if ok_rtsp else 'CLOSED'}")
        txt.append(f"ONVIF port {onvif_port}: {'OPEN' if ok_onvif else 'CLOSED'}")
        txt.append(f"ffprobe ({url_noauth}): {msg1}")
        if (url_auth != url_noauth):
            txt.append(f"ffprobe (auth URL): {msg2}")
        QtWidgets.QMessageBox.information(self._root, "Quick check", "\n".join(txt))

    def _open_external_vlc(self):
        url = self._compose_rtsp_url()  # no credentials in URL
        exe = default_vlc_path()
        cmd = [exe]
        if self._last_conn_force_tcp:
            cmd.append("--rtsp-tcp")
        user = self.user.text().strip()
        pwd  = self.pwd.text().strip()
        if user:
            cmd.extend([f"--rtsp-user={user}", f"--rtsp-pwd={pwd}"])
        cmd.append(url)
        try:
            subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if platform.system()=="Windows" else 0)
            )
            self._log("Opened external VLC: " + " ".join(cmd))
        except Exception as e:
            QtWidgets.QMessageBox.warning(self._root, "External VLC", f"Failed to launch VLC:\n{e}")

    # ===== Player / Record =====
    def _start_player(self, url: str, force_tcp: bool=True, user: str="", pwd: str="", reset_retry: bool=True):
        try:
            self.video.player().stop()
        except Exception:
            pass

        self.video.ensure_video_out()
        opts = []
        if force_tcp:
            opts.append(':rtsp-tcp')
        opts += [':avcodec-hw=none', ':network-caching=1200', ':clock-jitter=0', ':no-video-title-show']

        media = self.vlc_instance.media_new(url, *opts)
        if user:
            media.add_option(f":rtsp-user={user}")
        if pwd:
            media.add_option(f":rtsp-pwd={pwd}")
        self.video.player().set_media(media)
        self.video.player().play()
        self._log("Player started: " + url)

        # remember params for auto-retry
        self._retry_url = url
        self._retry_force_tcp = force_tcp
        self._last_conn_force_tcp = force_tcp
        self._retry_user = user
        self._retry_pwd = pwd
        if reset_retry:
            self._retry_attempts = 0
        self._retry_pending = False

        if self.chk_record_auto.isChecked():
            QtCore.QTimer.singleShot(300, self._start_manual_record)

        if self.hevc_guard_ms and not self._hevc_guard_tried:
            QtCore.QTimer.singleShot(self.hevc_guard_ms, self._hevc_guard_check)

        # promote preview to active camera (RTSP only)
        if url.lower().startswith("rtsp://"):
            host = parse_host_from_rtsp(url) or ""
            m = re.match(r"rtsp://(?:(?P<u>[^:@/]*)(?::(?P<p>[^@/]*))?@)?", url)
            user_url = m.group("u") if m and m.group("u") else ""
            pwd_url = m.group("p") if m and m.group("p") else ""
            u = user or user_url
            p = pwd or pwd_url
            ctx = LiveCameraContext(
                online=True,
                host=host,
                rtsp_url=url,
                user=u or None,
                pwd=p or None,
            )
            app_state.current_camera = ctx
            try:
                shared_state.signal_camera_changed.emit(ctx)
            except Exception:
                pass
            try:
                bus.signal_camera_changed.emit(ctx)
            except Exception:
                pass
            self._publish_ptz_cfg()
            try:
                self._start_ptz_meta(host, u, p)
            except Exception as e:
                self._log(f"PTZ meta start failed: {e}")

    def _stop_player(self):
        try:
            self.video.player().stop()
        except Exception:
            pass
        if self._ptz_meta:
            try:
                self._ptz_meta.stop()
            except Exception:
                pass
            self._ptz_meta = None
        if self._ptz_client:
            try:
                self._ptz_client.stop()
            except Exception:
                pass
            self._ptz_client = None

        # cancel auto-retry
        self._retry_url = ""
        self._retry_attempts = 0
        self._retry_pending = False

    def _start_manual_record(self):
        if self.recorder.is_active():
            QtWidgets.QMessageBox.information(self._root, "Record", "Recorder already running.")
            return
        url = self._compose_rtsp_url_with_auth()
        ffmpeg = self.ffmpeg_path.text().strip() or which("ffmpeg") or "ffmpeg"
        fmt = self.rec_format.currentText().lower().strip()

        # יעד: אם זה תיקייה — ניצור שם קובץ אוטומטי; אם זה קובץ — נשתמש בו.
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

        self.recorder.start_record(ffmpeg, url, out_path, self.force_tcp.isChecked(), fmt)
        self._rec_params = (ffmpeg, url, out_path, self.force_tcp.isChecked(), fmt)
        self._rec_retry_attempts = 0
        self._rec_retry_pending = False
        self._log(f"Recording -> {out_path}")
        self.btn_rec_start.setEnabled(False)
        self.btn_rec_stop.setEnabled(True)

    def _stop_manual_record(self):
        if not self.recorder.is_active():
            return
        self.recorder.stop()
        self.btn_rec_start.setEnabled(True)
        self.btn_rec_stop.setEnabled(False)
        self._rec_params = None
        self._rec_retry_attempts = 0
        self._rec_retry_pending = False

    def _set_rec_indicator(self, on: bool):
        if on:
            self.rec_indicator.setText("REC: ON")
            self.rec_indicator.setStyleSheet("color:#e33; font-weight:bold;")
        else:
            self.rec_indicator.setText("REC: OFF")
            self.rec_indicator.setStyleSheet("color:#aaa; font-weight:bold;")

    # ===== Metrics & Logs =====
    def _update_metrics(self):
        p = self.video.player()
        if p is None:
            return
        try:
            w,h = p.video_get_size(0)
        except Exception:
            w,h = 0,0
        fps = p.get_fps() or 0.0
        st = p.get_state()
        in_kbps = demux_kbps = "?"
        m = p.get_media()
        if m is not None:
            try:
                stats = m.get_stats()
                if stats:
                    ib = stats.get('input_bitrate',0.0) or 0.0
                    db = stats.get('demux_bitrate',0.0) or 0.0
                    in_kbps = f"{ib*8/1000:.1f}"
                    demux_kbps = f"{db*8/1000:.1f}"
            except Exception:
                pass
        self.metrics.setText(f"State: {st} | {w}x{h} | FPS:{fps:.1f} | in:{in_kbps} kbps | demux:{demux_kbps} kbps")

        # Auto-retry preview if VLC stopped
        if st == vlc.State.Playing:
            self._retry_attempts = 0
        elif st in (vlc.State.Ended, vlc.State.Error, vlc.State.Stopped):
            if self._retry_url and not self._retry_pending:
                delay = min(30000, 1000 * (2 ** self._retry_attempts))
                self._retry_attempts += 1
                self._retry_pending = True
                self._log(f"Player state {st} -> retry {self._retry_attempts} in {delay/1000:.1f}s")
                QtCore.QTimer.singleShot(delay, self._retry_player)

        # Auto-retry recorder if process died
        if self._rec_params:
            proc = self.recorder.proc
            if proc and proc.poll() is None:
                self._rec_retry_attempts = 0
            elif proc and proc.poll() is not None:
                self._log("Recorder stopped unexpectedly")
                self.recorder.stop()
                if not self._rec_retry_pending:
                    delay = min(30000, 1000 * (2 ** self._rec_retry_attempts))
                    self._rec_retry_attempts += 1
                self._rec_retry_pending = True
                self._log(f"Retrying recorder in {delay/1000:.1f}s (attempt {self._rec_retry_attempts})")
                QtCore.QTimer.singleShot(delay, self._retry_recorder)

    def _retry_player(self):
        self._retry_pending = False
        if not self._retry_url:
            return
        self._log("Retrying player...")
        self._start_player(self._retry_url, force_tcp=self._retry_force_tcp,
                           user=self._retry_user, pwd=self._retry_pwd, reset_retry=False)

    def _retry_recorder(self):
        self._rec_retry_pending = False
        if not self._rec_params:
            return
        ffmpeg, url, out_path, force_tcp, fmt = self._rec_params
        self.recorder.start_record(ffmpeg, url, out_path, force_tcp, fmt)
        self._log(f"Recording -> {out_path}")
    # ===== HEVC guard =====
    def _hevc_guard_check(self):
        if self._hevc_guard_tried or self.radio_mockup.isChecked():
            return
        p = self.video.player()
        if p is None:
            return
        try:
            w, h = p.video_get_size(0)
        except Exception:
            w = h = 0
        fps = p.get_fps() or 0.0
        if (w <= 0 or h <= 0 or fps < 0.1) and self._last_codec in ("hevc", "h265"):
            self._log("HEVC Guard: no video detected, trying H.264 fallback")
            self._fallback_to_h264()

    def _fallback_to_h264(self):
        if self._hevc_guard_tried:
            return
        self._hevc_guard_tried = True
        mode = self.real_mode.currentText()
        host_in = self.host.text().strip()
        h, _, _ = sanitize_host(host_in)
        user = self.user.text().strip(); pwd = self.pwd.text().strip()
        if mode.startswith("RTSP"):
            alt_path = self._guess_h264_alt_rtsp(self.rtsp_path.text().strip())
            if alt_path:
                alt_url = f"rtsp://{h}:{self.rtsp_port.value()}{alt_path}"
                tcp = self._last_conn_force_tcp
                ok, codec, msg = self._probe_codec(alt_url, user, pwd, tcp)
                if not ok and tcp and "Timeout" in msg:
                    ok, codec, msg = self._probe_codec(alt_url, user, pwd, False)
                    if ok:
                        tcp = False
                if ok and codec == "h264":
                    self.rtsp_path.setText(alt_path)
                    self._last_codec = codec
                    self._start_player(alt_url, force_tcp=tcp, user=user, pwd=pwd)
                    self._log("HEVC Guard: fallback to H.264 -> " + alt_url)
                    return
        else:
            alt = self._guess_h264_alt_onvif(h, int(self.onvif_port.value()), user, pwd)
            if alt:
                tcp = self._last_conn_force_tcp
                ok, codec, msg = self._probe_codec(alt, user, pwd, tcp)
                if not ok and tcp and "Timeout" in msg:
                    ok, codec, msg = self._probe_codec(alt, user, pwd, False)
                    if ok:
                        tcp = False
                if ok and codec == "h264":
                    self._last_codec = codec
                    self._start_player(alt, force_tcp=tcp, user=user, pwd=pwd)
                    self._log("HEVC Guard: fallback to H.264 -> " + alt)
                    return
        self._log("HEVC Guard: H.264 fallback failed")

    def _export_logs(self):
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        dst, _ = QtWidgets.QFileDialog.getSaveFileName(self._root, "Save logs", f"logs_{ts}.txt", "Text (*.txt)")
        if not dst:
            return
        try:
            Path(dst).write_text(self.logs.toPlainText(), encoding="utf-8")
            QtWidgets.QMessageBox.information(self._root, "Export logs", f"Saved to:\n{dst}")
        except Exception as e:
            QtWidgets.QMessageBox.warning(self._root, "Export logs", f"Failed:\n{e}")

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
            QtWidgets.QMessageBox.warning(self._root, "Profiles", f"Failed to save profiles:\n{e}")

    def _refresh_profiles_combo(self):
        cur = self.profiles_combo.currentText()
        self.profiles_combo.clear()
        self.profiles_combo.addItem("(select profile)")
        for p in self._profiles:
            self.profiles_combo.addItem(p.get("name","(noname)"))
        if cur:
            idx = self.profiles_combo.findText(cur)
            if idx >= 0:
                self.profiles_combo.setCurrentIndex(idx)

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
            "hevc_guard_ms": self.hevc_guard.value(),
            "ptz_cgi_port": self.ptz_cgi_port.value(),
            "ptz_cgi_channel": self.ptz_cgi_channel.value(),
            "ptz_cgi_poll_hz": self.ptz_cgi_poll.value(),
            "ptz_cgi_https": self.ptz_cgi_https.isChecked(),
        }

    # Public helpers for project IO
    def get_profile(self) -> dict:
        """Return current camera connection settings as a profile dict."""
        p = self._profile_from_ui()
        p["camera_mode"] = "online" if self.radio_online.isChecked() else "mockup"
        return p

    def apply_profile(self, p: dict) -> None:
        """Populate the UI from a profile dict."""
        mode = p.get("camera_mode")
        if mode is None:
            self.radio_online.setChecked(True)
        else:
            self.radio_online.setChecked(mode == "online")
            self.radio_mockup.setChecked(mode == "mockup")
        self._on_mode_changed()
        self._apply_profile(p)

    def _apply_profile(self, p: dict):
        if not p:
            return
        self.profile_name.setText(p.get("name",""))
        m = p.get("mode","RTSP")
        i = self.real_mode.findText(m)
        if i>=0:
            self.real_mode.setCurrentIndex(i)
        self.host.setText(p.get("host",""))
        self.user.setText(p.get("user",""))
        self.pwd.setText(p.get("pwd",""))
        self.rtsp_port.setValue(int(p.get("rtsp_port",554)))
        self.rtsp_path.setText(p.get("rtsp_path","/cam/realmonitor?channel=1&subtype=1"))
        self.onvif_port.setValue(int(p.get("onvif_port",80)))
        self.force_tcp.setChecked(bool(p.get("force_tcp",True)))
        self.hevc_guard.setValue(int(p.get("hevc_guard_ms", self.hevc_guard_ms)))
        self.hevc_guard_ms = self.hevc_guard.value()
        self.ptz_cgi_port.setValue(int(p.get("ptz_cgi_port", self._cfg.get("ptz_cgi_port", 80))))
        self.ptz_cgi_channel.setValue(int(p.get("ptz_cgi_channel", self._cfg.get("ptz_cgi_channel", 1))))
        self.ptz_cgi_poll.setValue(float(p.get("ptz_cgi_poll_hz", self._cfg.get("ptz_cgi_poll_hz", 5.0))))
        self.ptz_cgi_https.setChecked(bool(p.get("ptz_cgi_https", self._cfg.get("ptz_cgi_https", False))))
        # כשמיישמים פרופיל – נפרסם גם את הגדרות ה-PTZ (שימושי כשעוברים לטאב הבא)
        self._publish_ptz_cfg()

    # Profile actions (exposed to toolbar buttons)
    def profile_load(self):
        name = self.profiles_combo.currentText().strip()
        if not name or name == "(select profile)":
            QtWidgets.QMessageBox.information(self._root, "Profiles", "Select a profile to load.")
            return
        p = next((x for x in self._profiles if x.get("name")==name), None)
        if not p:
            QtWidgets.QMessageBox.warning(self._root,"Profiles","Profile not found.")
            return
        self._apply_profile(p); self._log(f"Loaded profile: {name}")

    def profile_saveas(self):
        name, ok = QtWidgets.QInputDialog.getText(None,"Save profile","Profile name:")
        name = (name or "").strip()
        if not ok or not name:
            return
        p = self._profile_from_ui(); p["name"]=name
        existing = next((x for x in self._profiles if x.get("name")==name), None)
        if existing:
            ans = QtWidgets.QMessageBox.question(self._root,"Save profile", f"Profile '{name}' exists. Overwrite?",
                                                 QtWidgets.QMessageBox.Yes|QtWidgets.QMessageBox.No)
            if ans != QtWidgets.QMessageBox.Yes:
                return
            self._profiles = [x for x in self._profiles if x.get("name")!=name]
        self._profiles.append(p); self._save_profiles(); self._refresh_profiles_combo()
        self.profiles_combo.setCurrentText(name); self._log(f"Saved profile: {name}")

    def profile_update(self):
        name = self.profiles_combo.currentText().strip()
        if not name or name == "(select profile)":
            QtWidgets.QMessageBox.information(self._root, "Profiles", "Select a profile to update (or use Save as…).")
            return
        p = self._profile_from_ui(); p["name"]=name
        found = False
        for i,x in enumerate(self._profiles):
            if x.get("name")==name:
                self._profiles[i]=p; found=True; break
        if not found: self._profiles.append(p)
        self._save_profiles(); self._refresh_profiles_combo()
        self.profiles_combo.setCurrentText(name); self._log(f"Updated profile: {name}")

    def profile_delete(self):
        name = self.profiles_combo.currentText().strip()
        if not name or name == "(select profile)":
            QtWidgets.QMessageBox.information(self._root, "Profiles", "Select a profile to delete.")
            return
        ans = QtWidgets.QMessageBox.question(self._root,"Delete profile", f"Delete '{name}'?",
                                             QtWidgets.QMessageBox.Yes|QtWidgets.QMessageBox.No)
        if ans != QtWidgets.QMessageBox.Yes: return
        self._profiles = [x for x in self._profiles if x.get("name")!=name]
        self._save_profiles(); self._refresh_profiles_combo()
        self.profile_name.clear(); self._log(f"Deleted profile: {name}")

    def _on_prefer_h264_changed(self, _: int):
        self.prefer_h264 = self.prefer_h264_chk.isChecked()
        self._save_app_cfg()

    def _save_app_cfg(self):
        self._cfg.update({
            "mediamtx_path": self.mediamtx_path.text().strip(),
            "ffmpeg_path": self.ffmpeg_path.text().strip(),
            "ffprobe_path": self.ffprobe_path.text().strip(),
            "hevc_guard_ms": self.hevc_guard.value(),
            "prefer_h264": self.prefer_h264,
            "silence_native_stderr": self.suppress_stderr_chk.isChecked(),
            "ptz_cgi_port": self.ptz_cgi_port.value(),
            "ptz_cgi_channel": self.ptz_cgi_channel.value(),
            "ptz_cgi_poll_hz": self.ptz_cgi_poll.value(),
            "ptz_cgi_https": self.ptz_cgi_https.isChecked(),
            "camera_mode": "online" if self.radio_online.isChecked() else "mockup",
        })
        self._cfg.pop("suppress_stderr", None)
        save_cfg(self._cfg)
        self.hevc_guard_ms = self.hevc_guard.value()
        self.prefer_h264 = self.prefer_h264_chk.isChecked()
        self.suppress_stderr = self.suppress_stderr_chk.isChecked()
        self.mediamtx.mediamtx_path = self.mediamtx_path.text().strip()
        self.pusher.ffmpeg_path = self.ffmpeg_path.text().strip()

    def _update_suppress(self):
        self.mediamtx.suppress = self.suppress_stderr
        self.pusher.suppress = self.suppress_stderr
        self.recorder.suppress = self.suppress_stderr

    # ---- logging ----
    def _log(self, s: str):
        red = redact(s)
        line = f"{time.strftime('%H:%M:%S')}  {red}"
        try:
            self.logs.appendPlainText(line)  # local pane
        except Exception:
            pass
        try:
            self._log_func(line)  # bubble up to shell
        except Exception:
            pass

    # ---- shared PTZ cfg ----
    def _publish_ptz_cfg(self):
        """
        מפרסם ל-shared_state את הגדרות ה-ONVIF/PTZ כך שלשונית Image→Ground
        תוכל למשוך אותן בלחיצה אחת.
        """
        try:
            shared_state.onvif_cfg = {
                "host": self.host.text().strip(),
                "port": int(self.onvif_port.value()),
                "user": self.user.text().strip(),
                "pwd":  self.pwd.text(),
            }
            self._log("Shared PTZ cfg published.")
        except Exception as e:
            self._log(f"Failed to publish PTZ cfg: {e}")

    def _start_ptz_meta(self, host: str, user: str, pwd: str):
        """מפעיל thread טלמטריית PTZ עם כתיבה ל-CSV."""
        if self._ptz_meta:
            try:
                self._ptz_meta.stop()
            except Exception:
                pass
            self._ptz_meta = None
        if self._ptz_client:
            try:
                self._ptz_client.stop()
            except Exception:
                pass
            self._ptz_client = None

        csv_path = str(Path.cwd() / 'ptz_log.csv')
        onvif_port = int(self.onvif_port.value())
        port = int(self.ptz_cgi_port.value())
        chan = int(self.ptz_cgi_channel.value())
        hz = float(self.ptz_cgi_poll.value())
        https = self.ptz_cgi_https.isChecked()
        try:
            self._ptz_client = AnyPTZClient(
                host,
                onvif_port,
                user,
                pwd,
                cgi_port=port,
                cgi_channel=chan,
                cgi_poll_hz=hz,
                https=https,
            )
            self._ptz_meta = PtzMetaThread(
                client=self._ptz_client,
                sensor_width_mm=6.4,
                csv_path=csv_path,
            )
            self._ptz_meta.start()
            mode = self._ptz_client.mode.upper() if self._ptz_client.mode else ""
            self._log(f"PTZ telemetry ({mode}) logging -> {csv_path}")
        except Exception as e:
            self._log(f"Failed to start PTZ telemetry: {e}")

    def _update_live_context(
        self,
        url: str,
        host: str,
        tcp: bool,
        codec: str,
        width: Optional[int],
        height: Optional[int],
        fps: Optional[float],
        user: str,
        pwd: str,
    ) -> None:
        alias = (
            self.profile_name.text().strip()
            or self.profiles_combo.currentText().strip()
            or "default"
        )
        brand = model = serial = None
        if ONVIFCamera is not None:
            try:
                cam = ONVIFCamera(host, int(self.onvif_port.value()), user, pwd)
                dev = cam.create_device_service()
                info = dev.GetDeviceInformation()
                brand = getattr(info, "Manufacturer", None)
                model = getattr(info, "Model", None)
                serial = getattr(info, "SerialNumber", None)
            except Exception:
                pass

        proj = getattr(app_state, "project", None)
        layers = None
        calib = None
        if proj:
            layers = getattr(proj, "layers_for_camera", {}).get(alias) or getattr(
                proj, "layers", None
            )
            calib = getattr(proj, "calibration_for_camera", {}).get(alias) or getattr(
                proj, "calibration", None
            )
        if not layers:
            layers = {}
            if shared_state.dtm_path:
                layers["dtm"] = shared_state.dtm_path
            if shared_state.orthophoto_path:
                layers["ortho"] = shared_state.orthophoto_path
            if not layers:
                layers = None

        intr = dist = None
        if calib:
            try:
                if all(k in calib for k in ("fx", "fy", "cx", "cy")):
                    intr = Intrinsics(
                        fx=float(calib.get("fx", 0.0)),
                        fy=float(calib.get("fy", 0.0)),
                        cx=float(calib.get("cx", 0.0)),
                        cy=float(calib.get("cy", 0.0)),
                    )
                if any(k in calib for k in ("k1", "k2", "p1", "p2", "k3")):
                    dist = Distortion(
                        k1=float(calib.get("k1", 0.0)),
                        k2=float(calib.get("k2", 0.0)),
                        p1=float(calib.get("p1", 0.0)),
                        p2=float(calib.get("p2", 0.0)),
                        k3=float(calib["k3"]) if calib.get("k3") is not None else None,
                    )
            except Exception:
                intr = dist = None
        if calib is None:
            intr, dist = load_calibration(serial, model, width, height)
            if intr or dist:
                calib = {}
                if intr:
                    calib.update(
                        {"fx": intr.fx, "fy": intr.fy, "cx": intr.cx, "cy": intr.cy}
                    )
                if dist:
                    calib.update(
                        {"k1": dist.k1, "k2": dist.k2, "p1": dist.p1, "p2": dist.p2}
                    )
                    if dist.k3 is not None:
                        calib["k3"] = dist.k3

        ctx = LiveCameraContext(
            online=True,
            brand=brand,
            host=host,
            port=int(self.rtsp_port.value()),
            rtsp_url=url,
            user=user or None,
            pwd=pwd or None,
            transport="tcp" if tcp else "udp",
            codec=codec,
            width=width,
            height=height,
            fps=fps,
            serial=serial,
            model=model,
            intrinsics=intr,
            distortion=dist,
            layers=layers,
            calibration=calib,
            alias=alias,
            used_tcp=tcp,
            mock_file=(
                getattr(proj, "mock_file_for_camera", {}).get(alias) if proj else None
            ),
        )
        app_state.current_camera = ctx
        try:
            shared_state.signal_camera_changed.emit(ctx)
        except Exception:
            pass
        try:
            bus.signal_camera_changed.emit(ctx)
        except Exception:
            pass

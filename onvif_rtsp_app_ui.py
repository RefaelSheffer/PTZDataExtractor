# onvif_rtsp_app_ui.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# UI for ONVIF/RTSP Simple App — split from monolith.
# Depends on camera_io.py (backend).

import sys, platform, subprocess, time, json, datetime, socket
from pathlib import Path
from typing import List

from PySide6 import QtCore, QtGui, QtWidgets
import vlc

from camera_io import (
    which,
    MediaMtxServer, PushStreamer, RecorderProc,
    probe_rtsp, sanitize_host, parse_host_from_rtsp,
    onvif_get_rtsp_uri
)

APP_DIR = Path(__file__).resolve().parent
PROFILES_PATH = APP_DIR / "profiles.json"


# ------------------- UI helpers -------------------
def default_vlc_path() -> str:
    if platform.system() == 'Windows':
        p = r"C:\Program Files\VideoLAN\VLC\vlc.exe"
        return str(p) if Path(p).exists() else "vlc"
    return "vlc"


def open_folder(folder: Path):
    try:
        if platform.system()=="Windows":
            import os
            os.startfile(str(folder))
        elif platform.system()=="Darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
    except Exception:
        pass


# ------------------- VLC video widget -------------------
class VlcVideoWidget(QtWidgets.QFrame):
    def __init__(self, instance: vlc.Instance):
        super().__init__()
        self.setAttribute(QtCore.Qt.WA_DontCreateNativeAncestors, True)
        self.setAttribute(QtCore.Qt.WA_NativeWindow, True)
        self.setStyleSheet("background:#111; border:1px solid #333;")
        self._instance = instance
        self._player = self._instance.media_player_new()

    def player(self) -> vlc.MediaPlayer:
        return self._player

    def ensure_video_out(self):
        wid = int(self.winId())
        if platform.system()=='Windows':
            self._player.set_hwnd(wid)
        elif platform.system()=='Darwin':
            self._player.set_nsobject(wid)
        else:
            self._player.set_xwindow(wid)

    def showEvent(self, e):
        super().showEvent(e); self.ensure_video_out()

    def resizeEvent(self, e):
        super().resizeEvent(e); self.ensure_video_out()


# ------------------- Main Window -------------------
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ONVIF/RTSP — Simple App v5 (modular)")
        self.resize(1280, 900)

        # VLC instance יחיד — אל תכריח opengl ב-Windows
        if platform.system() == "Windows":
            self.vlc_instance = vlc.Instance('--no-plugins-cache', '--vout=direct3d11')
        else:
            # ברירת מחדל עובדת טוב להטמעה ב-Linux/Mac
            self.vlc_instance = vlc.Instance('--no-plugins-cache')

        central = QtWidgets.QWidget(); self.setCentralWidget(central)
        vbox = QtWidgets.QVBoxLayout(central)

        self.mode = QtWidgets.QComboBox(); self.mode.addItems(["Mockup (local RTSP)", "Real camera (RTSP/ONVIF)"])
        vbox.addWidget(self.mode)
        self.stack = QtWidgets.QStackedWidget(); vbox.addWidget(self.stack, 1)

        # ---------- Mock page ----------
        mock = QtWidgets.QWidget(); ml = QtWidgets.QGridLayout(mock)
        self.mediamtx_path = QtWidgets.QLineEdit(str(Path.cwd() / "mediamtx.exe"))
        self.ffmpeg_path   = QtWidgets.QLineEdit(which("ffmpeg")  or r"C:\ffmpeg\bin\ffmpeg.exe")
        self.ffprobe_path  = QtWidgets.QLineEdit(which("ffprobe") or r"C:\ffmpeg\bin\ffprobe.exe")
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
        self.rec_path   = QtWidgets.QLineEdit(str(Path.cwd() / "recordings"))  # תיקייה או קובץ
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
        rl.addWidget(self.btn_open_ext,row,3); row+=1

        rl.addWidget(self.rec_group, row,0,1,4); row+=1

        self.stack.addWidget(real)

        # ---------- Video + controls ----------
        self.video = VlcVideoWidget(self.vlc_instance)
        self.video.setMinimumHeight(420)  # למנוע כיווץ ע"י הלייאאוט
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
        self.mediamtx = MediaMtxServer(self.mediamtx_path.text())
        self.pusher   = PushStreamer(self.ffmpeg_path.text())
        for srv in (self.mediamtx, self.pusher):
            srv.log.connect(self._log)

        self.mediamtx.started.connect(lambda _: self._log("MediaMTX started"))
        self.pusher.started.connect(lambda url: self._log("FFmpeg push started to: "+url))

        self.recorder = RecorderProc()
        self.recorder.log.connect(self._log)
        self.recorder.started.connect(lambda dst: (self._log(f"Recorder started -> {dst}"), self._set_rec_indicator(True)))
        self.recorder.stopped.connect(lambda dst: (self._log(f"Recorder stopped -> {dst}"), self._set_rec_indicator(False)))
        self.recorder.failed.connect(lambda msg: (self._log(f"Recorder failed: {msg}"), self._set_rec_indicator(False)))

        # ---------- Signals ----------
        self.mode.currentIndexChanged.connect(self.stack.setCurrentIndex)
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

        # ---------- Timer ----------
        self.t = QtCore.QTimer(self); self.t.timeout.connect(self._update_metrics); self.t.start(800)

        # ---------- VLC events ----------
        ev = self.video.player().event_manager()
        ev.event_attach(vlc.EventType.MediaPlayerPlaying, lambda e: self._log("VLC: Playing"))
        ev.event_attach(vlc.EventType.MediaPlayerEncounteredError, lambda e: self._log("VLC: EncounteredError"))
        ev.event_attach(vlc.EventType.MediaPlayerEndReached, lambda e: self._log("VLC: EndReached"))

        self._current_media = None  # strong ref

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
            QtWidgets.QMessageBox.warning(self, "Missing file", "Please choose an MP4.")
            return

        med = self.mediamtx_path.text().strip()
        if not Path(med).exists():
            QtWidgets.QMessageBox.critical(self, "MediaMTX not found", "שים נתיב מלא ל-mediamtx.exe")
            return
        self.mediamtx.mediamtx_path = med

        ok, chosen_port = self.mediamtx.start(int(self.mock_port.value()))
        if not ok:
            QtWidgets.QMessageBox.critical(self, "MediaMTX error", "MediaMTX לא מאזין (Firewall/AV/VPN?)")
            return

        self.mock_port.setValue(chosen_port)
        url = f"rtsp://127.0.0.1:{chosen_port}{self.mock_mount.text().strip() or '/cam'}"
        self.mock_url.setText(url)

        ff = self.ffmpeg_path.text().strip()
        if (which(Path(ff).name) is None) and (not Path(ff).exists()):
            QtWidgets.QMessageBox.critical(self, "FFmpeg not found", "הגדר נתיב ל-ffmpeg.exe")
            return
        self.pusher.ffmpeg_path = ff

        if not self.pusher.start(p, url):
            QtWidgets.QMessageBox.critical(self, "FFmpeg error", "FFmpeg push נכשל")
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
            QtWidgets.QMessageBox.warning(self, "No RTSP URL", "Start mock server first.")
            return
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
                        self._log(f"Probe {url} -> {msg}")
                        if ok:
                            self.rtsp_port.setValue(port); self.rtsp_path.setText(path)
                            self._start_player(url, force_tcp=True, user=user, pwd=pwd)
                            QtWidgets.QMessageBox.information(self,"Auto-try", f"Connected: {url}")
                            return
        QtWidgets.QMessageBox.information(self,"Auto-try","No RTSP URL worked (check permissions/ONVIF/stream settings).")

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
            ok, msg = probe_rtsp(ffprobe, url, user, pwd, prefer_tcp=self.force_tcp.isChecked(), timeout_ms=3500)
            self._log(f"Probe {url} -> {msg}")
            if not ok and ("Unauthorized" in msg or "Forbidden" in msg or "Not Found" in msg):
                QtWidgets.QMessageBox.warning(self,"RTSP check", msg)
            self._start_player(url, force_tcp=self.force_tcp.isChecked(), user=user, pwd=pwd)
            return

        # ONVIF → RTSP
        ok, res = onvif_get_rtsp_uri(h, int(self.onvif_port.value()), user, pwd)
        if not ok:
            QtWidgets.QMessageBox.critical(self, "ONVIF error", res)
            return
        self._log("ONVIF URI: " + res)
        self._start_player(res, force_tcp=self.force_tcp.isChecked(), user=user, pwd=pwd)

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

        ffprobe = self.ffprobe_path.text().strip() or which("ffprobe") or "ffprobe"
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
        QtWidgets.QMessageBox.information(self, "Quick check", "\n".join(txt))

    def _open_external_vlc(self):
        url = self._compose_rtsp_url_with_auth()
        exe = default_vlc_path()
        cmd = [exe, "--rtsp-tcp", url]
        try:
            subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if platform.system()=="Windows" else 0)
            )
            self._log("Opened external VLC: " + " ".join(cmd))
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "External VLC", f"Failed to launch VLC:\n{e}")

    # ===== Player / Record =====
    def _start_player(self, url: str, force_tcp: bool=True, user: str="", pwd: str=""):
        try:
            self.video.player().stop()
        except Exception:
            pass

        self.video.ensure_video_out()
        opts = []
        if force_tcp:
            opts.append(':rtsp-tcp')
        opts += [':avcodec-hw=none', ':network-caching=800']

        final_url = url
        if user and "@" not in url and "://" in url:
            sch, rest = url.split('://',1)
            final_url = f"{sch}://{user}:{pwd}@{rest}"

        media = self.vlc_instance.media_new(final_url, *opts)
        self._current_media = media  # keep ref!
        self.video.player().set_media(media)
        self.video.player().play()
        self._log("Player started: " + final_url)

        if self.chk_record_auto.isChecked():
            QtCore.QTimer.singleShot(300, self._start_manual_record)

    def _stop_player(self):
        try:
            self.video.player().stop()
        except Exception:
            pass

    def _start_manual_record(self):
        if self.recorder.is_active():
            QtWidgets.QMessageBox.information(self, "Record", "Recorder already running.")
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
        self.btn_rec_start.setEnabled(False); self.btn_rec_stop.setEnabled(True)

    def _stop_manual_record(self):
        if not self.recorder.is_active():
            return
        self.recorder.stop()
        self.btn_rec_start.setEnabled(True); self.btn_rec_stop.setEnabled(False)

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

    def _export_logs(self):
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        dst, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save logs", f"logs_{ts}.txt", "Text (*.txt)")
        if not dst:
            return
        try:
            Path(dst).write_text(self.logs.toPlainText(), encoding="utf-8")
            QtWidgets.QMessageBox.information(self, "Export logs", f"Saved to:\n{dst}")
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Export logs", f"Failed:\n{e}")

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
        }

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

    def _profile_load_clicked(self):
        name = self.profiles_combo.currentText().strip()
        if not name or name == "(select profile)":
            QtWidgets.QMessageBox.information(self, "Profiles", "Select a profile to load.")
            return
        p = next((x for x in self._profiles if x.get("name")==name), None)
        if not p:
            QtWidgets.QMessageBox.warning(self,"Profiles","Profile not found.")
            return
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
            if ans != QtWidgets.QMessageBox.Yes:
                return
            self._profiles = [x for x in self._profiles if x.get("name")!=name]
        self._profiles.append(p); self._save_profiles(); self._refresh_profiles_combo()
        self.profiles_combo.setCurrentText(name)
        self._log(f"Saved profile: {name}")

    def _profile_update_clicked(self):
        name = self.profiles_combo.currentText().strip()
        if not name or name == "(select profile)":
            QtWidgets.QMessageBox.information(self, "Profiles", "Select a profile to update (or use Save as…).")
            return
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
            QtWidgets.QMessageBox.information(self, "Profiles", "Select a profile to delete.")
            return
        ans = QtWidgets.QMessageBox.question(self,"Delete profile", f"Delete '{name}'?",
                                             QtWidgets.QMessageBox.Yes|QtWidgets.QMessageBox.No)
        if ans != QtWidgets.QMessageBox.Yes:
            return
        self._profiles = [x for x in self._profiles if x.get("name")!=name]
        self._save_profiles(); self._refresh_profiles_combo()
        self.profile_name.clear()
        self._log(f"Deleted profile: {name}")

    # ===== Logs =====
    def _log(self, s: str):
        self.logs.appendPlainText(f"{time.strftime('%H:%M:%S')}  {s}")


# ------------------- main -------------------
def main():
    app = QtWidgets.QApplication(sys.argv)
    w = MainWindow(); w.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()

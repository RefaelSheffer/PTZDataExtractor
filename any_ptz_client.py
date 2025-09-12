#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unified PTZ client with ONVIF→CGI fallback."""
from __future__ import annotations

import time
from typing import Optional

from onvif_ptz import OnvifPTZClient, PTZReading
from ptz_cgi import PtzCgiThread


class AnyPTZClient:
    """Try ONVIF first and fall back to CGI if needed.

    Provides a minimal PTZ polling API of ``start()``, ``stop()`` and
    ``last()`` compatible with :class:`OnvifPTZClient` and
    :class:`PtzCgiThread`.
    """

    def __init__(
        self,
        host: str,
        onvif_port: int,
        user: str,
        pwd: str,
        *,
        onvif_poll_hz: float = 5.0,
        cgi_port: int = 80,
        cgi_channel: int = 1,
        cgi_poll_hz: float = 5.0,
        https: bool = False,
    ) -> None:
        self.host = host
        self.onvif_port = onvif_port
        self.user = user
        self.pwd = pwd
        self.onvif_poll_hz = onvif_poll_hz
        self.cgi_port = cgi_port
        self.cgi_channel = cgi_channel
        self.cgi_poll_hz = cgi_poll_hz
        self.https = https

        self._client: Optional[object] = None
        self.mode: Optional[str] = None  # "onvif" or "cgi"
        self.poll_dt: float = 1.0

    def start(self) -> None:
        """Start PTZ polling.

        Attempts ONVIF first. If that fails, falls back to CGI using the
        parameters supplied at construction. Raises ``RuntimeError`` if both
        methods fail.
        """
        try:
            self._client = OnvifPTZClient(
                self.host,
                self.onvif_port,
                self.user,
                self.pwd,
                poll_hz=self.onvif_poll_hz,
            )
            self._client.start()
            self.mode = "onvif"
            self.poll_dt = getattr(self._client, "poll_dt", 1.0)
            empty = True
            for i in range(5):
                time.sleep(self.poll_dt)
                r = self._client.last()
                if (
                    getattr(r, "pan_deg", None) is not None
                    or getattr(r, "tilt_deg", None) is not None
                    or getattr(r, "zoom_norm", None) is not None
                    or getattr(r, "zoom_mm", None) is not None
                ):
                    empty = False
                    if i >= 2:
                        break
            if empty:
                print("ONVIF telemetry empty → CGI")
                self.switch_to_cgi()
        except Exception as e_onvif:  # pragma: no cover - exceptional path
            try:
                self.switch_to_cgi()
            except Exception as e_cgi:
                raise RuntimeError(
                    f"Failed ONVIF ({e_onvif}); CGI fallback failed ({e_cgi})"
                )

    def stop(self) -> None:
        if self._client:
            try:
                self._client.stop()
            finally:
                self._client = None
                self.mode = None

    def last(self) -> PTZReading:
        if self._client:
            return self._client.last()
        return PTZReading()

    # ----- internal helpers -----
    def switch_to_cgi(self) -> None:
        """Switch polling to CGI fallback."""
        if self._client:
            try:
                self._client.stop()
            except Exception:
                pass
        self._client = PtzCgiThread(
            self.host,
            self.cgi_port,
            self.user,
            self.pwd,
            channel=self.cgi_channel,
            poll_hz=self.cgi_poll_hz,
            https=self.https,
        )
        self._client.start()
        self.mode = "cgi"
        self.poll_dt = getattr(self._client, "poll_dt", 1.0)

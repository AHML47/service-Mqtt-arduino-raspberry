"""
Camera MJPEG streaming — HTTP server that serves a live stream at /stream.mjpg
and exposes focus, zoom, autofocus, and snapshot endpoints.
"""

import io
import json
import logging
import os
import threading
from datetime import datetime
from http import server
from threading import Condition
from urllib.parse import urlparse, parse_qs
from typing import Callable, Optional

logger = logging.getLogger(__name__)

PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Pi Camera Stream</title>
<style>
body{background:#111;color:#eee;font-family:Arial;margin:0;padding:20px;}
.container{max-width:900px;margin:auto;}
img{width:100%;border:2px solid #333;border-radius:10px;background:black;}
.panel{margin-top:20px;background:#1d1d1d;padding:15px;border-radius:10px;}
input[type=range]{width:100%;}
button{margin-top:10px;padding:12px 18px;border:none;border-radius:8px;cursor:pointer;}
#captureStatus{margin-top:10px;}
</style>
</head>
<body>
<div class="container">
<h1>Raspberry Pi Camera — Live Stream</h1>
<img src="/stream.mjpg">
<div class="panel">
<h3>Focus</h3>
<input type="range" id="focus" min="0" max="10" step="0.1" value="1">
<div id="focusValue">1.0</div>
<button onclick="autoFocus()">Auto Focus</button>
<button onclick="manualMode()">Manual</button>
<button onclick="continuousMode()">Continuous AF</button>
</div>
<div class="panel">
<h3>Capture Photo</h3>
<button onclick="capturePhoto()">Capture</button>
<div id="captureStatus"></div>
</div>
<div class="panel">
<h3>Zoom</h3>
<input type="range" id="zoom" min="1" max="8" step="0.5" value="1">
<div id="zoomValue">1.0x</div>
</div>
</div>
<script>
const focusSlider=document.getElementById("focus");
const zoomSlider=document.getElementById("zoom");
const focusValue=document.getElementById("focusValue");
const zoomValue=document.getElementById("zoomValue");
focusSlider.addEventListener("input",async()=>{
  const v=focusSlider.value;focusValue.innerText=v;
  await fetch("/focus?value="+v);
});
zoomSlider.addEventListener("input",async()=>{
  const v=zoomSlider.value;zoomValue.innerText=v+"x";
  await fetch("/zoom?value="+v);
});
async function autoFocus(){await fetch("/af_trigger");}
async function manualMode(){await fetch("/af_mode?mode=manual");}
async function continuousMode(){await fetch("/af_mode?mode=continuous");}
async function capturePhoto(){
  const s=document.getElementById("captureStatus");
  s.innerText="Capturing...";
  try{const r=await fetch("/capture");const d=await r.json();s.innerText="Saved: "+d.file;}
  catch{s.innerText="Capture failed";}
}
</script>
</body>
</html>"""


class StreamingOutput(io.BufferedIOBase):
    def __init__(self):
        self.frame = None
        self.condition = Condition()

    def write(self, buf):
        with self.condition:
            self.frame = bytes(buf)
            self.condition.notify_all()
        return len(buf)


class CameraState:
    def __init__(self, picam2):
        try:
            from libcamera import controls
            self._controls = controls
        except ImportError:
            self._controls = None

        self.picam2 = picam2
        self.lock = threading.Lock()
        self.zoom = 1.0
        self.lens_position = 1.0

        try:
            self.full_crop = picam2.camera_properties["ScalerCropMaximum"]
        except Exception:
            self.full_crop = None

        if self._controls and self.full_crop is not None:
            try:
                picam2.set_controls({
                    "AfMode": self._controls.AfModeEnum.Manual,
                    "LensPosition": self.lens_position,
                    "ScalerCrop": self.full_crop,
                })
            except Exception as exc:
                logger.debug("CameraState init controls: %s", exc)

    def apply_zoom(self):
        if self.full_crop is None:
            return
        x, y, w, h = self.full_crop
        z = max(1.0, self.zoom)
        new_w = int(w / z)
        new_h = int(h / z)
        crop = (x + (w - new_w) // 2, y + (h - new_h) // 2, new_w, new_h)
        try:
            self.picam2.set_controls({"ScalerCrop": crop})
        except Exception as exc:
            logger.debug("apply_zoom: %s", exc)

    def set_focus(self, value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return
        with self.lock:
            self.lens_position = value
            if self._controls:
                try:
                    self.picam2.set_controls({
                        "AfMode": self._controls.AfModeEnum.Manual,
                        "LensPosition": value,
                    })
                except Exception as exc:
                    logger.debug("set_focus: %s", exc)

    def set_zoom(self, value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return
        self.zoom = value
        self.apply_zoom()

    def autofocus(self):
        if not self._controls:
            return
        try:
            self.picam2.set_controls({"AfMode": self._controls.AfModeEnum.Auto})
            self.picam2.autofocus_cycle()
        except Exception as exc:
            logger.debug("autofocus: %s", exc)

    def set_af_mode(self, mode):
        if not self._controls:
            return
        try:
            if mode == "continuous":
                self.picam2.set_controls({"AfMode": self._controls.AfModeEnum.Continuous})
            else:
                self.picam2.set_controls({
                    "AfMode": self._controls.AfModeEnum.Manual,
                    "LensPosition": self.lens_position,
                })
        except Exception as exc:
            logger.debug("set_af_mode: %s", exc)


class _StreamingHandler(server.BaseHTTPRequestHandler):
    def do_GET(self):
        output: StreamingOutput = self.server.streaming_output
        state: CameraState = self.server.camera_state
        photo_dir: str = self.server.photo_dir

        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/":
            self.send_response(301)
            self.send_header("Location", "/index.html")
            self.end_headers()

        elif path == "/index.html":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/stream.mjpg":
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=FRAME")
            self.end_headers()
            try:
                while True:
                    with output.condition:
                        output.condition.wait()
                        frame = output.frame
                    self.wfile.write(b"--FRAME\r\n")
                    self.wfile.write(b"Content-Type:image/jpeg\r\n\r\n")
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
            except Exception:
                pass

        elif path == "/focus":
            state.set_focus(query.get("value", ["1"])[0])
            self._send_ok()

        elif path == "/zoom":
            state.set_zoom(query.get("value", ["1"])[0])
            self._send_ok()

        elif path == "/af_trigger":
            state.autofocus()
            self._send_ok()

        elif path == "/af_mode":
            state.set_af_mode(query.get("mode", ["manual"])[0])
            self._send_ok()

        elif path == "/capture":
            filename = self._save_photo(state.picam2, photo_dir)
            body = json.dumps({"status": "ok", "file": filename}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)

        else:
            self.send_error(404)

    def _send_ok(self):
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _save_photo(self, picam2, photo_dir) -> str:
        os.makedirs(photo_dir, exist_ok=True)
        filename = datetime.now().strftime("%Y%m%d_%H%M%S.jpg")
        path = os.path.join(photo_dir, filename)
        try:
            req = picam2.capture_request()
            req.save("main", path)
            req.release()
            logger.info("Stream capture saved: %s", path)

            on_capture = getattr(self.server, "on_capture", None)
            if on_capture:
                threading.Thread(
                    target=on_capture,
                    args=(path, filename),
                    daemon=True,
                    name="stream-capture-publish",
                ).start()

            return filename
        except Exception as exc:
            logger.error("Stream capture failed: %s", exc)
            return "error"

    def log_message(self, *args):
        pass


class _StreamingHTTPServer(server.ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class CameraStreamServer:
    """Wraps the MJPEG HTTP server lifecycle."""

    def __init__(self, port: int = 8000, photo_dir: str = "photos"):
        self._port = port
        self._photo_dir = photo_dir
        self._server: _StreamingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(
        self,
        streaming_output: StreamingOutput,
        camera_state: CameraState,
        on_capture: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        srv = _StreamingHTTPServer(("", self._port), _StreamingHandler)
        srv.streaming_output = streaming_output
        srv.camera_state = camera_state
        srv.photo_dir = self._photo_dir
        srv.on_capture = on_capture
        self._server = srv
        self._thread = threading.Thread(
            target=srv.serve_forever,
            daemon=True,
            name="camera-stream-http",
        )
        self._thread.start()
        logger.info("Camera stream server started on port %d", self._port)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server = None
        logger.info("Camera stream server stopped")

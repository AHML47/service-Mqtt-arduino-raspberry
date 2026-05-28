"""
Photo capture — keeps the camera running persistently and grabs frames
on demand using capture_request(), exactly as cam.py does.
"""

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


class PhotoCaptureError(RuntimeError):
    pass


@dataclass
class PhotoCaptureResult:
    path: Path
    filename: str
    content: bytes
    captured_at: str


class PhotoCaptureService:
    def __init__(
        self,
        output_dir: str = "photos",
        resolution: Tuple[int, int] = (1280, 720),
        raw_resolution: Tuple[int, int] = (4608, 2592),
        warmup_s: float = 2.0,
        autofocus: bool = True,
        streaming: bool = False,
    ):
        self._output_dir = Path(output_dir)
        self._resolution = resolution
        self._raw_resolution = raw_resolution
        self._warmup_s = warmup_s
        self._autofocus = autofocus
        self._streaming = streaming
        self._picam2 = None
        self._lock = threading.Lock()
        self._streaming_output = None
        self._camera_state = None

    @property
    def streaming_output(self):
        return self._streaming_output

    @property
    def camera_state(self):
        return self._camera_state

    def start(self):
        """Start the camera stream once at service startup."""
        try:
            from picamera2 import Picamera2
        except Exception as exc:
            raise PhotoCaptureError(f"picamera2 import failed: {exc}") from exc

        self._picam2 = Picamera2()
        config = self._picam2.create_video_configuration(
            main={"size": self._resolution, "format": "RGB888"},
            raw={"size": self._raw_resolution},
        )
        self._picam2.configure(config)

        if self._streaming:
            try:
                from picamera2.encoders import MJPEGEncoder
                from picamera2.outputs import FileOutput
                from .camera_stream import StreamingOutput, CameraState
            except Exception as exc:
                raise PhotoCaptureError(f"streaming imports failed: {exc}") from exc

            self._streaming_output = StreamingOutput()
            self._picam2.start_recording(MJPEGEncoder(), FileOutput(self._streaming_output))
            time.sleep(self._warmup_s)
            self._camera_state = CameraState(self._picam2)
        else:
            try:
                from libcamera import controls as lc
                lc_available = True
            except ImportError:
                lc_available = False

            self._picam2.start()
            time.sleep(self._warmup_s)

            if self._autofocus and lc_available:
                try:
                    self._picam2.set_controls({"AfMode": lc.AfModeEnum.Continuous})
                    logger.info("Autofocus set to continuous")
                except Exception as exc:
                    logger.debug("Continuous autofocus not available: %s", exc)

        logger.info("Camera started (%dx%d, streaming=%s)", *self._resolution, self._streaming)

    def stop(self):
        """Stop the camera stream at service shutdown."""
        if self._picam2:
            try:
                if self._streaming:
                    self._picam2.stop_recording()
                else:
                    self._picam2.stop()
            except Exception:
                pass
            self._picam2 = None
        self._streaming_output = None
        self._camera_state = None
        logger.info("Camera stopped")

    def capture(self, delay_s: float = 0.0) -> PhotoCaptureResult:
        """Grab a frame from the live stream after an optional delay."""
        if delay_s > 0:
            time.sleep(delay_s)

        if self._picam2 is None:
            raise PhotoCaptureError("Camera is not started")

        self._output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"photo_{timestamp}.jpg"
        path = self._output_dir / filename

        with self._lock:
            request = self._picam2.capture_request()
            try:
                request.save("main", str(path))
            finally:
                request.release()

        logger.debug("Saved: %s", path)
        return PhotoCaptureResult(
            path=path,
            filename=filename,
            content=path.read_bytes(),
            captured_at=datetime.now().isoformat(),
        )

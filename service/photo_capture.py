"""
Photo capture helper used by the service and the standalone capture script.
"""

import logging
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
        resolution: Tuple[int, int] = (4608, 2592),
        warmup_s: float = 2.0,
        autofocus: bool = True,
    ):
        self._output_dir = Path(output_dir)
        self._resolution = resolution
        self._warmup_s = warmup_s
        self._autofocus = autofocus

    def capture(self, delay_s: float = 0.0) -> PhotoCaptureResult:
        if delay_s > 0:
            time.sleep(delay_s)

        try:
            from picamera2 import Picamera2
        except Exception as exc:
            raise PhotoCaptureError(
                "picamera2 is not available on this system"
            ) from exc

        self._output_dir.mkdir(parents=True, exist_ok=True)

        picam2 = Picamera2()
        path: Optional[Path] = None
        try:
            config = picam2.create_still_configuration(
                main={"size": self._resolution}
            )
            picam2.configure(config)
            picam2.start()
            time.sleep(self._warmup_s)

            if self._autofocus:
                try:
                    picam2.autofocus_cycle()
                    logger.info("Autofocus completed")
                except Exception as exc:
                    logger.debug("Autofocus not available: %s", exc)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"photo_{timestamp}.jpg"
            path = self._output_dir / filename

            picam2.capture_file(str(path))
        finally:
            try:
                picam2.stop()
            except Exception:
                pass

        if path is None:
            raise PhotoCaptureError("photo capture failed before file creation")

        return PhotoCaptureResult(
            path=path,
            filename=path.name,
            content=path.read_bytes(),
            captured_at=datetime.now().isoformat(),
        )

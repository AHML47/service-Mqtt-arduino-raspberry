"""
Camera registry — maps logical camera names to capture services.

The service accepts camera names in payloads so multiple cameras can be
addressed independently:
    {"id": "...", "param": {"camera": "front", "time": 2}}

Each configured camera owns its own PhotoCaptureService instance.
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from .photo_capture import PhotoCaptureError, PhotoCaptureResult, PhotoCaptureService

logger = logging.getLogger(__name__)


@dataclass
class CameraDef:
    name: str
    service: PhotoCaptureService


class CameraRegistry:
    def __init__(self, cameras_config: list, photo_defaults: dict):
        self._by_name: Dict[str, CameraDef] = {}
        self._default_name: Optional[str] = None

        configs = cameras_config or [
            {
                "name": photo_defaults.get("name", "camera1"),
            }
        ]

        for cfg in configs:
            name = (cfg.get("name") or "").strip()
            if not name:
                logger.warning("Skipping camera entry missing name: %s", cfg)
                continue

            resolution = cfg.get("resolution", photo_defaults.get("resolution", [1280, 720]))
            raw_resolution = cfg.get("raw_resolution", photo_defaults.get("raw_resolution", [4608, 2592]))
            camera = PhotoCaptureService(
                name=name,
                output_dir=cfg.get("output_dir", photo_defaults.get("output_dir", "photos")),
                resolution=(int(resolution[0]), int(resolution[1])),
                raw_resolution=(int(raw_resolution[0]), int(raw_resolution[1])),
                warmup_s=float(cfg.get("warmup_s", photo_defaults.get("warmup_s", 2.0))),
                autofocus=bool(cfg.get("autofocus", photo_defaults.get("autofocus", True))),
                streaming=bool(cfg.get("streaming", photo_defaults.get("streaming", False))),
            )

            self._by_name[name] = CameraDef(name=name, service=camera)
            if self._default_name is None:
                self._default_name = name

        logger.info("CameraRegistry: %d camera(s) registered", len(self._by_name))

    def all(self) -> List[CameraDef]:
        return list(self._by_name.values())

    def get(self, name: str) -> Optional[CameraDef]:
        return self._by_name.get(name)

    def default_name(self) -> Optional[str]:
        return self._default_name

    def start_all(self) -> None:
        for camera in self._by_name.values():
            camera.service.start()

    def stop_all(self) -> None:
        for camera in self._by_name.values():
            camera.service.stop()

    def capture(self, name: str, delay_s: float = 0.0) -> PhotoCaptureResult:
        camera = self.get(name)
        if camera is None:
            raise PhotoCaptureError(f"Unknown camera: {name}")
        return camera.service.capture(delay_s=delay_s)
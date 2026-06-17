"""Local ONNX dynamic-expert inference for plant-disease detection.

Pipeline:
    JPEG image
      -> EfficientNet-B0 feature extractor (mrXtrak)
      -> 1280-dimensional feature vector
      -> meta-learner router
      -> one selected expert: ConvNeXt, ResNet50, or InceptionV3
      -> final disease class, confidence, and all class probabilities
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import onnxruntime as ort
from PIL import Image

logger = logging.getLogger(__name__)

IMAGENET_MEAN = np.asarray([0.485, 0.456, 0.406], dtype=np.float32)
IMAGENET_STD = np.asarray([0.229, 0.224, 0.225], dtype=np.float32)

try:
    RESAMPLING_BILINEAR = Image.Resampling.BILINEAR
except AttributeError:  # Pillow < 9 compatibility
    RESAMPLING_BILINEAR = Image.BILINEAR


def _load_json(path: Path) -> Any:
    if not path.is_file():
        raise FileNotFoundError(f"Required metadata file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _stable_softmax(logits: np.ndarray) -> np.ndarray:
    logits = np.asarray(logits, dtype=np.float32)
    shifted = logits - np.max(logits, axis=-1, keepdims=True)
    exponentials = np.exp(shifted)
    return exponentials / np.sum(exponentials, axis=-1, keepdims=True)



def _format_class_probabilities(
    class_names: list[str],
    probabilities: np.ndarray,
) -> list[dict[str, Any]]:
    """Return JSON-friendly probabilities for every class in class-index order."""
    probabilities = np.asarray(probabilities, dtype=np.float32).reshape(-1)

    if len(probabilities) != len(class_names):
        raise ValueError(
            "Expert output size and classes.json disagree: "
            f"{len(probabilities)} probabilities versus {len(class_names)} classes"
        )

    return [
        {
            "class_index": int(index),
            "class_name": str(class_name),
            "probability": float(probability),
            "percentage": float(probability * 100.0),
        }
        for index, (class_name, probability) in enumerate(zip(class_names, probabilities))
    ]


def _resize_shorter_side(image: Image.Image, shorter_side: int) -> Image.Image:
    width, height = image.size
    if width <= 0 or height <= 0:
        raise ValueError(f"Invalid image dimensions: {image.size}")

    if width < height:
        new_width = shorter_side
        new_height = round(height * shorter_side / width)
    else:
        new_height = shorter_side
        new_width = round(width * shorter_side / height)

    return image.resize((new_width, new_height), resample=RESAMPLING_BILINEAR)


def _center_crop(image: Image.Image, crop_size: int) -> Image.Image:
    width, height = image.size
    if width < crop_size or height < crop_size:
        raise ValueError(
            f"Cannot center-crop {crop_size}x{crop_size} from {width}x{height}"
        )
    left = (width - crop_size) // 2
    top = (height - crop_size) // 2
    return image.crop((left, top, left + crop_size, top + crop_size))


def _preprocess_image(image_path: Path, resize_side: int, crop_size: int) -> np.ndarray:
    if not image_path.is_file():
        raise FileNotFoundError(f"Image not found: {image_path}")

    with Image.open(image_path) as image:
        image = image.convert("RGB")
        image = _resize_shorter_side(image, resize_side)
        image = _center_crop(image, crop_size)
        array = np.asarray(image, dtype=np.float32) / 255.0

    array = (array - IMAGENET_MEAN) / IMAGENET_STD
    array = np.transpose(array, (2, 0, 1))
    array = np.expand_dims(array, axis=0)
    return np.ascontiguousarray(array, dtype=np.float32)


class PlantDiseaseDES:
    """Run the exported ONNX dynamic expert-selection pipeline locally."""

    def __init__(self, models_dir: str | Path, cpu_threads: int = 4) -> None:
        self.models_dir = Path(models_dir).resolve()
        self.cpu_threads = max(1, int(cpu_threads))
        self._inference_lock = threading.Lock()

        self.manifest = _load_json(self.models_dir / "manifest.json")
        self.class_names = _load_json(self.models_dir / "classes.json")
        self._validate_manifest()

        exports = self.manifest["exports"]
        extractor_spec = exports["mrxtrak_features"]
        router_spec = exports["meta_learner"]

        self.extractor_input_name = extractor_spec["input_name"]
        self.extractor_output_name = extractor_spec["output_name"]
        self.router_input_name = router_spec["input_name"]
        self.router_output_name = router_spec["output_name"]

        self.extractor_session = self._create_session(extractor_spec["file"])
        self.router_session = self._create_session(router_spec["file"])

        # Only one selected expert stays loaded at a time to limit Pi RAM use.
        self._loaded_expert_index: int | None = None
        self._loaded_expert_session: ort.InferenceSession | None = None

        logger.info(
            "AI pipeline ready: %d classes, models=%s, CPU threads=%d",
            len(self.class_names),
            self.models_dir,
            self.cpu_threads,
        )

    def _validate_manifest(self) -> None:
        num_classes = int(self.manifest["num_classes"])
        if len(self.class_names) != num_classes:
            raise ValueError(
                "classes.json and manifest.json disagree: "
                f"{len(self.class_names)} classes versus {num_classes}"
            )

        experts = self.manifest["experts_in_router_index_order"]
        indices = [int(item["router_index"]) for item in experts]
        if indices != list(range(len(experts))):
            raise ValueError(f"Invalid router indices: {indices}")

    def _create_session(self, model_file: str) -> ort.InferenceSession:
        model_path = self.models_dir / model_file
        if not model_path.is_file():
            raise FileNotFoundError(f"ONNX model not found: {model_path}")

        options = ort.SessionOptions()
        options.intra_op_num_threads = self.cpu_threads
        options.inter_op_num_threads = 1
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        return ort.InferenceSession(
            str(model_path),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )

    def _get_expert_session(self, expert_index: int) -> ort.InferenceSession:
        if (
            self._loaded_expert_session is None
            or self._loaded_expert_index != expert_index
        ):
            self._loaded_expert_session = None
            self._loaded_expert_index = None

            expert_spec = self.manifest["experts_in_router_index_order"][expert_index]
            self._loaded_expert_session = self._create_session(expert_spec["file"])
            self._loaded_expert_index = expert_index
            logger.info("Loaded AI expert: %s", expert_spec["name"])

        return self._loaded_expert_session

    def classify(self, image_path: str | Path) -> dict[str, Any]:
        """Classify one saved image and return the final disease result."""
        with self._inference_lock:
            return self._classify_unlocked(Path(image_path))

    def _classify_unlocked(self, image_path: Path) -> dict[str, Any]:
        timings: dict[str, float] = {}
        preprocessing = self.manifest.get("preprocessing", {})
        router_prep = preprocessing.get("router_and_224_experts", {})
        inception_prep = preprocessing.get("inception_v3", {})

        started = time.perf_counter()
        image_224 = _preprocess_image(
            image_path,
            resize_side=int(router_prep.get("resize_shorter_side", 256)),
            crop_size=int(router_prep.get("center_crop", 224)),
        )
        timings["preprocess_224_seconds"] = time.perf_counter() - started

        started = time.perf_counter()
        features = self.extractor_session.run(
            [self.extractor_output_name],
            {self.extractor_input_name: image_224},
        )[0]
        features = np.ascontiguousarray(features, dtype=np.float32)
        timings["feature_extraction_seconds"] = time.perf_counter() - started

        started = time.perf_counter()
        selector_logits = self.router_session.run(
            [self.router_output_name],
            {self.router_input_name: features},
        )[0]
        selector_probabilities = _stable_softmax(selector_logits)[0]
        selected_expert_index = int(np.argmax(selector_probabilities))
        timings["router_seconds"] = time.perf_counter() - started

        expert_spec = self.manifest["experts_in_router_index_order"][selected_expert_index]
        expected_size = int(expert_spec["input_shape"][-1])

        if expected_size == 224:
            expert_input = image_224
        elif expected_size == 299:
            started = time.perf_counter()
            expert_input = _preprocess_image(
                image_path,
                resize_side=int(inception_prep.get("resize_shorter_side", 342)),
                crop_size=int(inception_prep.get("center_crop", 299)),
            )
            timings["preprocess_299_seconds"] = time.perf_counter() - started
        else:
            raise ValueError(f"Unsupported expert image size: {expected_size}")

        started = time.perf_counter()
        expert_session = self._get_expert_session(selected_expert_index)
        expert_output_name = self._expert_output_name(expert_spec)
        expert_logits = expert_session.run(
            [expert_output_name],
            {expert_spec["input_name"]: expert_input},
        )[0]
        class_probabilities = _stable_softmax(expert_logits)[0]
        timings["selected_expert_seconds"] = time.perf_counter() - started

        class_index = int(np.argmax(class_probabilities))
        probability = float(class_probabilities[class_index])
        all_class_probabilities = _format_class_probabilities(
            self.class_names,
            class_probabilities,
        )
        ranked_class_probabilities = sorted(
            all_class_probabilities,
            key=lambda item: item["probability"],
            reverse=True,
        )
        timings["total_seconds"] = sum(timings.values())

        return {
            # Backward-compatible top prediction fields.
            "class_index": class_index,
            "class_name": self.class_names[class_index],
            "probability": probability,
            "percentage": probability * 100.0,
            "description": f"{self.class_names[class_index]}: {probability * 100.0:.3f}%",

            # Full distribution returned by the selected expert.
            # class_probabilities keeps the same order as classes.json.
            "class_probabilities": all_class_probabilities,

            # ranked_class_probabilities is useful for dashboards / top-k displays.
            "ranked_class_probabilities": ranked_class_probabilities,

            "selected_expert_index": selected_expert_index,
            "selected_expert_name": expert_spec["name"],
            "feature_vector_shape": list(features.shape),
            "router_probabilities": {
                expert["name"]: float(selector_probabilities[int(expert["router_index"])])
                for expert in self.manifest["experts_in_router_index_order"]
            },
            "timings": timings,
        }

    def _expert_output_name(self, expert_spec: dict) -> str:
        """Read the expert output name from manifest exports when available."""
        export_key_by_index = {
            0: "convnext",
            1: "resnet50",
            2: "inception_v3",
        }
        export_key = export_key_by_index.get(int(expert_spec["router_index"]))
        export_spec = self.manifest.get("exports", {}).get(export_key or "", {})
        return str(export_spec.get("output_name", "class_logits"))

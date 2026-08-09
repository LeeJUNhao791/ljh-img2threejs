#!/usr/bin/env python3
"""Extract a constrained pump assembly description with Qwen vision."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


MODEL_ID = "qwen3.7-plus"
REQUEST_TIMEOUT_SECONDS = 90
REQUIRED_KINDS = {"motor", "pump_casing", "base_plate"}
ALLOWED_KINDS = {
    "motor",
    "pump_casing",
    "inlet_flange",
    "outlet_flange",
    "base_plate",
    "support",
    "coupling",
    "fan_cover",
    "cooling_fin",
    "lifting_ring",
    "bolt",
}

SYSTEM_PROMPT = """
You analyze a single product image of an industrial centrifugal pump and motor
assembly. Return one JSON object only. Do not return Markdown or JavaScript.

Schema:
{
  "object_type": "centrifugal_pump_assembly",
  "confidence": 0.0,
  "axis": "x",
  "colors": {
    "painted_metal": "#3977A8",
    "dark_metal": "#263746",
    "bare_metal": "#AAB4BC"
  },
  "components": [
    {
      "id": "motor",
      "name": "Electric motor",
      "kind": "motor",
      "position": [1.2, 0.9, 0.0],
      "size": [1.8, 1.2, 1.2],
      "rotation": [0.0, 0.0, 1.5708],
      "confidence": 0.9
    }
  ],
  "repetitions": [
    {
      "id": "motor-fins",
      "kind": "cooling_fin",
      "parent": "motor",
      "count": 12,
      "axis": [1, 0, 0],
      "radius": 0.55,
      "instance_size": [1.3, 0.08, 0.12],
      "confidence": 0.8
    }
  ]
}

Use only these component kinds: motor, pump_casing, inlet_flange,
outlet_flange, base_plate, support, coupling, fan_cover, lifting_ring.
Use repetitions only for cooling_fin and bolt. Positions, sizes and rotations
are relative values in a right-handed coordinate system. All size values must
be positive. Include motor, pump_casing and base_plate when the image is truly
a pump assembly. If the image is not such an assembly, return the most accurate
object_type instead of pretending it is a pump.
""".strip()


class PumpVisionError(RuntimeError):
    """A safe, user-facing pump vision failure."""


def _finite_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PumpVisionError(f"{field} must be a number")
    number = float(value)
    if number != number or number in {float("inf"), float("-inf")}:
        raise PumpVisionError(f"{field} must be finite")
    return number


def _vector3(value: object, field: str, *, positive: bool = False) -> list[float]:
    if not isinstance(value, list) or len(value) != 3:
        raise PumpVisionError(f"{field} must contain three numbers")
    vector = [_finite_number(item, field) for item in value]
    if positive and any(item <= 0 for item in vector):
        raise PumpVisionError(f"{field} values must be positive")
    return vector


def _confidence(value: object, field: str) -> float:
    confidence = _finite_number(value, field)
    if not 0 <= confidence <= 1:
        raise PumpVisionError(f"{field} must be between 0 and 1")
    return confidence


def validate_pump_visual_spec(payload: object) -> dict[str, Any]:
    """Validate and normalize the constrained visual contract."""
    if not isinstance(payload, dict):
        raise PumpVisionError("Qwen response must be a JSON object")
    if payload.get("object_type") != "centrifugal_pump_assembly":
        raise PumpVisionError("image was not classified as a centrifugal pump assembly")

    confidence = _confidence(payload.get("confidence", 0), "confidence")
    if confidence < 0.65:
        raise PumpVisionError("pump recognition confidence is below 0.65")

    components = payload.get("components")
    if not isinstance(components, list):
        raise PumpVisionError("components must be an array")

    normalized_components: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, component in enumerate(components):
        if not isinstance(component, dict):
            raise PumpVisionError(f"components[{index}] must be an object")
        component_id = component.get("id")
        kind = component.get("kind")
        name = component.get("name")
        if not isinstance(component_id, str) or not component_id.strip():
            raise PumpVisionError(f"components[{index}].id must be a non-empty string")
        if component_id in seen_ids:
            raise PumpVisionError(f"duplicate component id: {component_id}")
        if kind not in ALLOWED_KINDS:
            raise PumpVisionError(f"unsupported pump component kind: {kind}")
        if not isinstance(name, str) or not name.strip():
            raise PumpVisionError(f"components[{index}].name must be a non-empty string")
        seen_ids.add(component_id)
        normalized = dict(component)
        normalized["id"] = component_id.strip()
        normalized["name"] = name.strip()
        normalized["position"] = _vector3(component.get("position"), f"components[{index}].position")
        normalized["size"] = _vector3(component.get("size"), f"components[{index}].size", positive=True)
        normalized["rotation"] = _vector3(component.get("rotation"), f"components[{index}].rotation")
        normalized["confidence"] = _confidence(
            component.get("confidence", confidence),
            f"components[{index}].confidence",
        )
        normalized_components.append(normalized)

    kinds = {component["kind"] for component in normalized_components}
    missing = REQUIRED_KINDS - kinds
    if missing:
        raise PumpVisionError(
            f"missing required pump components: {', '.join(sorted(missing))}"
        )

    repetitions = payload.get("repetitions", [])
    if not isinstance(repetitions, list):
        raise PumpVisionError("repetitions must be an array")
    normalized_repetitions: list[dict[str, Any]] = []
    for index, repetition in enumerate(repetitions):
        if not isinstance(repetition, dict):
            raise PumpVisionError(f"repetitions[{index}] must be an object")
        kind = repetition.get("kind")
        if kind not in {"cooling_fin", "bolt"}:
            raise PumpVisionError(f"unsupported repetition kind: {kind}")
        count = repetition.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise PumpVisionError(f"repetitions[{index}].count must be a positive integer")
        parent = repetition.get("parent")
        if not isinstance(parent, str) or parent not in seen_ids:
            raise PumpVisionError(f"repetitions[{index}].parent must reference a component")
        normalized = dict(repetition)
        normalized["axis"] = _vector3(repetition.get("axis"), f"repetitions[{index}].axis")
        normalized["instance_size"] = _vector3(
            repetition.get("instance_size"),
            f"repetitions[{index}].instance_size",
            positive=True,
        )
        normalized["radius"] = _finite_number(
            repetition.get("radius", 0),
            f"repetitions[{index}].radius",
        )
        normalized["confidence"] = _confidence(
            repetition.get("confidence", confidence),
            f"repetitions[{index}].confidence",
        )
        normalized_repetitions.append(normalized)

    normalized_payload = dict(payload)
    normalized_payload["confidence"] = confidence
    normalized_payload["components"] = normalized_components
    normalized_payload["repetitions"] = normalized_repetitions
    return normalized_payload


def _image_data_url(image_path: Path) -> str:
    mime_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _extract_message_content(response_payload: object) -> str:
    try:
        content = response_payload["choices"][0]["message"]["content"]  # type: ignore[index]
    except (KeyError, IndexError, TypeError) as exc:
        raise PumpVisionError("Qwen response did not contain message content") from exc
    if not isinstance(content, str) or not content.strip():
        raise PumpVisionError("Qwen response content was empty")
    return content


def analyze_pump_image(
    image_path: Path,
    *,
    opener: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    """Send one image to Qwen and return a validated PumpVisualSpec."""
    api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        raise PumpVisionError("DASHSCOPE_API_KEY is not set")
    base_url = os.environ.get("DASHSCOPE_BASE_URL", "").strip()
    if not base_url:
        raise PumpVisionError("DASHSCOPE_BASE_URL is not set")

    image_path = Path(image_path)
    if not image_path.is_file():
        raise PumpVisionError("uploaded image file was not found")

    request_body = {
        "model": MODEL_ID,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Analyze this pump assembly and return JSON only.",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": _image_data_url(image_path)},
                    },
                ],
            },
        ],
        "response_format": {"type": "json_object"},
        "enable_thinking": False,
        "stream": False,
    }
    request = Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with opener(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise PumpVisionError(f"Qwen request failed with HTTP {exc.code}") from None
    except URLError:
        raise PumpVisionError("Qwen request failed because the service was unreachable") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PumpVisionError("Qwen service returned invalid JSON") from None

    content = _extract_message_content(response_payload)
    try:
        visual_spec = json.loads(content)
    except json.JSONDecodeError:
        raise PumpVisionError("Qwen message content was not valid JSON") from None
    return validate_pump_visual_spec(visual_spec)

#!/usr/bin/env python3
"""Apply a validated pump vision contract to an ObjectSculptSpec."""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


PRIMITIVE_BY_KIND = {
    "motor": "cylinder",
    "pump_casing": "torus",
    "inlet_flange": "cylinder",
    "outlet_flange": "cylinder",
    "base_plate": "box",
    "support": "box",
    "coupling": "cylinder",
    "fan_cover": "cylinder",
    "lifting_ring": "torus",
}
MATERIAL_BY_KIND = {
    "coupling": "dark-metal",
    "inlet_flange": "bare-metal",
    "outlet_flange": "bare-metal",
    "lifting_ring": "bare-metal",
}
REQUIRED_KINDS = {"motor", "pump_casing", "base_plate"}


class PumpSpecError(RuntimeError):
    """The visual contract cannot produce an acceptable pump spec."""


def _component_kinds(components: list[dict[str, Any]]) -> set[str]:
    return {
        str(component.get("visualKind"))
        for component in components
        if component.get("visualKind")
    }


def assert_generated_pump_quality(components: list[dict[str, Any]]) -> None:
    """Reject the placeholder outcomes this adapter exists to prevent."""
    if len(components) < 6:
        raise PumpSpecError("parameterized pump needs at least 6 visible components")
    missing = REQUIRED_KINDS - _component_kinds(components)
    if missing:
        raise PumpSpecError(
            f"parameterized pump is missing: {', '.join(sorted(missing))}"
        )
    primitives = {str(component.get("primitive")) for component in components}
    if primitives == {"box"}:
        raise PumpSpecError("all-box pump conversion is not allowed")
    if not primitives.intersection({"cylinder", "torus", "ellipsoid"}):
        raise PumpSpecError("pump conversion needs at least one rounded primitive")


def _color(colors: object, key: str, fallback: str) -> str:
    if not isinstance(colors, dict):
        return fallback
    value = colors.get(key)
    if not isinstance(value, str) or len(value) != 7 or not value.startswith("#"):
        return fallback
    try:
        int(value[1:], 16)
    except ValueError:
        return fallback
    return value.upper()


def _material_from_template(
    template: dict[str, Any],
    material_id: str,
    name: str,
    color: str,
    *,
    metalness: float,
    roughness: float,
) -> dict[str, Any]:
    material = copy.deepcopy(template)
    material["id"] = material_id
    material["name"] = name
    material["baseColor"] = color
    material["color"] = color
    albedo = material.get("albedo")
    if not isinstance(albedo, dict):
        albedo = {}
        material["albedo"] = albedo
    albedo["dominant"] = color
    albedo["secondary"] = [color]
    variation = material.get("colorVariation")
    if not isinstance(variation, dict):
        variation = {}
        material["colorVariation"] = variation
    variation["palette"] = [color, color]
    material["metalness"] = {"base": metalness, "variation": 0.05}
    material["roughness"] = {
        "base": roughness,
        "variation": 0.08,
        "map": "independent-procedural-field",
        "localResponse": "slightly rougher in cavities",
    }
    material["notes"] = "Image-guided industrial pump material approximation."
    return material


def _make_materials(base_spec: dict[str, Any], colors: object) -> list[dict[str, Any]]:
    existing = base_spec.get("materials")
    if not isinstance(existing, list) or not existing or not isinstance(existing[0], dict):
        raise PumpSpecError("base sculpt spec has no material template")
    template = existing[0]
    return [
        _material_from_template(
            template,
            "painted-metal",
            "Painted metal",
            _color(colors, "painted_metal", "#3977A8"),
            metalness=0.55,
            roughness=0.34,
        ),
        _material_from_template(
            template,
            "dark-metal",
            "Dark metal",
            _color(colors, "dark_metal", "#263746"),
            metalness=0.72,
            roughness=0.28,
        ),
        _material_from_template(
            template,
            "bare-metal",
            "Bare metal",
            _color(colors, "bare_metal", "#AAB4BC"),
            metalness=0.9,
            roughness=0.22,
        ),
    ]


def _visual_components(visual_spec: dict[str, Any]) -> list[dict[str, Any]]:
    if visual_spec.get("object_type") != "centrifugal_pump_assembly":
        raise PumpSpecError("visual spec is not a centrifugal pump assembly")
    confidence = visual_spec.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or confidence < 0.65:
        raise PumpSpecError("visual pump confidence is below 0.65")
    components = visual_spec.get("components")
    if not isinstance(components, list):
        raise PumpSpecError("visual components must be an array")
    if not all(isinstance(component, dict) for component in components):
        raise PumpSpecError("every visual component must be an object")
    return components


def _make_component(
    template: dict[str, Any],
    source: dict[str, Any],
) -> dict[str, Any]:
    kind = str(source.get("kind"))
    primitive = PRIMITIVE_BY_KIND.get(kind)
    if primitive is None:
        raise PumpSpecError(f"unsupported visible pump component kind: {kind}")
    component_id = str(source.get("id") or "").strip()
    if not component_id:
        raise PumpSpecError("pump component id is required")
    position = source.get("position")
    size = source.get("size")
    rotation = source.get("rotation")
    if not all(isinstance(value, list) and len(value) == 3 for value in (position, size, rotation)):
        raise PumpSpecError(f"pump component {component_id!r} needs position/size/rotation vectors")

    material_id = MATERIAL_BY_KIND.get(kind, "painted-metal")
    component = copy.deepcopy(template)
    component.update(
        {
            "id": component_id,
            "name": str(source.get("name") or component_id),
            "level": "macro",
            "role": kind.replace("_", "-"),
            "importance": 1.0 if kind in REQUIRED_KINDS else 0.75,
            "confidence": float(source.get("confidence", 0.7)),
            "primitive": primitive,
            "topologyClass": "assembled-solid",
            "topologyRationale": f"{kind} is a separate visible pump assembly part.",
            "parent": None,
            "attachment": None,
            "material": material_id,
            "materialLayers": [material_id],
            "fidelityTier": "blockout",
            "visualKind": kind,
            "sourceConfidence": float(source.get("confidence", 0.7)),
            "detachable": kind != "base_plate",
        }
    )
    component["dimensions"] = {
        "width": float(size[0]),
        "height": float(size[1]),
        "depth": float(size[2]),
        "units": "relative",
        "confidence": float(source.get("confidence", 0.7)),
    }
    component["transform"] = {
        "position": [float(value) for value in position],
        "rotation": [float(value) for value in rotation],
        "scale": [float(value) for value in size],
    }
    descriptor = component.get("geometryDescriptor")
    if not isinstance(descriptor, dict):
        descriptor = {}
        component["geometryDescriptor"] = descriptor
    descriptor["topologyIntent"] = f"parameterized {kind} primitive approximation"
    if kind == "pump_casing":
        descriptor["torusTubeRatio"] = 0.72
    action = component.get("actionProfile")
    if isinstance(action, dict):
        channels = action.get("transformChannels")
        if isinstance(channels, dict):
            channels["detach"] = kind != "base_plate"
            channels["materialState"] = True
        action["animationRole"] = "fixed-base" if kind == "base_plate" else "detachable-part"
        destruction = action.get("destruction")
        if isinstance(destruction, dict):
            destruction["fractureGroup"] = component_id
            destruction["debrisMaterial"] = material_id
    return component


def _make_repetition_systems(
    visual_spec: dict[str, Any],
    component_ids: set[str],
) -> list[dict[str, Any]]:
    source_repetitions = visual_spec.get("repetitions", [])
    if not isinstance(source_repetitions, list):
        raise PumpSpecError("visual repetitions must be an array")
    systems: list[dict[str, Any]] = []
    for index, source in enumerate(source_repetitions):
        if not isinstance(source, dict):
            raise PumpSpecError(f"repetition {index} must be an object")
        kind = source.get("kind")
        if kind not in {"cooling_fin", "bolt"}:
            raise PumpSpecError(f"unsupported repetition kind: {kind}")
        parent = str(source.get("parent") or "")
        if parent not in component_ids:
            raise PumpSpecError(f"repetition parent does not exist: {parent}")
        raw_count = source.get("count")
        if isinstance(raw_count, bool) or not isinstance(raw_count, int):
            raise PumpSpecError("repetition count must be an integer")
        minimum, maximum = (6, 24) if kind == "cooling_fin" else (4, 16)
        count = max(minimum, min(maximum, raw_count))
        axis = source.get("axis", [1, 0, 0])
        instance_size = source.get("instance_size", [0.1, 0.1, 0.1])
        if not isinstance(axis, list) or len(axis) != 3:
            raise PumpSpecError("repetition axis must contain three numbers")
        if not isinstance(instance_size, list) or len(instance_size) != 3:
            raise PumpSpecError("repetition instance_size must contain three numbers")
        systems.append(
            {
                "id": str(source.get("id") or f"{kind}-{index}"),
                "name": "Motor cooling fins" if kind == "cooling_fin" else "Mounting bolts",
                "level": "macro",
                "role": "repeated-detail",
                "importance": 0.7,
                "confidence": float(source.get("confidence", 0.7)),
                "primitive": "box" if kind == "cooling_fin" else "cylinder",
                "parent": parent,
                "count": count,
                "placement": {
                    "mode": "radial",
                    "axis": [float(value) for value in axis],
                    "radius": float(source.get("radius", 0.0)),
                    "startAngleDeg": 0,
                },
                "instanceScale": [float(value) for value in instance_size],
                "material": "painted-metal" if kind == "cooling_fin" else "bare-metal",
                "visualKind": kind,
                "sourceConfidence": float(source.get("confidence", 0.7)),
            }
        )
    return systems


def apply_pump_visual_spec(
    base_spec: dict[str, Any],
    visual_spec: dict[str, Any],
) -> dict[str, Any]:
    """Return a copy of base_spec with its placeholder geometry replaced."""
    if not isinstance(base_spec, dict):
        raise PumpSpecError("base sculpt spec must be an object")
    source_components = _visual_components(visual_spec)
    existing = base_spec.get("componentTree")
    if not isinstance(existing, list) or not existing or not isinstance(existing[0], dict):
        raise PumpSpecError("base sculpt spec has no component template")

    result = copy.deepcopy(base_spec)
    template = existing[0]
    components = [_make_component(template, source) for source in source_components]
    ids = [component["id"] for component in components]
    if len(ids) != len(set(ids)):
        raise PumpSpecError("pump component ids must be unique")
    assert_generated_pump_quality(components)

    result["componentTree"] = components
    result["materials"] = _make_materials(base_spec, visual_spec.get("colors"))
    result["repetitionSystems"] = _make_repetition_systems(visual_spec, set(ids))
    result["componentAdapter"] = "qwen-parameterized-pump-v1"
    result["route"] = "qwen-vision-to-parameterized-threejs"
    result["exactnessTier"] = "single-view-visual-approximation"
    result["approximationNotes"] = [
        "Hidden surfaces and internal mechanical geometry are not recovered from one image.",
        "Dimensions are relative visual estimates, not manufacturing measurements.",
    ]
    result["pumpVisualSpec"] = copy.deepcopy(visual_spec)

    refs = ids
    for build_pass in result.get("buildPasses", []):
        if isinstance(build_pass, dict):
            build_pass["componentRefs"] = refs.copy()
    quality = result.get("qualityContract")
    if isinstance(quality, dict):
        depth = quality.get("minimumSpecDepth")
        if isinstance(depth, dict):
            depth.update(
                {
                    "macroComponents": len(components),
                    "materialLayers": 3,
                    "repetitionSystems": len(result["repetitionSystems"]),
                }
            )
    return result


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PumpSpecError(f"could not read {label} JSON") from exc
    if not isinstance(payload, dict):
        raise PumpSpecError(f"{label} JSON must be an object")
    return payload


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        temporary_path.replace(path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_spec", type=Path)
    parser.add_argument("visual_spec", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        base_spec = _load_json(args.base_spec.expanduser().resolve(), "base spec")
        visual_spec = _load_json(args.visual_spec.expanduser().resolve(), "visual spec")
        result = apply_pump_visual_spec(base_spec, visual_spec)
        output = args.out.expanduser().resolve()
        _write_json_atomic(output, result)
    except PumpSpecError as exc:
        parser.error(str(exc))
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

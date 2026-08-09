import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from apply_pump_visual_spec import (
    PumpSpecError,
    apply_pump_visual_spec,
    assert_generated_pump_quality,
)
from new_sculpt_spec import make_spec


REPO_ROOT = Path(__file__).resolve().parents[2]


def component(component_id, kind, position, size, rotation=None, confidence=0.9):
    return {
        "id": component_id,
        "name": component_id.replace("-", " ").title(),
        "kind": kind,
        "position": position,
        "size": size,
        "rotation": rotation or [0.0, 0.0, 0.0],
        "confidence": confidence,
    }


def valid_visual_spec():
    return {
        "object_type": "centrifugal_pump_assembly",
        "confidence": 0.92,
        "axis": "x",
        "colors": {
            "painted_metal": "#3977A8",
            "dark_metal": "#263746",
            "bare_metal": "#AAB4BC",
        },
        "components": [
            component("motor", "motor", [1.1, 0.9, 0.0], [1.8, 1.15, 1.15], [0, 0, 1.5708]),
            component("pump-casing", "pump_casing", [-0.65, 0.9, 0.0], [1.25, 1.35, 0.55]),
            component("base-plate", "base_plate", [0.15, 0.1, 0.0], [4.2, 0.2, 2.0]),
            component("inlet-flange", "inlet_flange", [-1.25, 0.9, 0.0], [0.65, 0.2, 0.65], [0, 0, 1.5708]),
            component("outlet-flange", "outlet_flange", [-0.65, 1.65, 0.0], [0.6, 0.2, 0.6]),
            component("coupling", "coupling", [0.3, 0.9, 0.0], [0.45, 0.5, 0.5], [0, 0, 1.5708]),
            component("fan-cover", "fan_cover", [2.05, 0.9, 0.0], [0.5, 1.2, 1.2], [0, 0, 1.5708]),
            component("pump-support", "support", [-0.65, 0.42, 0.0], [0.7, 0.65, 0.65]),
        ],
        "repetitions": [
            {
                "id": "motor-fins",
                "kind": "cooling_fin",
                "parent": "motor",
                "count": 40,
                "axis": [1, 0, 0],
                "radius": 0.55,
                "instance_size": [1.4, 0.06, 0.12],
                "confidence": 0.88,
            },
            {
                "id": "base-bolts",
                "kind": "bolt",
                "parent": "base-plate",
                "count": 2,
                "axis": [0, 1, 0],
                "radius": 1.4,
                "instance_size": [0.12, 0.18, 0.12],
                "confidence": 0.8,
            },
        ],
    }


class ApplyPumpVisualSpecTests(unittest.TestCase):
    def test_replaces_placeholder_root_with_parameterized_parts(self):
        result = apply_pump_visual_spec(
            make_spec("Object_123", None),
            valid_visual_spec(),
        )

        components = result["componentTree"]
        ids = {item["id"] for item in components}
        primitives = {item["primitive"] for item in components}
        self.assertNotIn("root", ids)
        self.assertTrue({"motor", "pump-casing", "base-plate"} <= ids)
        self.assertGreaterEqual(len(components), 6)
        self.assertIn("cylinder", primitives)
        self.assertIn("torus", primitives)
        self.assertTrue(all(item["parent"] is None for item in components))
        self.assertEqual(
            next(item for item in components if item["id"] == "motor")["visualKind"],
            "motor",
        )

    def test_creates_materials_and_caps_repetition_counts(self):
        result = apply_pump_visual_spec(
            make_spec("Object_123", None),
            valid_visual_spec(),
        )

        self.assertEqual(
            {item["id"] for item in result["materials"]},
            {"painted-metal", "dark-metal", "bare-metal"},
        )
        repetitions = {item["id"]: item for item in result["repetitionSystems"]}
        self.assertEqual(repetitions["motor-fins"]["count"], 24)
        self.assertEqual(repetitions["base-bolts"]["count"], 4)
        self.assertEqual(repetitions["motor-fins"]["parent"], "motor")

    def test_rejects_too_few_or_all_box_components(self):
        with self.assertRaisesRegex(PumpSpecError, "at least 6"):
            assert_generated_pump_quality(
                [
                    {"id": "motor", "visualKind": "motor", "primitive": "cylinder"},
                    {"id": "pump-casing", "visualKind": "pump_casing", "primitive": "torus"},
                    {"id": "base-plate", "visualKind": "base_plate", "primitive": "box"},
                ]
            )

        all_box = [
            {"id": f"part-{index}", "visualKind": kind, "primitive": "box"}
            for index, kind in enumerate(
                ["motor", "pump_casing", "base_plate", "support", "coupling", "fan_cover"]
            )
        ]
        with self.assertRaisesRegex(PumpSpecError, "all-box"):
            assert_generated_pump_quality(all_box)

    def test_adapted_spec_generates_valid_javascript(self):
        result = apply_pump_visual_spec(
            make_spec("Object_123", None),
            valid_visual_spec(),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            spec_path = Path(temp_dir) / "pump-spec.json"
            ts_path = Path(temp_dir) / "pump-model.ts"
            js_path = Path(temp_dir) / "pump-model.js"
            spec_path.write_text(json.dumps(result), encoding="utf-8")
            generated = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "forge" / "stage3_build" / "generate_threejs_factory.py"),
                    str(spec_path),
                    "--out",
                    str(ts_path),
                    "--force",
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            typescript = ts_path.read_text(encoding="utf-8")
            self.assertIn("new THREE.CylinderGeometry", typescript)
            self.assertIn("new THREE.TorusGeometry", typescript)

            compiled = subprocess.run(
                [
                    "node",
                    str(REPO_ROOT / "demo-viewer" / "node_modules" / "esbuild" / "bin" / "esbuild"),
                    str(ts_path),
                    "--format=esm",
                    f"--outfile={js_path}",
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            checked = subprocess.run(
                ["node", "--check", str(js_path)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)


if __name__ == "__main__":
    unittest.main()

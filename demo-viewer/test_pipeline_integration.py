import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import server


class PipelineIntegrationTests(unittest.TestCase):
    def _run_with_paths(self, temp_dir, analyzer, runner, compiler):
        output_path = Path(temp_dir) / "output"
        source_path = Path(temp_dir) / "src"
        output_path.mkdir()
        source_path.mkdir()
        image_path = Path(temp_dir) / "pump.png"
        image_path.write_bytes(b"fake-pump")
        with (
            patch.object(server, "OUTPUT_PATH", output_path),
            patch.object(server, "SRC_PATH", source_path),
        ):
            result = server.run_pipeline(
                str(image_path),
                "Object_123",
                vision_analyzer=analyzer,
                command_runner=runner,
                module_compiler=compiler,
            )
        return result, output_path, source_path

    def test_qwen_analysis_and_adapter_run_before_stage3(self):
        command_names = []

        def analyzer(image_path):
            self.assertEqual(image_path.name, "pump.png")
            return {
                "object_type": "centrifugal_pump_assembly",
                "confidence": 0.9,
                "components": [],
                "repetitions": [],
            }

        def runner(command, **kwargs):
            command_name = Path(command[2]).name
            command_names.append(command_name)
            if command_name == "generate_threejs_factory.py":
                output = Path(command[command.index("--out") + 1])
                output.write_text("export const generated: boolean = true;", encoding="utf-8")
            return SimpleNamespace(returncode=0, stderr="", stdout="")

        def compiler(source, output):
            self.assertTrue(source.exists())
            output.write_text("export const generated = true;", encoding="utf-8")

        with tempfile.TemporaryDirectory() as temp_dir:
            result, output_path, source_path = self._run_with_paths(
                temp_dir,
                analyzer,
                runner,
                compiler,
            )
            vision_payload = json.loads(
                (output_path / "Object_123-pump-vision.json").read_text(encoding="utf-8")
            )
            self.assertEqual(vision_payload["confidence"], 0.9)
            self.assertTrue((source_path / "createObject_123Model.js").exists())

        self.assertEqual(
            command_names,
            [
                "probe_image.py",
                "new_pre_spec_assessment.py",
                "new_sculpt_spec.py",
                "apply_pump_visual_spec.py",
                "generate_threejs_factory.py",
            ],
        )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["modelPath"], "/src/createObject_123Model.js")

    def test_qwen_failure_stops_before_stage2_and_publishes_no_model_path(self):
        command_names = []

        def runner(command, **kwargs):
            command_names.append(Path(command[2]).name)
            return SimpleNamespace(returncode=0, stderr="", stdout="")

        def analyzer(image_path):
            raise RuntimeError("pump recognition confidence is below 0.65")

        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(RuntimeError, "below 0.65"):
                self._run_with_paths(
                    temp_dir,
                    analyzer,
                    runner,
                    lambda source, output: self.fail("compiler called"),
                )

        self.assertEqual(command_names, ["probe_image.py"])
        self.assertEqual(server.generation_status["status"], "error")
        self.assertIsNone(server.generation_status["modelPath"])

    def test_compile_failure_keeps_existing_model_and_error_status(self):
        def runner(command, **kwargs):
            command_name = Path(command[2]).name
            if command_name == "generate_threejs_factory.py":
                output = Path(command[command.index("--out") + 1])
                output.write_text("export const generated: boolean = true;", encoding="utf-8")
            return SimpleNamespace(returncode=0, stderr="", stdout="")

        def compiler(source, output):
            raise RuntimeError("esbuild failed: deliberate test failure")

        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "src"
            source_path.mkdir()
            previous_model = source_path / "createObject_123Model.js"
            previous_model.write_text("export const previous = true;", encoding="utf-8")
            output_path = Path(temp_dir) / "output"
            output_path.mkdir()
            image_path = Path(temp_dir) / "pump.png"
            image_path.write_bytes(b"fake-pump")
            with (
                patch.object(server, "OUTPUT_PATH", output_path),
                patch.object(server, "SRC_PATH", source_path),
                self.assertRaisesRegex(RuntimeError, "esbuild failed"),
            ):
                server.run_pipeline(
                    str(image_path),
                    "Object_123",
                    vision_analyzer=lambda _path: {
                        "object_type": "centrifugal_pump_assembly",
                        "confidence": 0.9,
                        "components": [],
                        "repetitions": [],
                    },
                    command_runner=runner,
                    module_compiler=compiler,
                )

            self.assertEqual(
                previous_model.read_text(encoding="utf-8"),
                "export const previous = true;",
            )
        self.assertEqual(server.generation_status["status"], "error")
        self.assertIsNone(server.generation_status["modelPath"])


if __name__ == "__main__":
    unittest.main()

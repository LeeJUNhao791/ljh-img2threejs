import subprocess
import tempfile
import unittest
from pathlib import Path

import server


class CompileTypescriptModuleTests(unittest.TestCase):
    def test_compiles_generated_typescript_patterns_to_valid_javascript(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "model.ts"
            output_path = Path(temp_dir) / "model.js"
            source_path.write_text(
                """
type Spec = { doubleSided?: boolean };

function createMapTexture(
  canvas: HTMLCanvasElement,
  colorSpace: THREE.ColorSpace,
  spec: Spec,
): object {
  const stretch = [1, 2];
  return {
    canvas,
    colorSpace,
    stretchX: typeof stretch[0] === 'number' ? Math.max(0.1, stretch[0]) : 1,
    side: spec.doubleSided === true ? THREE.DoubleSide : THREE.FrontSide,
  };
}
""".strip(),
                encoding="utf-8",
            )

            compiler = getattr(server, "compile_typescript_module", None)
            self.assertIsNotNone(compiler, "compile_typescript_module is not implemented")
            compiler(source_path, output_path)

            check = subprocess.run(
                ["node", "--check", str(output_path)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(check.returncode, 0, check.stderr)

    def test_compile_failure_keeps_existing_model_and_reports_diagnostic(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "broken.ts"
            output_path = Path(temp_dir) / "model.js"
            source_path.write_text("const broken: = ;", encoding="utf-8")
            output_path.write_text("export const previous = true;", encoding="utf-8")

            compiler = getattr(server, "compile_typescript_module", None)
            self.assertIsNotNone(compiler, "compile_typescript_module is not implemented")
            with self.assertRaisesRegex(RuntimeError, "esbuild failed"):
                compiler(source_path, output_path)

            self.assertEqual(
                output_path.read_text(encoding="utf-8"),
                "export const previous = true;",
            )


class ImageUploadFormatTests(unittest.TestCase):
    def test_detects_supported_raster_formats_from_bytes(self):
        detector = getattr(server, "detect_image_extension", None)
        self.assertIsNotNone(detector, "detect_image_extension is not implemented")

        self.assertEqual(detector(b"\x89PNG\r\n\x1a\nrest"), ".png")
        self.assertEqual(detector(b"\xff\xd8\xffrest"), ".jpg")
        self.assertEqual(detector(b"RIFF\x00\x00\x00\x00WEBPrest"), ".webp")

    def test_rejects_svg_with_conversion_guidance(self):
        detector = getattr(server, "detect_image_extension", None)
        self.assertIsNotNone(detector, "detect_image_extension is not implemented")

        with self.assertRaisesRegex(ValueError, "convert it to PNG or JPEG"):
            detector(b'<svg xmlns="http://www.w3.org/2000/svg"></svg>')

    def test_file_picker_lists_only_supported_raster_formats(self):
        html = (server.DEMO_PATH / "index.html").read_text(encoding="utf-8")
        self.assertIn('accept="image/png,image/jpeg,image/webp"', html)


if __name__ == "__main__":
    unittest.main()

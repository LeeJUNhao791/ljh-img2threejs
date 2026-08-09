import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from qwen_pump_vision import (
    PumpVisionError,
    analyze_pump_image,
    validate_pump_visual_spec,
)


def valid_pump_payload():
    return {
        "object_type": "centrifugal_pump_assembly",
        "confidence": 0.91,
        "axis": "x",
        "colors": {
            "painted_metal": "#3977A8",
            "dark_metal": "#263746",
            "bare_metal": "#AAB4BC",
        },
        "components": [
            {
                "id": "motor",
                "name": "Electric motor",
                "kind": "motor",
                "position": [1.2, 0.9, 0.0],
                "size": [1.8, 1.2, 1.2],
                "rotation": [0.0, 0.0, 1.5708],
                "confidence": 0.95,
            },
            {
                "id": "pump-casing",
                "name": "Pump casing",
                "kind": "pump_casing",
                "position": [-0.6, 0.9, 0.0],
                "size": [1.2, 1.4, 1.4],
                "rotation": [0.0, 0.0, 0.0],
                "confidence": 0.92,
            },
            {
                "id": "base-plate",
                "name": "Base plate",
                "kind": "base_plate",
                "position": [0.2, 0.1, 0.0],
                "size": [4.0, 0.2, 2.0],
                "rotation": [0.0, 0.0, 0.0],
                "confidence": 0.97,
            },
        ],
        "repetitions": [],
    }


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


class QwenPumpVisionTests(unittest.TestCase):
    def test_missing_key_fails_before_reading_image_or_calling_network(self):
        with patch.dict(
            os.environ,
            {
                "DASHSCOPE_API_KEY": "",
                "DASHSCOPE_BASE_URL": "https://example.test/compatible-mode/v1",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(PumpVisionError, "DASHSCOPE_API_KEY"):
                analyze_pump_image(
                    Path("missing-pump.png"),
                    opener=lambda *_args, **_kwargs: self.fail("network called"),
                )

    def test_validates_required_pump_components(self):
        spec = validate_pump_visual_spec(valid_pump_payload())

        self.assertEqual(spec["object_type"], "centrifugal_pump_assembly")
        self.assertEqual(len(spec["components"]), 3)

    def test_rejects_low_confidence_or_missing_required_parts(self):
        low_confidence = valid_pump_payload()
        low_confidence["confidence"] = 0.64
        with self.assertRaisesRegex(PumpVisionError, "below 0.65"):
            validate_pump_visual_spec(low_confidence)

        missing_motor = valid_pump_payload()
        missing_motor["components"] = [
            item for item in missing_motor["components"] if item["kind"] != "motor"
        ]
        with self.assertRaisesRegex(PumpVisionError, "motor"):
            validate_pump_visual_spec(missing_motor)

    def test_sends_non_thinking_multimodal_json_request(self):
        captured = {}
        api_response = {
            "choices": [
                {"message": {"content": json.dumps(valid_pump_payload())}}
            ]
        }

        def opener(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse(api_response)

        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "pump.png"
            image_path.write_bytes(b"fake-png")
            with patch.dict(
                os.environ,
                {
                    "DASHSCOPE_API_KEY": "secret-test-key",
                    "DASHSCOPE_BASE_URL": "https://example.test/compatible-mode/v1/",
                },
                clear=False,
            ):
                result = analyze_pump_image(image_path, opener=opener)

        request = captured["request"]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, "https://example.test/compatible-mode/v1/chat/completions")
        self.assertEqual(body["model"], "qwen3.7-plus")
        self.assertFalse(body["enable_thinking"])
        self.assertFalse(body["stream"])
        self.assertEqual(body["response_format"], {"type": "json_object"})
        image_item = body["messages"][1]["content"][1]
        self.assertTrue(image_item["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(result["object_type"], "centrifugal_pump_assembly")
        self.assertEqual(captured["timeout"], 90)

    def test_http_error_does_not_leak_key_or_image_data(self):
        def opener(request, timeout):
            raise HTTPError(
                request.full_url,
                401,
                "Unauthorized secret-test-key ZmFrZS1wbmc=",
                hdrs=None,
                fp=io.BytesIO(b"sensitive provider response"),
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "pump.png"
            image_path.write_bytes(b"fake-png")
            with patch.dict(
                os.environ,
                {
                    "DASHSCOPE_API_KEY": "secret-test-key",
                    "DASHSCOPE_BASE_URL": "https://example.test/compatible-mode/v1",
                },
                clear=False,
            ):
                with self.assertRaises(PumpVisionError) as caught:
                    analyze_pump_image(image_path, opener=opener)

        message = str(caught.exception)
        self.assertIn("HTTP 401", message)
        self.assertNotIn("secret-test-key", message)
        self.assertNotIn("ZmFrZS1wbmc=", message)
        self.assertNotIn("sensitive provider response", message)


if __name__ == "__main__":
    unittest.main()

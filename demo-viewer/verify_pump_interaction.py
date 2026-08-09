#!/usr/bin/env python3
"""Headless browser acceptance check for one generated pump module."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model_file")
    parser.add_argument("--screenshot", type=Path, required=True)
    args = parser.parse_args()

    safe_name = Path(args.model_file).name
    if safe_name != args.model_file or not safe_name.endswith("Model.js"):
        parser.error("model_file must be a generated Model.js filename")

    console_errors: list[str] = []
    page_errors: list[str] = []
    http_errors: list[str] = []
    edge_path = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=edge_path,
            headless=True,
            args=["--disable-gpu", "--no-proxy-server"],
        )
        page = browser.new_page(viewport={"width": 1200, "height": 1000})
        page.on(
            "console",
            lambda message: console_errors.append(message.text)
            if message.type == "error"
            and not message.text.startswith("Failed to load resource")
            else None,
        )
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on(
            "response",
            lambda response: http_errors.append(f"{response.status} {response.url}")
            if response.status >= 400 and not response.url.endswith("/favicon.ico")
            else None,
        )
        page.goto("http://127.0.0.1:8765/", wait_until="networkidle")
        page.wait_for_function("() => Boolean(window.img2threejsViewer)")

        load_summary = page.evaluate(
            """async (modelFile) => {
              const module = await import(`/src/${modelFile}?t=${Date.now()}`);
              const factoryName = Object.keys(module).find(
                (name) => /^create.+Model$/.test(name) && typeof module[name] === 'function'
              );
              if (!factoryName) throw new Error(`No generated model factory in ${modelFile}`);
              const model = module[factoryName]({
                textureSize: 128,
                qualityPriority: 'reference-fidelity',
              });
              window.img2threejsViewer.clearScene();
              window.img2threejsViewer.addModel(model);
              window.img2threejsViewer.controls.autoRotate = false;
              const runtime = model.userData.sculptRuntime;
              return {
                factoryName,
                componentIds: Object.keys(runtime?.meshes || {}),
                childCount: model.children.length,
              };
            }""",
            safe_name,
        )
        if len(load_summary["componentIds"]) < 6:
            raise AssertionError(f"expected at least 6 selectable parts: {load_summary}")

        click_point = page.evaluate(
            """() => {
              const viewer = window.img2threejsViewer;
              const root = viewer.scene.children.find(
                (child) => child.userData?.sculptRuntime
              );
              const mesh = Object.values(root.userData.sculptRuntime.meshes)[0];
              const point = mesh.getWorldPosition(mesh.position.clone()).project(viewer.camera);
              const rect = viewer.renderer.domElement.getBoundingClientRect();
              return {
                x: rect.left + ((point.x + 1) / 2) * rect.width,
                y: rect.top + ((1 - point.y) / 2) * rect.height,
              };
            }"""
        )
        page.mouse.click(click_point["x"], click_point["y"])
        page.locator("#component-info:not(.hidden)").wait_for()
        selected_name = page.locator("#component-name").inner_text()
        if not selected_name:
            raise AssertionError("component click did not populate the information panel")

        canvas = page.locator("#canvas-container canvas").bounding_box()
        if canvas is None:
            raise AssertionError("viewer canvas was not rendered")
        page.mouse.click(canvas["x"] + 8, canvas["y"] + 8)
        page.locator("#component-info.hidden").wait_for(state="hidden")

        args.screenshot.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(args.screenshot), full_page=True)
        browser.close()

    if console_errors or page_errors or http_errors:
        raise AssertionError(
            json.dumps(
                {"consoleErrors": console_errors, "pageErrors": page_errors, "httpErrors": http_errors},
                ensure_ascii=False,
            )
        )
    print(
        json.dumps(
            {
                "status": "ok",
                "factoryName": load_summary["factoryName"],
                "selectableParts": len(load_summary["componentIds"]),
                "selectedName": selected_name,
                "emptyClickClearedSelection": True,
                "screenshot": str(args.screenshot),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

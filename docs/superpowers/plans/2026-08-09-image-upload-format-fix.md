# Image Upload Format Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Preserve supported raster-image formats during upload, reject SVG clearly, and keep the pump-only quality gate unchanged.

**Architecture:** Detect the actual image format from its byte signature at the HTTP boundary and choose the saved suffix from that result. Restrict the browser file picker to PNG, JPEG, and WebP while retaining server-side validation as the authoritative gate.

**Tech Stack:** Python standard library HTTP server and unittest; browser HTML/JavaScript.

---

### Task 1: Detect supported raster uploads

**Files:**
- Modify: `demo-viewer/test_server.py`
- Modify: `demo-viewer/server.py`

- [x] Add tests asserting PNG, JPEG, and WebP signatures map to `.png`, `.jpg`, and `.webp`.
- [x] Add a test asserting SVG bytes raise `ValueError` with a conversion-to-PNG/JPEG message.
- [x] Run `python -m unittest demo-viewer/test_server.py -v` and confirm the new tests fail because the detector is absent.
- [x] Implement `detect_image_extension(image_data)` and use it when constructing the uploaded image path.
- [x] Run `python -m unittest demo-viewer/test_server.py -v` and confirm all tests pass.

### Task 2: Restrict the browser picker

**Files:**
- Modify: `demo-viewer/test_server.py`
- Modify: `demo-viewer/index.html`

- [x] Add a test asserting the file input accepts only `image/png,image/jpeg,image/webp`.
- [x] Run the targeted test and confirm it fails against `accept="image/*"`.
- [x] Change the input accept list to the supported raster formats.
- [x] Run the targeted test and confirm it passes.

### Task 3: Regression verification

**Files:**
- Test: `demo-viewer/test_server.py`
- Test: `demo-viewer/test_pipeline_integration.py`
- Test: `demo-viewer/test_qwen_pump_vision.py`

- [x] Run `python -m unittest discover -s demo-viewer -p "test_*.py" -v`.
- [x] Run `npm test --prefix demo-viewer`.
- [x] Run `git diff --check` and inspect the final diff to ensure unrelated local files remain untouched.

# Qwen 参数化泵演示 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 上传一张离心泵/电机图片后，由 `qwen3.7-plus` 输出受约束的泵结构 JSON，再由本地适配器生成可拆分、可点击的 Three.js 参数化泵；不再把默认占位 `root` 方块当成最终模型。

**Architecture:** `server.py` 只编排流水线；`qwen_pump_vision.py` 负责图片请求、响应解析与视觉规格校验；`apply_pump_visual_spec.py` 把通过质量门禁的视觉规格转换为现有 ObjectSculptSpec；Stage 3 继续使用现有生成器。前端用射线检测读取生成器已有的 `userData.sculptComponent`，完成选中、高亮和部件信息展示。任何视觉/API/结构校验失败都终止流水线，不回退到默认方块。

**Tech Stack:** Python 3.12 标准库（`urllib.request`、`unittest`）、Qwen OpenAI-compatible Chat Completions API、现有 ObjectSculptSpec/Three.js 生成器、Three.js r170、Node.js 内置测试与 `node --check`。

---

## 文件结构

- Create: `demo-viewer/qwen_pump_vision.py` — Qwen 请求、JSON 提取、`PumpVisualSpec` 校验及脱敏错误。
- Create: `demo-viewer/test_qwen_pump_vision.py` — API 请求契约和失败门禁单元测试。
- Create: `forge/stage2_spec/apply_pump_visual_spec.py` — 将视觉规格转换为 ObjectSculptSpec 的纯函数和 CLI。
- Create: `forge/stage2_spec/test_apply_pump_visual_spec.py` — 组件树、材质、重复结构及拒绝方块回退测试。
- Modify: `demo-viewer/server.py` — 在 Stage 1 后调用 Qwen，在 Stage 2b 后应用泵规格，再进入 Stage 3。
- Modify: `demo-viewer/test_server.py` — 流水线成功、门禁失败和不发布旧模型路径测试。
- Create: `demo-viewer/src/component-selection.js` — 可测试的选中目标解析与高亮逻辑。
- Create: `demo-viewer/test_component_selection.mjs` — Node 单元测试。
- Modify: `demo-viewer/src/main.js` — 射线拾取、清除旧选中状态、接入信息面板。
- Modify: `demo-viewer/index.html` — 部件信息面板与操作提示。
- Modify: `demo-viewer/src/style.css` — 信息面板样式。
- Modify: `demo-viewer/package.json` — 增加前端测试命令。

## 约束与验收标准

- 模型固定为 `qwen3.7-plus`；只从 `DASHSCOPE_API_KEY` 和 `DASHSCOPE_BASE_URL` 读取配置。
- 请求使用 `response_format={"type":"json_object"}`、`enable_thinking=false`、`stream=false`。
- 日志和异常不得包含 API Key、Authorization header 或图片 Base64。
- 视觉规格必须是泵类型，整体置信度至少 `0.65`，并含 `motor`、`pump_casing`、`base_plate`。
- 转换后的 `componentTree` 不包含默认可见 `root` 方块；真实部件直接以 `parent: "root"` 挂载到生成器自带的 `THREE.Group`。
- 至少生成 6 个可选择部件，且不能全部是 `box`；冷却片/螺栓使用 `repetitionSystems`。
- 任何门禁失败时 `generation_status.status == "error"`、`modelPath is None`，不生成/发布新的 JS 模型。
- 浏览器中点击真实部件会高亮并显示 `id/name/kind/confidence`；点击空白会恢复材质并清空信息。
- 首版不承诺 CAD 精度、STEP/BOM、隐藏面恢复、爆炸动画或任意物体通用图生 3D。

### Task 1: 建立 Qwen 请求契约与视觉规格校验

**Files:**
- Create: `demo-viewer/qwen_pump_vision.py`
- Create: `demo-viewer/test_qwen_pump_vision.py`

- [ ] **Step 1: 写缺少环境变量和合法响应的失败测试**

```python
class QwenPumpVisionTests(unittest.TestCase):
    def test_missing_key_fails_before_network(self):
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": ""}, clear=False):
            with self.assertRaisesRegex(PumpVisionError, "DASHSCOPE_API_KEY"):
                analyze_pump_image(Path("pump.png"), opener=lambda *_a, **_k: self.fail("network called"))

    def test_accepts_required_pump_components(self):
        spec = validate_pump_visual_spec(valid_pump_payload())
        self.assertEqual(spec["object_type"], "centrifugal_pump_assembly")
        self.assertGreaterEqual(len(spec["components"]), 3)
```

- [ ] **Step 2: 运行测试，确认因模块不存在而失败**

Run: `python -m unittest discover -s demo-viewer -p "test_qwen_pump_vision.py" -v`

Expected: `ModuleNotFoundError` 或缺少目标函数。

- [ ] **Step 3: 实现最小视觉规格类型和门禁**

```python
MODEL_ID = "qwen3.7-plus"
REQUIRED_KINDS = {"motor", "pump_casing", "base_plate"}
ALLOWED_KINDS = {
    "motor", "pump_casing", "inlet_flange", "outlet_flange",
    "base_plate", "support", "coupling", "fan_cover",
    "cooling_fin", "lifting_ring", "bolt",
}

class PumpVisionError(RuntimeError):
    pass

def validate_pump_visual_spec(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise PumpVisionError("Qwen response must be a JSON object")
    if payload.get("object_type") != "centrifugal_pump_assembly":
        raise PumpVisionError("image was not classified as a centrifugal pump assembly")
    confidence = float(payload.get("confidence", 0))
    if confidence < 0.65:
        raise PumpVisionError("pump recognition confidence is below 0.65")
    components = payload.get("components")
    if not isinstance(components, list):
        raise PumpVisionError("components must be an array")
    kinds = {item.get("kind") for item in components if isinstance(item, dict)}
    missing = REQUIRED_KINDS - kinds
    if missing:
        raise PumpVisionError(f"missing required pump components: {', '.join(sorted(missing))}")
    return payload
```

- [ ] **Step 4: 实现无第三方依赖的 OpenAI-compatible 请求**

请求体使用多模态 `messages[].content`：一项严格 JSON 指令、一项 Data URL 图片；超时 90 秒。Base URL 用 `rstrip('/') + '/chat/completions'`，只返回 `choices[0].message.content` 解析后的 JSON。HTTP 错误转换为不含密钥和图片内容的 `PumpVisionError("Qwen request failed with HTTP <code>")`。

```python
body = {
    "model": MODEL_ID,
    "messages": [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": [
            {"type": "text", "text": "Analyze this pump assembly and return JSON only."},
            {"type": "image_url", "image_url": {"url": image_data_url}},
        ]},
    ],
    "response_format": {"type": "json_object"},
    "enable_thinking": False,
    "stream": False,
}
```

- [ ] **Step 5: 补齐请求契约与脱敏测试**

测试 fake opener 捕获 `Request`，断言：模型名正确、图片字段存在、`enable_thinking` 为 false；模拟 401 时异常里不含假 Key 和 Base64。

- [ ] **Step 6: 运行测试**

Run: `python -m unittest discover -s demo-viewer -p "test_qwen_pump_vision.py" -v`

Expected: 所有测试 `OK`。

- [ ] **Step 7: 提交本任务**

```powershell
git add demo-viewer/qwen_pump_vision.py demo-viewer/test_qwen_pump_vision.py
git commit -m "feat: add qwen pump vision contract"
```

### Task 2: 把视觉规格转换为非方块泵组件树

**Files:**
- Create: `forge/stage2_spec/apply_pump_visual_spec.py`
- Create: `forge/stage2_spec/test_apply_pump_visual_spec.py`

- [ ] **Step 1: 写适配器失败测试**

```python
def test_replaces_placeholder_root_with_parameterized_parts(self):
    result = apply_pump_visual_spec(base_spec(), valid_pump_payload())
    components = result["componentTree"]
    self.assertNotIn("root", {item["id"] for item in components})
    self.assertTrue({"motor", "pump-casing", "base-plate"} <= {item["id"] for item in components})
    self.assertGreater(len({item["primitive"] for item in components}), 1)

def test_rejects_all_box_conversion(self):
    with self.assertRaisesRegex(PumpSpecError, "all-box"):
        assert_generated_pump_quality([{"id": "motor", "primitive": "box"}] * 6)
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python -m unittest discover -s forge/stage2_spec -p "test_apply_pump_visual_spec.py" -v`

Expected: 目标模块/函数不存在而失败。

- [ ] **Step 3: 实现部件到 Three.js 原语的固定映射**

```python
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
```

每个 component 生成完整的现有 schema 节点，至少包含：`id`、`name`、`level`、`role`、`importance`、`confidence`、`primitive`、`parent: "root"`、`dimensions`、`transform`、`actionProfile`、`material`、`fidelityTier`。额外保存：

```python
node["visualKind"] = source["kind"]
node["sourceConfidence"] = source["confidence"]
node["detachable"] = source["kind"] not in {"base_plate"}
```

- [ ] **Step 4: 实现材料和重复结构**

从视觉规格颜色生成 `painted-metal`、`dark-metal`、`bare-metal` 三种材料。`cooling_fin` 和 `bolt` 不展开成大量 Mesh，而转换为现有 `repetitionSystems`，限制数量分别为 `6..24` 和 `4..16`。

- [ ] **Step 5: 实现转换后质量门禁和 CLI**

`assert_generated_pump_quality()` 检查至少 6 个组件、必需 ID 存在、至少一个 cylinder/torus、不能全部 box。CLI 接收：

```text
python forge/stage2_spec/apply_pump_visual_spec.py BASE_SPEC.json VISION.json --out OUTPUT.json
```

写文件前完成全部验证；使用临时文件加 `Path.replace()`，避免留下半写规格。

- [ ] **Step 6: 测试适配结果可进入 Stage 3**

测试把适配后的完整 base spec 写入临时文件，并运行：

```python
with tempfile.TemporaryDirectory() as temp_dir:
    spec_path = Path(temp_dir) / "pump-spec.json"
    ts_path = Path(temp_dir) / "pump-model.ts"
    js_path = Path(temp_dir) / "pump-model.js"
    spec_path.write_text(json.dumps(result), encoding="utf-8")
    generate = subprocess.run([
        sys.executable, "forge/stage3_build/generate_threejs_factory.py",
        str(spec_path), "--out", str(ts_path), "--force",
    ], capture_output=True, text=True)
    self.assertEqual(generate.returncode, 0, generate.stderr)
    compiled = subprocess.run([
        "node", "demo-viewer/node_modules/esbuild/bin/esbuild",
        str(ts_path), "--format=esm", f"--outfile={js_path}",
    ], capture_output=True, text=True)
    self.assertEqual(compiled.returncode, 0, compiled.stderr)
    checked = subprocess.run(["node", "--check", str(js_path)], capture_output=True, text=True)
    self.assertEqual(checked.returncode, 0, checked.stderr)
```

Expected: 三条命令退出码均为 `0`，生成源码含 `new THREE.CylinderGeometry` 和 `new THREE.TorusGeometry`。

- [ ] **Step 7: 运行本任务测试并提交**

```powershell
python -m unittest discover -s forge/stage2_spec -p "test_apply_pump_visual_spec.py" -v
git add forge/stage2_spec/apply_pump_visual_spec.py forge/stage2_spec/test_apply_pump_visual_spec.py
git commit -m "feat: adapt pump vision into sculpt spec"
```

### Task 3: 把 Qwen 和泵适配器接入服务流水线

**Files:**
- Modify: `demo-viewer/server.py`
- Modify: `demo-viewer/test_server.py`

- [ ] **Step 1: 写可注入依赖的流水线测试**

将 `run_pipeline()` 增加仅供测试注入的关键字参数，而不是 mock 全局网络：

```python
def run_pipeline(
    image_path: str,
    object_name: str,
    *,
    vision_analyzer=analyze_pump_image,
    command_runner=subprocess.run,
) -> dict:
```

新增测试断言：

- analyzer 在 Stage 1 成功后被调用一次；
- `<object>-pump-vision.json` 写入输出目录；
- 适配器命令发生在 `new_sculpt_spec.py` 与 Stage 3 之间；
- analyzer 抛出 `PumpVisionError` 时不执行 Stage 2/3，最终 `modelPath` 为 `None`；
- Stage 3/编译失败不覆盖已存在的同名模型文件。

- [ ] **Step 2: 运行定向测试，确认失败**

Run: `python -m unittest discover -s demo-viewer -p "test_server.py" -v`

Expected: 新增的流水线测试失败，原有 TypeScript 编译测试仍通过。

- [ ] **Step 3: 接入视觉分析阶段**

在 Stage 1 后：

```python
generation_status.update(progress=25, message="Recognizing pump components with Qwen...")
visual_spec = vision_analyzer(Path(image_path))
vision_out = OUTPUT_PATH / f"{object_name}-pump-vision.json"
write_json_atomic(vision_out, visual_spec)
```

不要输出 `visual_spec` 全文或请求头到控制台。

- [ ] **Step 4: 在 Stage 2b 后应用视觉规格**

保留现有 pre-spec/sculpt-spec 生成以复用完整 schema；随后调用适配器覆盖占位组件树：

```python
adapt_cmd = [
    sys.executable, "-u",
    str(FORGE_PATH / "stage2_spec" / "apply_pump_visual_spec.py"),
    str(spec_out), str(vision_out), "--out", str(spec_out),
]
```

适配成功后才把进度更新到 75 并执行 Stage 3。所有异常沿用统一 error 状态，不能继续默认 box 路径。

- [ ] **Step 5: 运行服务端测试**

Run: `python -m unittest discover -s demo-viewer -p "test_*.py" -v`

Expected: 全部 `OK`。

- [ ] **Step 6: 提交本任务**

```powershell
git add demo-viewer/server.py demo-viewer/test_server.py
git commit -m "feat: gate model generation on qwen pump analysis"
```

### Task 4: 增加可测试的部件选择和高亮

**Files:**
- Create: `demo-viewer/src/component-selection.js`
- Create: `demo-viewer/test_component_selection.mjs`
- Modify: `demo-viewer/package.json`

- [ ] **Step 1: 写选择目标解析测试**

使用轻量假对象，不启动 WebGL：

```javascript
test('finds the nearest sculpt component ancestor', () => {
  const pivot = { userData: { sculptComponent: { id: 'motor', name: 'Motor', visualKind: 'motor' } }, parent: null };
  const mesh = { userData: {}, parent: pivot };
  assert.equal(findSelectableAncestor(mesh), pivot);
});

test('ignores objects without sculpt metadata', () => {
  assert.equal(findSelectableAncestor({ userData: {}, parent: null }), null);
});
```

- [ ] **Step 2: 增加测试脚本并确认失败**

```json
"scripts": {
  "test": "node --test test_component_selection.mjs",
  "dev": "vite",
  "build": "vite build",
  "preview": "vite preview"
}
```

Run: `npm test --prefix demo-viewer`

Expected: 模块不存在而失败。

- [ ] **Step 3: 实现选中状态函数**

导出：`findSelectableAncestor(object)`、`selectComponent(object)`、`clearComponentSelection()`。高亮只临时修改 emissive，首次选择时保存原值；切换/清空时完整恢复。返回供 UI 显示的纯数据：

```javascript
return {
  id: component.id,
  name: component.name,
  kind: component.visualKind || component.role,
  confidence: component.sourceConfidence ?? component.confidence,
};
```

- [ ] **Step 4: 运行测试并提交**

```powershell
npm test --prefix demo-viewer
git add demo-viewer/src/component-selection.js demo-viewer/test_component_selection.mjs demo-viewer/package.json
git commit -m "feat: add selectable pump component state"
```

### Task 5: 接入射线拾取和部件信息面板

**Files:**
- Modify: `demo-viewer/src/main.js`
- Modify: `demo-viewer/index.html`
- Modify: `demo-viewer/src/style.css`

- [ ] **Step 1: 在页面添加默认隐藏的信息面板**

```html
<aside id="component-info" class="hidden" aria-live="polite">
  <strong id="component-name"></strong>
  <span id="component-kind"></span>
  <span id="component-confidence"></span>
</aside>
```

并将控制提示增加“Click component: Select / Click empty space: Clear selection”。

- [ ] **Step 2: 在 `main.js` 增加射线检测**

使用 `renderer.domElement.getBoundingClientRect()` 计算标准化指针坐标，只检测当前生成模型的后代，忽略 grid/axes。鼠标拖动旋转不得误触选择：`pointerdown` 保存坐标，`pointerup` 位移小于 4px 才执行 raycast。

- [ ] **Step 3: 处理模型生命周期**

- `addModel()` 保存 `currentModelRoot`；
- `clearScene()` 先调用 `clearComponentSelection()`，再正确从父节点移除嵌套 Mesh/Group；
- 选择时更新信息面板；空白点击隐藏面板；
- wireframe 切换后仍能恢复选择高亮，不覆盖生成器保存的原材质。

- [ ] **Step 4: 静态和构建验证**

```powershell
npm test --prefix demo-viewer
npm run build --prefix demo-viewer
node --check demo-viewer/src/main.js
```

Expected: 测试通过、Vite build 成功、语法检查退出码为 `0`。

- [ ] **Step 5: 提交本任务**

```powershell
git add demo-viewer/src/main.js demo-viewer/index.html demo-viewer/src/style.css
git commit -m "feat: make generated pump parts interactive"
```

### Task 6: 全量自动化验证与一次真实 Qwen 验收

**Files:**
- Modify only if a failing assertion proves a defect in files from Tasks 1-5.

- [ ] **Step 1: 确认凭据只检查存在性，不打印值**

```powershell
@{
  ApiKeySet = [bool]$env:DASHSCOPE_API_KEY
  BaseUrlSet = [bool]$env:DASHSCOPE_BASE_URL
  BaseUrlMatchesCompatibleMode = $env:DASHSCOPE_BASE_URL -match '/compatible-mode/v1/?$'
}
```

Expected: 三项均为 `True`。

- [ ] **Step 2: 运行全部相关测试**

```powershell
python -m unittest discover -s demo-viewer -p "test_*.py" -v
python -m unittest discover -s forge/stage2_spec -p "test_apply_pump_visual_spec.py" -v
npm test --prefix demo-viewer
npm run build --prefix demo-viewer
git diff --check
```

Expected: Python/Node/Vite 全通过，`git diff --check` 无输出。

- [ ] **Step 3: 启动本地服务并上传确认过的泵图片一次**

Run: `python demo-viewer/server.py`

浏览器验收：

- 状态依次经过图片探测、Qwen 部件识别、规格适配、Three.js 生成；
- 生成结果明显包含电机圆柱体、泵壳、两个法兰、底座和支撑，不出现中央默认立方体；
- 点击电机、泵壳、法兰至少三个部件，均能独立高亮并显示不同 ID；
- 点击空白后高亮清除；
- 控制台无语法错误，服务端日志无密钥/Base64。

- [ ] **Step 4: 检查生成证据而不提交生成物**

确认以下临时输出存在且结构有效：

- `demo-viewer/output/<object>-pump-vision.json`
- `demo-viewer/output/<object>-sculpt-spec.json`
- `demo-viewer/src/create<object>Model.js`

除非仓库现有策略明确跟踪生成物，否则不 `git add` 这些文件。

- [ ] **Step 5: 最终范围审查**

Run: `git status --short` 和 `git diff --stat HEAD~5..HEAD`

确认没有修改用户现有的 `forge/stage3_build/generate_threejs_factory.py` 工作区改动，也没有加入 API Key、上传图片或无关文件。

## 实施注意事项

- 本计划不增加 OpenAI Python SDK 或 JSON Schema 依赖；现有项目可直接运行。
- API 模型只做受约束的视觉语义抽取，不直接生成 JavaScript，减少不可控语法错误和任意代码风险。
- 单图无法恢复不可见侧面的真实机械结构；首版输出是“视觉近似的参数化教学演示”，不是制造级 CAD。
- 所有新提交都应使用精确文件列表暂存，避免把当前未跟踪的 `DEPLOYMENT.md`、`start-viewer.ps1` 或其他用户文件带入提交。

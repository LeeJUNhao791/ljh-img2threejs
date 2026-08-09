# 千问视觉驱动的参数化泵演示设计

日期：2026-08-09
状态：已确认方案，待用户审阅后实施

## 目标

将单张工业离心泵+电机参考图转换为可演示的 Three.js 参数化装配，而不是输出默认方块。

首版成功定义：对清晰的泵总成图片，生成至少电机、泵壳、入口法兰、出口法兰和底座五类独立部件；用户可旋转、缩放、线框查看，并点击部件获得高亮和名称提示。

## 非目标

- 不生成或声称生成 STEP、IGES、B-rep、BOM、尺寸公差或可制造 CAD。
- 不恢复不可见的叶轮、背面、内部流道或真实工程尺寸。
- 不在首版接入图生 GLB 网格服务、爆炸动画或多模型供应商切换。
- 不在代码、日志、响应或仓库中保存 API Key。

## 方案选择

采用方案 A：`qwen3.7-plus` 视觉理解 + 严格 JSON + 离心泵参数化适配器。

选择理由：首版需要可拆分的语义组件和稳定交互，而不是缺乏部件语义的单体网格。视觉模型用于识别与结构化推断；现有 Three.js 工厂用于受约束的程序化几何生成。

## 架构与数据流

```text
浏览器上传图片
  -> POST /api/generate
  -> 保存原图
  -> qwen3.7-plus 图片理解（非思考模式、JSON 输出）
  -> PumpVisualSpec JSON Schema 校验
  -> PumpSpecAdapter 转换为 ObjectSculptSpec
  -> 现有 Stage 3 Three.js 生成与 esbuild 编译门禁
  -> 浏览器加载 ESM 模型，点击部件高亮
```

### 凭据与模型配置

- `DASHSCOPE_API_KEY`：必需，只由服务端进程读取。
- `DASHSCOPE_BASE_URL`：必需，必须是同一百炼业务空间的 OpenAI Compatible `.../compatible-mode/v1` 地址。
- 模型固定为 `qwen3.7-plus`。
- 请求不启用思考模式，要求 JSON 输出；服务端仍执行 JSON Schema 校验。
- 日志仅记录请求 ID、模型名、延迟和校验结果，绝不记录 Authorization、Key、Base URL 或原始图片 Base64 数据。

## PumpVisualSpec 合约

视觉模型必须返回 JSON 对象，包含：

```json
{
  "objectType": "centrifugal_pump_motor_assembly",
  "overallConfidence": 0.0,
  "view": "three_quarter",
  "components": [
    {
      "kind": "motor|pump_casing|inlet_flange|outlet_flange|base_plate|support|coupling|fan_cover|cooling_fin|lifting_ring|bolt",
      "visible": true,
      "confidence": 0.0,
      "relativePosition": [0, 0, 0],
      "relativeScale": [1, 1, 1],
      "orientation": "x|y|z",
      "count": 1,
      "notes": "image-supported observation only"
    }
  ],
  "unknowns": ["back-side geometry is inferred"]
}
```

数值均为相对比例，不能被解释为毫米或工程尺寸。

## 质量门禁

在调用 Stage 3 前拒绝以下结果：

- 未配置 Key 或 Base URL。
- API 请求失败、超时、非 JSON 或 JSON Schema 不通过。
- `objectType` 不是离心泵电机总成。
- `overallConfidence < 0.65`。
- 缺少 `motor`、`pump_casing`、`base_plate` 中任一关键部件。
- 转换后仍只有 `root` 或只有 `box` 组件。

拒绝时 API 返回明确错误状态和可操作原因；绝不发布默认方块模型。

## 参数化泵适配器

适配器将视觉 JSON 映射为受限几何组件：

| 视觉组件 | Three.js 组合 |
|---|---|
| 电机 | 圆柱主体、端盖、风罩、散热片实例阵列、吊环 |
| 泵壳 | 旋转体/圆柱体组合、前盖、轴端 |
| 入口/出口 | 管段、法兰盘、孔阵列 |
| 联轴器 | 轴与短圆柱联轴器 |
| 底座与支架 | 底板、支脚、斜撑、地脚螺栓 |

每个组件拥有稳定 ID、名称、`Object3D` 节点和 `userData` 元数据。模型可含“推断”标记，供 UI 提示用户图片未覆盖的部位。

## 前端交互

- 保留现有旋转、缩放、线框和背景控制。
- 鼠标点击通过 Raycaster 选中最近的命名部件网格。
- 选中部件使用临时高亮材质或边框，不破坏原始材质。
- 显示部件中文名称、类型及“可见/推断”状态。
- 重新加载或选中空白处时清除高亮。

## 错误处理

| 情况 | 用户可见结果 |
|---|---|
| 凭据未配置 | “未配置百炼视觉服务，请设置环境变量后重启服务。” |
| 千问请求失败或超时 | “视觉识别失败，未生成模型；可重试。” |
| 图片不是可识别的泵总成 | “图片未识别为离心泵电机总成，未生成方块替代物。” |
| 识别置信度不足或关键构件缺失 | “无法生成可信泵模型；请提供更清晰的 3/4 视角或补充多张图片。” |

## 测试与验收

1. 单元测试：环境变量缺失时不发出网络请求。
2. 单元测试：有效的泵视觉 JSON 映射为至少五类组件，且不产生 root-only spec。
3. 单元测试：无效 JSON、低置信度或缺失关键构件被拒绝，不生成 JS 模型。
4. 单元测试：点击选择和清除选择不会破坏原始材质。
5. 集成测试：模拟千问 HTTP 响应，验证请求包含图片、`qwen3.7-plus`、非思考模式和 JSON 输出要求。
6. 真实验收：使用用户提供的泵图调用已配置的百炼服务一次；检查 API 成功、模型可加载、至少五个可点击部件可见。

## 发布边界

首版仅适用于“参数化视觉演示”。界面和 API 结果必须明确包含“相对比例/视觉推断，非工程 CAD”的边界说明。

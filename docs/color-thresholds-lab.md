# 颜色阈值智能调优（/tools/color_thresholds）设计与接口契约

> 本文是页面 `/tools/color_thresholds` 与后端 `app/color_lab.py` 之间的契约。
> 页面与后端可以并行开发，但 **本文件中的字段名、枚举值、错误码不得单方面修改**；
> 如需变更先改本文件。

## 1. 目标与完整工作流

旧的「按时间点挑选历史分析结果 + 打 tag」流程全部移除。新流程只围绕「导入的卫星图片」：

```
导入卫星图片（单张 / 多张 / ZIP 批量）
  → 后端自动缩放并生成分析输入（source.png、缩略图）
  → 与监控范围同比例的图片按 GeoJSON 生成陆地遮罩（陆地不参与分析）
  → 用「当前生效的 HSV 参数」自动分类渲染（云层 / 蓝海 / 黄海）
  → 用户发现某块区域分错了：鼠标悬停查看色块信息，点击那个色块
  → 选择它真实的类别（云层 / 蓝海 / 黄海 / 忽略）
  → 后端把所有图片上的纠正样本汇总，自动搜索更优的 HSV 参数
  → 页面回填建议参数并实时重算，用户确认后「保存并立即生效」（写 app_settings，生产立即采用）
```

关键概念：

- **色块（color block）**：后端在导入时用 Lab 空间聚类（K=14）+ 连通域把图片切成一块块
  颜色相近的连通区域。用户点击结果图时，命中的是整个色块，而不是单个像素。
- **陆地遮罩（land mask）**：导入图若与监控范围（`config.TARGET_AREA`）长宽比一致
  （容差 `config.TOOL_GEO_MASK_ASPECT_TOLERANCE`），导入时按 `config.GEOJSON_PATH`
  生成与图片同尺寸的海洋蒙版，**陆地（mask=0）从色块划分、分类与指标统计中排除**，
  分类图中渲染为深灰。比例不一致的图不硬套遮罩，以免把画面切错。
- **纠正（correction）**：`{block_id, label}`。label 取值 `CLOUD` / `BLUE_WATER` /
  `YELLOW_WATER` / `IGNORE`（忽略 = 非海域/陆地，排除出指标统计）。
- **自动优化**：把全部导入图片上的纠正色块像素当作**用户标注样本**，同时从同一批图中
  采样**未标注像素**（目标 = 当前阈值的判定，权重各 50%），用坐标下降法搜索
  CLOUD / BLUE_WATER 的 HSV 阈值，最大化「标注准确率」同时尽量不改变未标注区域的
  既有判定（避免为了几处纠正把大片区域改错）。同分时优先保留离当前参数更近的解。
  黄海（YELLOW_WATER）没有独立阈值，它是「非云、非蓝」的兜底类。

## 2. 页面结构（前端按此实现）

单文件内联 CSS/JS（与现状一致，不依赖 `/static`），浅色仪表盘风格，沿用现有 CSS 变量与组件。

### 顶栏
- 标题：`颜色阈值智能调优`；副标题说明「导入卫星图 → 点击色块纠正 → 自动优化参数 → 保存即生效」。
- 右侧/第二行状态 chip：生效来源（数据库/默认值）、更新时间、`未保存修改` 徽标、保存提示。
- 导航链接：`← HSV 批量调试器`（/tools/hsv_tuner）、`API 文档`（/docs）。
- 错误提示条（可关闭，逐条列出 details）。

### 左栏（sticky）
1. 阈值控制组：`CLOUD 云层` 与 `BLUE_WATER 蓝海` 两组，各含 H/S/V 的 lower/upper
   滑块 + 数字输入 + 阈值带可视化 + 「载入默认值」。（沿用现有实现与样式）
2. 说明：`黄海 = 既不是云、也不是蓝水的海洋像素（由上面两组阈值共同决定）`。
3. 底部动作条：`保存并立即生效` / `载入默认值（不保存）` / `恢复内置默认`；
   校验失败时逐条展示错误；`Ctrl/⌘+S` 保存；保存成功后清空 dirty。

### 右栏
**卡片 1「1 · 导入卫星图片」**
- 拖放区 + 文件选择（`multiple`），`accept` 为
  `.png,.jpg,.jpeg,.webp,.bmp,.tif,.tiff,.zip`。
- 提示：单次 ≤ 60 MB、最多保留 60 张、图片会等比缩放到最长边 1600 px 后分析；
  支持 png/jpg/webp/bmp/tif，ZIP 内按条目批量导入。
- 导入结果：`已导入 N 张，跳过 M 张` + 跳过原因（可折叠列出，最多 8 条）。
- 已导入图片横向缩略图条：缩略图、文件名、`已纠正 N` 徽标、删除按钮；
  点击选中图片；选中项高亮。空态提示「还没有导入图片」。
- 「清空全部」按钮（需确认；`confirm()` 即可）。

**卡片 2「2 · 色块纠正」**
- 头部：当前图片名 + 尺寸；徽标：色块数 / `陆地已遮罩`（仅套用遮罩时显示）/ 已纠正数 /
  忽略占比 / 纠正符合率。
- 两图并排：
  - 左：`分析输入`（source_preview，只读）。
  - 右：`HSV 分类结果`（classification，**可点击**）。鼠标样式 `crosshair`；
    未选图时显示空态。陆地/无效区域渲染为深灰，不参与分类。
- **悬停查看**：鼠标移到分类图上（防抖约 110 ms）会浮出信息提示，跟随光标显示
  `色块 #id`、像素数/占比、均值 HSV / RGB、当前判定、已有纠正；悬停不改变选中状态，
  也不触发重绘。命中陆地/纯黑区域或碎片块时提示原因。
- 点击分类图 → 请求 `POST .../block`，在图上叠加选中色块描边，并弹出「色块操作浮层」
  （固定在图片右下角或跟随点击位置均可）：
  - 显示 `色块 #id`、像素数、占比、均值 HSV / RGB、当前判定类别；
  - 四个按钮：`云层` `蓝海` `黄海` `忽略`；已纠正的色块显示 `取消纠正`；
  - 快捷键 `1/2/3/4` 选择对应类别，`Esc` 关闭浮层；
  - 浮层上注明：`标注会作为自动优化的训练样本`。
- 纠正列表（图片下方 chips）：每项显示类别色点 + `#block_id` + 像素占比 + 删除（×）；
  另有 `清空本图纠正`。
- 自动优化区：
  - 复选框 `标注后自动优化参数`（默认勾选）；
  - 按钮 `立即优化`；
  - 结果面板：`准确率 78.4% → 96.2%`、样本数、各类样本数、参数变化表
    （类别/通道/旧值→新值）、提示语；按钮 `应用建议值并预览`（自动模式下也保留，
    用于重新应用）；若 `ok=false` 显示 `message`（如「至少需要标注两种类别」）。
- 标注说明文字：`绿色描边 = 当前参数判定与纠正一致；橙色描边 = 不一致（优化目标）；
  灰色描边 = 已标记为忽略；深灰 = 陆地/无效区域（已遮罩）`。

**卡片 3「分析指标」**：沿用现有指标网格；文案改为「分析像素」（= totalOceanPixels），
新增「纠正符合率」「忽略占比」两个指标；含义不明的指标显示 `—`。

### 交互细节
- 导入中显示进度（按钮 loading + 拖放区遮罩）；并发上传按文件逐个 `POST`，串行。
- 预览请求防抖 180 ms，`AbortController` 取消旧请求；用递增序号防止旧响应覆盖新结果。
- 保存失败：错误条 + toast；校验错误定位到第一个非法输入。
- 所有枚举中文映射：
  `CLOUD→云层`、`BLUE_WATER→蓝海`、`YELLOW_WATER→黄海`、`IGNORE→忽略`。

## 3. 接口契约

Base：`/tools/api/color_thresholds`

通用约定：
- 成功：HTTP 200 `{"success": true, "data": ...}`。
- 失败：HTTP 400/404/413/500 `{"success": false, "error": "中文说明", "details": ["逐条错误"]}`
  （`details` 可省略）。
- 所有图片 id 形如 `^[0-9a-f]{12}$`。

### 3.1 读取阈值（沿用，不变）
`GET /tools/api/color_thresholds`
```json
{"success": true, "data": {
  "ranges": {"CLOUD": {"lower": [0,0,121], "upper": [179,40,255]},
             "BLUE_WATER": {"lower": [30,20,0], "upper": [130,255,255]}},
  "defaults": { "...": "..." },
  "source": "db",
  "updated_at": "2026-10-28T14:00:00+08:00",
  "bounds": {"H": [0,179], "S": [0,255], "V": [0,255]},
  "classes": ["CLOUD", "BLUE_WATER"]
}}
```

`POST /tools/api/color_thresholds` body `{"ranges": {...}}`：校验+保存（沿用）。
`POST /tools/api/color_thresholds/reset`：恢复默认（沿用）。

### 3.2 实验台初始状态
`GET /tools/api/color_thresholds/lab`
```json
{"success": true, "data": {
  "images": [ImageSummary, "..."],
  "limits": {
    "max_images": 60,
    "max_upload_bytes": 62914560,
    "max_side": 1600,
    "zip_max_entries": 120,
    "allowed_ext": [".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"]
  },
  "labels": [
    {"key": "CLOUD", "label": "云层"},
    {"key": "BLUE_WATER", "label": "蓝海"},
    {"key": "YELLOW_WATER", "label": "黄海"},
    {"key": "IGNORE", "label": "忽略"}
  ],
  "corrections_total": 12
}}
```

`ImageSummary`：
```json
{
  "id": "a1b2c3d4e5f6",
  "name": "clear_sky_01.png",
  "width": 1600,
  "height": 1600,
  "thumb_url": "/data/tool_uploads/color_thresholds/a1b2c3d4e5f6/thumb.jpg",
  "created_at": "2026-10-28T14:30:00+08:00",
  "source": "file",              // 或 "zip:sub/dir/cloudy_02.png"
  "correction_count": 3,
  "block_count": 148,
  "ignored_ratio": 0.12,
  "land_masked": true            // 是否套用了监控范围的陆地遮罩
}
```

### 3.3 导入图片
`POST /tools/api/color_thresholds/images?filename=<URL 编码文件名>`

- 请求体：原始文件二进制（`Content-Type: application/octet-stream`，服务端不解析多段表单）。
- 服务端按魔数/扩展名识别 ZIP；ZIP 内条目逐个导入。
- 超过 `max_upload_bytes` → 413；既不是图片也不是 ZIP → 400；已达图片上限 → 400（中文提示先删除）。
```json
{"success": true, "data": {
  "imported": [ImageSummary, "..."],
  "skipped": [{"name": "readme.txt", "reason": "不是支持的图片格式"}],
  "truncated": false,
  "selected": "a1b2c3d4e5f6"
}}
```

`DELETE /tools/api/color_thresholds/images/{image_id}`
```json
{"success": true, "data": {"deleted": true, "images": [ImageSummary, "..."]}}
```
`POST /tools/api/color_thresholds/images/clear`
```json
{"success": true, "data": {"deleted": 3, "images": []}}
```

### 3.4 预览（用给定/当前阈值渲染一张图）
`POST /tools/api/color_thresholds/preview`
```json
// 请求
{"image_id": "a1b2c3d4e5f6",
 "ranges": null,                         // 可选：null/缺省 = 用当前生效阈值
 "selection": {"block_id": 37},          // 可选：高亮选中的色块
 "show_corrections": true}               // 可选，默认 true
```
```json
// 响应
{"success": true, "data": {
  "image": {
    "classification": "data:image/png;base64,...",
    "source_preview": "data:image/png;base64,..."
  },
  "width": 1000, "height": 1000,          // 返回图尺寸（最长边 ≤1000）
  "full_width": 1600, "full_height": 1600,
  "metrics": {
    "status": "completed", "seaBlueness": 0.71, "cloudCoverage": 0.12,
    "bluenessIndex": 0.62, "bluePercentage": 0.62, "yellowPercentage": 0.26,
    "visibilityKm": 22.4, "visibilityLevel": "良好", "hazeScore": 0.31,
    "bluePixels": 100000, "yellowPixels": 42000, "cloudPixels": 19000,
    "totalOceanPixels": 161000
  },
  "analysis": {
    "analyzed_pixels": 161000, "auto_excluded_pixels": 500,
    "ignored_pixels": 12000, "ignored_ratio": 0.046,
    "total_pixels": 256000, "block_count": 148, "correction_accuracy": 0.93,
    "land_masked": true, "land_pixels": 84000
  },
  "corrections": [Correction, "..."],
  "elapsedMs": 87
}}
```
说明：分类图中已叠加纠正描边/角标；`correction_accuracy` 为各纠正色块中
「当前参数判定 == 纠正类别」的像素占比（无纠正或全为忽略时为 `null`）。
`corrections` 是**当前图片的完整纠正明细**（元素结构与 3.6 相同），
页面切图 / 刷新 / 改阈值重算后可直接用它恢复底部 chips，不必再发额外请求。
`analysis.ignored_ratio` = 忽略像素 / 图片总像素（与 ImageSummary.ignored_ratio 同口径）。
`analysis.auto_excluded_pixels` = 纯黑 + 陆地等无效像素；其中陆地部分单独放在
`analysis.land_pixels`（未套用遮罩时为 0），`analysis.land_masked` 表示是否套用。
`width`/`height` 是**返回图尺寸**：小图会等比放大到 `config.TOOL_PREVIEW_MIN_SIDE`
（≤ `TOOL_PREVIEW_MAX_SIDE`）便于悬停/点击，坐标仍按归一化值回传；
`full_width`/`full_height` 始终是分析原图尺寸。

注意：`ImageSummary.block_count` 在图片尚未被预览/点击过（色块尚未惰性计算）时为 `null`；
点击色块时 `block.predicted` 也可能为 `null`（该色块已被标记忽略时）。

### 3.5 点击色块 / 悬停查询
`POST /tools/api/color_thresholds/block`
```json
// 请求：x/y 为相对返回图（data URL 那张）的归一化坐标 0~1；
// preview=false 供鼠标悬停复用：只查色块信息，不重新渲染预览图
{"image_id": "a1b2c3d4e5f6", "x": 0.42, "y": 0.61, "ranges": null, "preview": true}
```
```json
// 响应（preview=true）
{"success": true, "data": {
  "block": {
    "block_id": 37,
    "pixel_count": 8123,
    "pixel_ratio": 0.031,
    "bbox": [820, 640, 940, 710],          // 全图像素坐标 [x0,y0,x1,y1]
    "mean_hsv": [112, 96, 74],
    "mean_rgb": [24, 78, 112],
    "predicted": "BLUE_WATER",
    "predicted_label": "蓝海",
    "label": null,                          // 已保存的纠正（无则 null）
    "label_label": null
  },
  "preview": { "classification": "data:image/png;base64,...",
               "source_preview": "data:image/png;base64,..." },
  "width": 1000, "height": 1000
}}
```
```json
// 响应（preview=false，悬停查询）：不含 preview/width/height，响应通常 < 50 ms
{"success": true, "data": {
  "block": { "block_id": 37, "...": "与上面同结构" },
  "reason": null
}}
```
点击无效像素（纯黑 / 陆地遮罩区域 / 已被标为忽略 / 碎片块）时 `block` 为 `null`，
并给出 `reason` 中文说明；HTTP 仍为 200。`preview=false` 时同样返回 `block=null` + `reason`。

### 3.6 保存 / 取消纠正
`POST /tools/api/color_thresholds/corrections`
```json
// 请求：label 为 null 表示取消该色块的纠正
{"image_id": "a1b2c3d4e5f6", "block_id": 37,
 "label": "BLUE_WATER",                 // CLOUD | BLUE_WATER | YELLOW_WATER | IGNORE | null
 "auto_optimize": true,
 "ranges": null}                        // 可选：页面当前表单里的阈值（可能未保存），
                                        // 作为预览与自动优化的起点；缺省用已保存阈值
```
```json
// 响应
{"success": true, "data": {
  "corrections": [{
    "block_id": 37, "label": "BLUE_WATER", "label_cn": "蓝海",
    "pixel_count": 8123, "pixel_ratio": 0.031,
    "mean_hsv": [112, 96, 74], "mean_rgb": [24, 78, 112],
    "bbox": [820, 640, 940, 710],
    "current_predicted": "BLUE_WATER",
    "matches": true,
    "created_at": "2026-10-28T14:31:02+08:00"
  }],
  "preview": { "classification": "...", "source_preview": "..." },
  "width": 1000, "height": 1000,
  "metrics": { "...": "..." },
  "analysis": { "...": "..." },
  "correction_accuracy": 0.93,
  "optimize": OptimizeResult | null
}}
```
`auto_optimize=true` 时后端会跑一次自动优化（约 1~3 s），把结果放在 `optimize`。

`POST /tools/api/color_thresholds/corrections/clear`
```json
{"image_id": "a1b2c3d4e5f6", "ranges": null}   // 可选；缺省 = 清空全部图片的纠正
```
```json
{"success": true, "data": {
  "cleared": 5, "corrections_total": 2,
  // 指定了 image_id 时额外返回（与 3.6 同形的预览，页面无需再发 /preview）：
  "preview": {"classification": "...", "source_preview": "..."},
  "width": 1000, "height": 1000,
  "metrics": {"...": "..."}, "analysis": {"...": "..."},
  "corrections": [], "correction_accuracy": null
}}
```

### 3.7 自动优化
`POST /tools/api/color_thresholds/optimize`
```json
{"image_id": "a1b2c3d4e5f6",   // 可选，仅用于提示，优化始终使用全部图片的纠正
 "ranges": null}               // 可选：优化起点/对照基准（页面当前表单值），缺省用当前生效阈值
```
```json
{"success": true, "data": {
  "optimize": OptimizeResult,
  "images_used": 3,
  "corrections_used": 7
}}
```

`OptimizeResult`：
```json
{
  "ok": true,
  "improved": true,
  "accuracy_before": 0.784,
  "accuracy_after": 0.962,
  "samples": 18422,
  "samples_unlabeled": 15000,
  "class_counts": {"CLOUD": 5000, "BLUE_WATER": 8000, "YELLOW_WATER": 5422},
  "unlabeled_accuracy_before": 1.0,
  "unlabeled_accuracy_after": 0.963,
  "images_used": 3,
  "corrections_used": 7,
  "suggested": {
    "CLOUD": {"lower": [0, 0, 118], "upper": [179, 34, 255]},
    "BLUE_WATER": {"lower": [82, 48, 0], "upper": [134, 255, 196]}
  },
  "diffs": [
    {"class": "CLOUD", "class_label": "云层", "side": "upper", "channel": "S",
     "from": 40, "to": 34},
    {"class": "BLUE_WATER", "class_label": "蓝海", "side": "lower", "channel": "H",
     "from": 30, "to": 82}
  ],
  "message": "已根据 7 处纠正优化参数，标注像素准确率 78.4% → 96.2%；未标注区域保真度 100.0% → 96.3%"
}
```
- `accuracy_before/after` 是**用户标注像素**上的准确率；`unlabeled_accuracy_*` 是
  未标注像素保持当前判定的比例（页面可展示，也可以忽略）。
- 无纠正样本 / 只标注了一种类别 / 样本像素过少：`ok=false`，`improved=false`，
  `suggested` 为当前生效阈值，`message` 为中文原因（如 `至少需要标注两种类别（当前只有 云层）`）。
- `improved=false` 且 `ok=true` 表示当前参数已经足够好：`suggested` 回填当前值、
  `diffs=[]`，`message` 形如 `当前参数已能较好满足标注（准确率 98.1%），未做明显调整`。

## 4. 后端实现约束（供自测/验收）

- 导入图片落盘：`data/tool_uploads/color_thresholds/<id>/source.png`、
  `thumb.jpg`、`meta.json`、`corrections.json`、`blocks.png`、`block_stats.json`。
  其中 `source.png` 是分析输入（最长边 ≤1600 px 的 RGB）。
- 分析区域：默认全图，但会排除 `RGB 最大通道 ≤ 3` 的纯黑像素；与监控范围
  （`config.TARGET_AREA`）长宽比一致的图片还会按 `config.GEOJSON_PATH` 生成
  **陆地遮罩**，陆地不进入色块划分、分类与指标统计。标为 `IGNORE` 的色块同样排除出指标；
  页面与接口仍可看到这些区域（分类图中渲染为深灰）。
  遮罩口径记录在 `block_stats.json` 的 `mask` 字段里，口径变化时旧色块缓存自动失效重建。
- 指标复用 `processor.classify_hsv` / `processor.render_classification_map` /
  `processor.compute_ocean_color_metrics`，分析区域为去掉纯黑与陆地后的海洋区域，
  与生产海洋蒙版口径一致。
- 自动优化不得写入数据库；只有用户点「保存并立即生效」才通过既有
  `POST /tools/api/color_thresholds` 落库。
- 单张图导入后在 1~2 s 内可预览；点击色块与保存纠正响应 < 500 ms（不含优化）；
  自动优化单次 < 5 s（CPU 环境）。
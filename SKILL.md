# HICUSTOM Product Info Extractor

从 hicustom.com / jit.hicustom.com（指纹科技）商品详情页通过 **API 拦截**提取结构化商品信息。

## 触发条件

用户提供 hicustom.com 商品详情页 URL 时触发：
- "帮我提取这个 hicustom 商品的详情"
- "获取这个商品的信息" + hicustom URL
- "拉取 hicustom 的商品数据"

## 页面架构

| 层面 | 技术 | 说明 |
|------|------|------|
| 登录页 | 服务端渲染 + jQuery | 微信扫码/账号/手机号/QQ 登录 |
| 外层壳 | Vue.js SPA (static.hihumbird.com) | Ant Design 布局 + 导航 |
| 商品内容 | wujie 微前端 iframe | `<iframe name="fnsz-sale">` 嵌套渲染 |
| 数据层 | REST API (apigw.hihumbird.com) | 3 个关键端点（见下方） |

**必须登录才能访问商品详情。**

## 提取字段

| 字段 | 来源 API | 路径 |
|------|---------|------|
| `product_name` | spu-itg | `data.name` |
| `unit_price` | spu-itg | `data.skus[0].sku_price_template.price_level_factors[0].calculate_value` (C级=1件) |
| `estimated_shipping` | spu_freight | `data[0].freight` + `.method_name` + `.country_code` |
| `weight` | spu-itg | `data.skus[0].weight` (g) |
| `color_variants` | spu-itg | `data.attribute_items` type=1 |
| `size_variants` | spu-itg | `data.attribute_items` type=2 |
| `product_details` | spu-itg | `data.extra.spu_features.{material_description, performance, ...}` — 自动剔除：商品编码、底款编码、"默认工艺路线" |
| `package_specs` | product/styles | `data.skus[0].{length, width, height}` (英寸) + `weight`/`net_weight` (g) |
| `images` | spu-itg | `data.images[].file_path` |

### 额外输出
- `price_tiers`: 完整价格阶梯 (C/V1-V5, 1-∞件)
- `blank_code`: 白品编码
- `style_code`: 款式编码
- `factory`: 工厂名称
- `category`: 品类路径（见下方品类提取规则）
- `delivery_period_hours`: 出货周期

## 品类提取规则（铁律）

| 优先级 | 来源 | 说明 |
|--------|------|------|
| 1 | 页面 API `data.category` | 直接使用，不做修改 |
| 2 | 页面 DOM 品类文本 | 直接使用，不做修改 |
| 3 | LLM 分析兜底 | 以上皆无时，主控 Agent 根据商品名称/详情推断品类 |

> **强制规则**: 页面有品类则禁止 LLM 分析。采集阶段完成品类填充，不下沉到 Router 处理。

## 三个关键 API 端点

| 端点 | URL 模式 | 返回 |
|------|---------|------|
| SPU 商品 | `apigw.hihumbird.com/spu-itg/uct/v1/spus/{spu_id}` | 名称/价格/变体/详情/图片/工厂 |
| Style 工艺 | `apigw.hihumbird.com/product/uct/v1/styles/{style_id}` | 包装尺寸/净重/毛重 |
| 最低运费 | `apigw.hihumbird.com/spu/v1/spu_freight/lowest_freight` | 运费/物流方式 |

**数据结构**: 响应格式为 `{result_code, msg, data: {...}}`，商品数据在 `data` 直接层级。

## 使用方式

### 🚀 一键同步（推荐）

```bash
# 提取商品信息 → 交互式运费试算(默认) → 上传图片 → 写入飞书多维表格
python scripts/sync_to_feishu.py "https://jit.hicustom.com/merchant/.../productDetail?id=xxx"

# 自定义邮编
python scripts/sync_to_feishu.py "URL" --zip 10001

# 强制重新登录（会话过期时）
python scripts/sync_to_feishu.py "URL" --force-login

# 关闭交互式运费试算（使用API拦截模式）
python scripts/sync_to_feishu.py "URL" --no-interactive-freight

# 跳过图片上传
python scripts/sync_to_feishu.py "URL" --no-images

# JSON 输出
python scripts/sync_to_feishu.py "URL" --output json
```

**飞书目标表**: `ONy9bZ0oFaaiSEsf4ggcs61enRc` / `tbl75glY29VulRLm`

### 仅提取（不写飞书）

```bash
# 首次使用 — 弹出浏览器窗口，完成登录（自动填写账号密码）
python scripts/extract_product.py "URL" --headless false

# 后续使用 — 后台无头模式，复用已保存的会话
python scripts/extract_product.py "URL" --headless true

# JSON 输出
python scripts/extract_product.py "URL" --output json

# 调试模式 — 导出 HTML/截图/API 响应
python scripts/extract_product.py "URL" --debug
```

### 📋 分类页批量同步

```bash
# 一键同步整个分类页下所有商品到飞书
python scripts/batch_sync_to_feishu.py "https://jit.hicustom.com/.../chooseProduct?recommend_id=xxx"
```
流程：拦截列表API获取所有商品ID → 逐品调用单品同步（含交互运费试算）→ 数据与单品采集完全一致。

## 命令行参数

### sync_to_feishu.py（一键同步 Bot）

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `url` | 商品详情页 URL | 必填 |
| `--force-login` | 强制重新登录 | false |
| `--no-images` | 跳过图片上传 | false |
| `--output` | 输出格式 (json/text) | text |
| `--zip` | 预估运费邮编 | 33101 |
| `--timeout` | 页面加载超时(秒) | 60 |
| `--debug` | 导出调试文件 | false |
| `--interactive-freight` | 通过浏览器点击交互试算运费 | false |

### extract_product.py（仅提取）

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `url` | 商品详情页 URL | 必填 |
| `--zip` | 预估运费邮编 | 33101 |
| `--output` | 输出格式 (json/text/csv) | text |
| `--headless` | 无头模式 (true/false) | 有会话时 true |
| `--force-login` | 强制重新登录 | false |
| `--debug` | 导出页面 HTML/截图 | false |
| `--timeout` | 页面加载超时(秒) | 60 |
| `--download-image` | 下载首张商品图片到当前目录 | false |
| `--interactive-freight` | 通过浏览器点击「更多成本试算」交互试算运费 | false |

## 提取流程

1. 加载 Playwright 浏览器会话（`~/.hicustom_session/state.json`）
2. 导航到商品页 → `page.on("response")` 拦截所有 JSON API
3. 检测登录状态 → 按需弹出窗口等待用户登录
4. 等待 8s 让 wujie iframe + API 请求完成
5. **匹配 3 个 API** → 按精确字段路径提取数据
6. API 兜底：未匹配到时回退 DOM 提取
7. 映射字段 → 上传图片 → 写入飞书

## 反检测配置

Playwright 启动时自动注入反检测参数（`--disable-blink-features=AutomationControlled` 等），绕过滑块验证。

## 变体字段映射规则

始终固定映射：API `type=1 → 颜色`，`type=2 → 尺码`。不做语义判断，与 hicustom 平台自身分类一致。

## 商品详情过滤规则

提取 `product_details` 时自动执行以下整行剔除：

| 规则 | 行为 |
|------|------|
| **商品编码** | 无论内容是什么，一律剔除 |
| **底款编码** | 无论内容是什么，一律剔除 |
| **生产工艺** | 仅当值为「默认工艺路线」时剔除（其他具体工艺名称保留） |
| **价格说明** | 无论内容是什么，一律剔除 |

其余字段（材质说明、商品性能、适用场景、洗涤说明等）100% 原样保留，不做任何修改。

## 图片去重规则

| 规则 | 行为 |
|------|------|
| **有颜色变体** | 每种颜色只保留 1 张图片（尺码差异从主图看不出，自动去重） |
| **无颜色变体** | 有尺码变体也只保留 1 张（首张），无变体则保留全部 |
| **回退策略** | 去重后图片为空时，回退为全部原始图片 |

> 逻辑：通过 SKU 的 `attribute_items` 将图片映射到颜色，同色只取首张。尺码不参与去重判定——尺码差异无法从商品主图中分辨。

## 运费试算（两种模式）

### 模式 1: API 拦截（默认）
页面加载时自动调用 `spu/v1/spu_freight/lowest_freight`，提取最低运费。无需额外交互。

### 模式 2: 浏览器交互试算
通过 `--interactive-freight` 启用，模拟人工操作：
1. 点击商品页上的「更多成本试算」链接
2. 在弹出层输入邮编（默认 33101）
3. 点击「试算」按钮
4. 提取运费结果（多物流方式）
5. 自动关闭弹层

**触发条件**:
- 显式传参 `--interactive-freight` 强制使用
- 或 API 未返回运费时自动降级

**多选择器容错**: 支持 ant-design、element-ui 等常见 UI 框架的 modal/dialog 组件，
自动识别文本为「试算」「计算」「查询」「估算」的按钮，以及 placeholder 包含「邮编」「zip」的输入框。

**运费选择规则**: 交互试算后自动按以下规则选取最优运费：
- 🚀 **中国发货**: 在「递四方」或「云途」中选最低价
- ✈️ **国外发货**: 筛选本土物流（排除跨境小包/云途/递四方等跨境渠道），选最低价
- 🔍 **自动检测**: 含"跨境小包"→中国发货；否则→国外发货
- 📮 **默认邮编**: `33101` (Miami, FL)
- 🔧 **默认模式**: 交互式试算（`--no-interactive-freight` 可关闭）

## 登录方式

| 方式 | 操作 | 说明 |
|------|------|------|
| 账号登录（自动） | 脚本自动点击「账号登录」→ 填写账号密码 → 点击登录 | 弹出浏览器窗口后，**需手动完成滑块验证码** |
| 微信扫码 | 浏览器窗口展示二维码 | 自动登录触发验证码时的备选方案 |
| 手机号登录 | 点击「手机号登录」→ 输入手机号 + 验证码 | 备选方案 |

> **⚠️ 验证码限制**：HiCustom 在提交账号密码后会触发滑块验证码（"请按住滑块，拖动到最右边"），**此步骤无法通过程序自动完成**，必须人工操作。完成一次后，会话自动保存，后续数小时内无需再次登录。

**登录流程**：
1. 脚本自动导航到 `jit.hicustom.com` 商品页 → 触发重定向到 `www.hicustom.com/login`
2. 自动点击底部「账号登录」图标（`p-login_navItm`）
3. 自动填写账号 `15112381122` 和密码 `Leiyuzhe2366`
4. 自动点击「登 录」按钮（`p-login-acc-sub`）
5. **用户手动完成滑块验证码**
6. 登录成功，会话自动保存至 `~/.hicustom_session/state.json`

## 会话管理

- 会话状态: `~/.hicustom_session/state.json`
- 首次或 `--force-login` 时必须用 `--headless false` 弹出窗口
- 登录成功后自动保存，后续可无头复用
- 会话过期时自动提示重新登录

## 依赖

```bash
pip install playwright
playwright install chromium
```

Python 3.9+, playwright >= 1.50

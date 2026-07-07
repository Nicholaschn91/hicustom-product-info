#!/usr/bin/env python3
"""
HICUSTOM Product Info Extractor
从 hicustom.com 商品详情页提取结构化商品信息

工作原理:
  首次: python extract_product.py <url>  → 打开可见浏览器，用户登录，保存会话
  后续: python extract_product.py <url> --headless true  → 复用会话，后台提取
  调试: python extract_product.py <url> --debug  → 导出页面 HTML 用于分析

页面架构:
  - 登录页: 服务端渲染 + jQuery（微信扫码/账号/手机号/QQ）
  - 商品页: Vue.js SPA（static.hihumbird.com），数据通过 API 加载
"""

import json
import argparse
import os
import re
import sys
import time
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any

from playwright.sync_api import sync_playwright, Page, Frame

# ── 路径 ──────────────────────────────────────────────
SESSION_DIR = Path.home() / ".hicustom_session"
SESSION_FILE = SESSION_DIR / "state.json"
DEBUG_DIR = SESSION_DIR / "debug"

# ── 登录/鉴权相关 URL/文本 ──────────────────────────
LOGIN_URL_PATTERNS = [
    "/login",
    "/signin",
    "/auth"
]
LOGIN_TEXT_INDICATORS = [
    "微信扫码登录",
    "扫码关注公众号 即可注册/登录",
    "手机号登录",
]

# ── 数据结构 ──────────────────────────────────────────

@dataclass
class ProductInfo:
    product_id: str = ""
    product_name: str = ""
    unit_price: str = ""
    estimated_shipping: str = ""
    weight: str = ""
    color_variants: List[Dict[str, Any]] = field(default_factory=list)
    size_variants: List[Dict[str, Any]] = field(default_factory=list)
    product_details: str = ""
    package_specs: Dict[str, str] = field(default_factory=dict)
    product_url: str = ""
    images: List[str] = field(default_factory=list)
    api_data: Optional[Dict] = field(default_factory=dict)  # 从 API 拦截到的原始数据

# ── 登录检测 ──────────────────────────────────────────

def is_login_page(page: Page) -> bool:
    """检测当前是否为登录页（综合 URL + 页面文本）"""
    url = page.url.lower()

    # 1. URL 匹配
    for pat in LOGIN_URL_PATTERNS:
        if pat in url:
            return True

    # 2. 页面文本匹配
    try:
        body_text = page.locator("body").first.inner_text(timeout=2000)
        for indicator in LOGIN_TEXT_INDICATORS:
            if indicator in body_text:
                return True
    except:
        pass

    return False


def is_on_product_page(page: Page) -> bool:
    """检测当前是否在商品详情页（已登录状态）"""
    url = page.url.lower()

    # 排除登录页
    if is_login_page(page):
        return False

    # 商品页特征: URL 含 productDetail 或类似路径
    product_indicators = [
        "productdetail",
        "goods/detail",
        "item/detail",
        "/detail",
    ]
    for ind in product_indicators:
        if ind in url:
            return True

    return False


def wait_for_user_login(page: Page, timeout: int = 120) -> bool:
    """等待用户在浏览器窗口中完成登录（自动尝试账号登录）"""
    print("🔐 自动尝试账号登录...")

    # ═══ 自动登录 ═══
    page.wait_for_timeout(2000)
    content = page.content()
    
    if "账号登录" in content or "登录" in content:
        # 点击账号登录 Tab（优先点击可见的 p-login_navItm）
        clicked = False
        for sel in ['a.p-login_navItm:has-text("账号登录")', 'text=账号登录', 'text=帐号登录', 'a:has-text("账号")', '[class*=account]', '.login-tab']:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    page.click(sel, force=True)
                    print(f"   👆 点击: {sel}")
                    page.wait_for_timeout(1500)
                    clicked = True
                    break
            except Exception:
                pass
        
        if not clicked:
            # JS 兜底：点击可见的 login nav 链接
            try:
                page.evaluate('''() => {
                    const links = document.querySelectorAll('a.p-login_navItm, a.m-login_navItm');
                    for (const a of links) {
                        const rect = a.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0) {
                            a.click();
                            return true;
                        }
                    }
                    return false;
                }''')
                page.wait_for_timeout(1500)
            except Exception:
                pass
        
        # 填账号（优先使用精确 ID）
        for sel in ['#p-login-acc-username', '#c-login-acc-username', 'input[placeholder*="用户名"]', 'input[placeholder*="手机"]', 'input[placeholder*="邮箱"]', 'input[name=username]', 'input[name=account]', 'input[type=text]:not([readonly])']:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.fill('15112381122')
                    print(f"   ⌨️  填写账号: {sel}")
                    break
            except Exception:
                pass
        
        page.wait_for_timeout(500)
        
        # 填密码（优先使用精确 ID）
        for sel in ['#p-login-acc-password', '#c-login-acc-password', 'input[type=password]', 'input[placeholder*="密码"]', 'input[name=password]']:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.fill('Leiyuzhe2366.')
                    print(f"   ⌨️  填写密码: {sel}")
                    break
            except Exception:
                pass
        
        page.wait_for_timeout(500)
        
        # 点击登录（优先使用精确 ID + force 点击）
        clicked_btn = False
        for sel in ['#p-login-acc-sub', '#c-login-acc-sub', 'button:has-text("登录")', 'button:has-text("登 录")', 'input[type=submit]', 'button[type=submit]', '[class*=login-btn]']:
            try:
                el = page.query_selector(sel)
                if el:
                    page.click(sel, force=True)
                    print(f"   🔘 点击登录: {sel}")
                    page.wait_for_timeout(2000)
                    clicked_btn = True
                    break
            except Exception:
                pass
        
        if not clicked_btn:
            try:
                page.evaluate('''() => {
                    const btn = document.getElementById('p-login-acc-sub') || document.getElementById('c-login-acc-sub');
                    if (btn) { btn.click(); return 'clicked'; }
                    return 'not found';
                }''')
                page.wait_for_timeout(2000)
            except Exception:
                pass

    # ═══ 等待跳转到商品页 ═══
    print("")
    print(f"   等待登录完成（{timeout}s 后超时）...")
    start = time.time()
    last_url = page.url

    while time.time() - start < timeout:
        page.wait_for_timeout(1500)
        current_url = page.url.lower()
        if current_url != last_url:
            print(f"   🔄 页面跳转: {current_url}")
            last_url = current_url
        if is_on_product_page(page):
            print("✅ 登录成功！已进入商品详情页")
            return True
        
        # 检测验证码
        if "验证码" in page.content() or "captcha" in page.content().lower() or "滑动" in page.content():
            print("   ⚠️ 需要验证码，请手动完成")
            # 等待手动操作
            for _ in range(timeout):
                page.wait_for_timeout(1000)
                if is_on_product_page(page):
                    print("✅ 登录成功！已进入商品详情页")
                    return True
            return False

        remaining = int(timeout - (time.time() - start))
        step = (remaining // 10) * 10
        if step > 0 and step % 20 == 0:
            print(f"   ⏳ 等待中... ({step}s)")

    return False


# ── 页面调试 ──────────────────────────────────────────

def dump_page_debug(page: Page, stage: str):
    """导出页面 HTML、文本、截图用于调试"""
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())

    try:
        content = page.content()
    except:
        content = "<no content>"
    html_path = DEBUG_DIR / f"page_{stage}_{ts}.html"
    html_path.write_text(content, encoding="utf-8")

    try:
        body_text = page.locator("body").first.inner_text()[:8000]
    except:
        body_text = "<no text>"
    txt_path = DEBUG_DIR / f"page_{stage}_{ts}.txt"
    txt_path.write_text(body_text, encoding="utf-8")

    # Frame 对象不支持 screenshot，跳过
    try:
        ss_path = DEBUG_DIR / f"page_{stage}_{ts}.png"
        page.screenshot(path=str(ss_path), full_page=True)
        print(f"   📸 截图: {ss_path}")
    except:
        pass

    print(f"   📄 HTML: {html_path}")
    print(f"   📝 文本: {txt_path}")


# ── 数据提取 ──────────────────────────────────────────

class ProductExtractor:
    """商品信息提取器 — 处理 wujie 微前端 iframe 架构"""

    def __init__(self, page: Page, debug: bool = False):
        self.page = page
        self.debug = debug
        self.api_responses: List[Dict] = []  # 拦截到的 API 响应
        self.iframe_page: Optional[Page] = None  # 商品 iframe 内的 page

    def _find_product_iframe(self) -> Optional[Page]:
        """查找商品详情所在的 iframe（wujie 微前端）"""
        # 遍历所有 frames，找到包含商品详情的 frame
        # wujie 微前端 iframe 的 name 属性通常包含路由路径
        for frame in self.page.frames:
            if not frame.parent_frame:
                continue  # 跳过主 frame
            try:
                content = frame.content()
                # 商品详情页特征: 包含价格/规格等关键字段
                if any(kw in content for kw in [
                    "productDetail", "商品详情", "规格", "颜色", "尺码",
                    "price", "Price", "amount", "goodsId",
                ]):
                    if self.debug:
                        print(f"   🔍 发现商品 iframe: {frame.name} ({len(content)} chars)")
                    return frame
            except:
                continue

        # fallback: 找最后一个非隐藏 iframe
        for frame in reversed(self.page.frames):
            if frame.parent_frame:
                try:
                    content = frame.content()
                    if "goodsId" in content or "productId" in content:
                        return frame
                except:
                    continue

        return None

    def _get_search_root(self):
        """获取搜索根：优先用 iframe 内的 page，否则用主 page"""
        if self.iframe_page:
            return self.iframe_page
        return self.page

    def extract(self, zip_code: str = "33101", interactive_freight: bool = True) -> ProductInfo:
        """执行完整提取

        Args:
            zip_code: 运费试算邮编
            interactive_freight: True=使用浏览器交互试算运费（默认）；False=仅 API 拦截
        """
        info = ProductInfo()
        info.product_url = self.page.url
        # 从 URL 提取商品 ID (id=xxx 参数)
        match = re.search(r'[?&]id=(\d+)', self.page.url)
        if match:
            info.product_id = match.group(1)

        print("📦 开始提取商品信息...")

        # 等待页面渲染完成
        self._wait_for_render()

        # 查找商品 iframe
        self.iframe_page = self._find_product_iframe()
        if self.iframe_page:
            print(f"   🔍 已定位商品 iframe: lang={self.iframe_page.name}")
        else:
            print("   ⚠️ 未找到商品 iframe，使用主页面提取")

        if self.debug:
            dump_page_debug(self.page, "loaded")
            if self.iframe_page:
                dump_page_debug(self.iframe_page, "iframe_loaded")

        # 从 API 拦截提取（主数据源）
        api_info = self._try_extract_from_api()

        if api_info.get("_api_source"):
            print("   📡 主要数据来自 API 拦截")

            # 货币符号：currency_id=1 为 CNY (¥), 2 为 USD ($)
            currency_symbol = api_info.get("currency_symbol", "¥")

            info.product_name = api_info.get("product_name", "")
            raw_price = api_info.get("unit_price", "")
            info.unit_price = f"{currency_symbol}{raw_price}" if raw_price else ""

            raw_shipping = api_info.get("shipping_cost", "")
            info.estimated_shipping = f"{currency_symbol}{raw_shipping}" if raw_shipping else ""

            info.weight = f"{api_info.get('weight_g', '')}g"
            info.color_variants = api_info.get("color_variants", [])
            info.size_variants = api_info.get("size_variants", [])
            info.product_details = api_info.get("product_details", "")
            info.images = api_info.get("images", [])

            # 包装规格：API 返回的是 cm，同时输出英制
            pkg = api_info.get("package_specs", {})
            if pkg:
                l = float(pkg.get("length_cm", 0))
                w = float(pkg.get("width_cm", 0))
                h = float(pkg.get("height_cm", 0))
                gross_w = pkg.get("gross_weight_g", "")
                info.package_specs = {}
                if l and w and h:
                    # 公制
                    metric = f"{l}cm x {w}cm x {h}cm"
                    # 英制
                    l_in = round(l / 2.54, 2)
                    w_in = round(w / 2.54, 2)
                    h_in = round(h / 2.54, 2)
                    imperial = f'{l_in}" x {w_in}" x {h_in}"'
                    info.package_specs["metric"] = metric
                    info.package_specs["imperial"] = imperial
                    info.package_specs["dimensions"] = f"{metric} ({imperial})"
                if gross_w:
                    gross_lb = round(float(gross_w) / 453.592, 2)
                    info.package_specs["gross_weight"] = f"{gross_w}g ({gross_lb}lbs)"

            info.api_data = {k: v for k, v in api_info.items()
                            if k not in ["_api_source"]}

            # 价格阶梯
            tiers = api_info.get("price_tiers", [])
            if tiers:
                tier_text = "\n\n📊 价格阶梯:\n"
                for t in tiers:
                    tier_text += f"  {t['level']}({t['level_name']}): {t['min_qty']}-{t['max_qty'] or '∞'}件 → {currency_symbol}{t['price']}\n"
                info.product_details += tier_text
        else:
            print("   ⚠️ API 未捕获到商品数据，回退到 DOM 提取")
            info = self._extract_from_dom(info, zip_code)

        # ── 交互式运费试算（仅当 API 未返回运费 或 强制交互模式）──
        if interactive_freight or not info.estimated_shipping:
            if interactive_freight:
                print("   🔧 强制交互模式：通过浏览器试算运费")
            elif not info.estimated_shipping:
                print("   🔧 API 未返回运费，尝试浏览器交互试算")

            freight_result = self._interactive_freight_estimation(
                zip_code=zip_code
            )
            if freight_result.get("success"):
                print(f"   📦 运费试算完成" + (f"（{len(freight_result.get('shipping_methods', []))} 条记录）" if freight_result.get("shipping_methods") else "（无可用物流方案）"))
                # 应用智能运费选择规则
                best = self._select_best_freight(freight_result, is_overseas=None)
                if best.get("selected"):
                    currency = api_info.get("currency_symbol", "¥") if api_info.get("_api_source") else "$"
                    info.estimated_shipping = f"{currency}{best['price']}"
                    print(f"   💰 采用运费: {best['method']} {currency}{best['price']} ({best['note']})")

                # 将完整运费明细追加到商品详情
                methods = freight_result.get("shipping_methods", [])
                if methods:
                    methods_text = "\n\n📦 运费试算 (邮编:" + zip_code + "):\n"
                    parsed_all = best.get("all_parsed", [])
                    for m in parsed_all[:15]:
                        methods_text += f"  · {m['method']} ¥{m['price_min']}"
                        if m['price_max'] != m['price_min']:
                            methods_text += f"~{m['price_max']}"
                        if m.get('rate') is not None:
                            methods_text += f" ({m['rate']}%)"
                        methods_text += "\n"
                    info.product_details += methods_text

                # 标记已选中的
                if best.get("selected"):
                    info.api_data["selected_freight"] = {
                        "method": best["method"],
                        "price": best["price"],
                        "rule": best["note"],
                    }
                info.api_data["interactive_freight"] = freight_result

        # ── 输出提取结果 ──
        print(f"   ✅ 商品名称: {info.product_name}")
        print(f"   ✅ 单价 (1件): {info.unit_price}")
        print(f"   ✅ 预估运费: {info.estimated_shipping}")
        print(f"   ✅ 重量: {info.weight}")
        print(f"   ✅ 颜色变体: {len(info.color_variants)} 种")
        print(f"   ✅ 尺码变体: {len(info.size_variants)} 种")
        detail_preview = info.product_details[:100].replace('\n', ' ') if info.product_details else '(空)'
        print(f"   ✅ 商品详情: {detail_preview}...")
        print(f"   ✅ 包装规格: {info.package_specs}")
        print(f"   ✅ 图片: {len(info.images)} 张")

        if self.debug:
            dump_page_debug(self.page, "extracted")
            if self.iframe_page:
                dump_page_debug(self.iframe_page, "iframe_extracted")

        return info

    def _extract_from_dom(self, info: ProductInfo, zip_code: str) -> ProductInfo:
        """回退方案：DOM 提取"""
        root = self._get_search_root()

        info.product_name = self._extract_field([
            '[class*="title"]', 'h1', 'h2',
        ], search_root=root)
        info.unit_price = self._extract_field([
            '[class*="price"]', '[class*="amount"]',
        ], search_root=root)
        weight_text = self._extract_regex_from_page(
            root, r'\d+\.?\d*\s*(g|kg|lbs?|oz)\b', case_sensitive=False)
        if weight_text:
            info.weight = weight_text
        info.estimated_shipping = ""
        info.images = self._extract_image_urls(root)
        return info

    def _wait_for_render(self):
        """等待页面渲染完成"""
        try:
            self.page.wait_for_load_state("networkidle", timeout=20000)
        except:
            pass
        self.page.wait_for_timeout(5000)
        # 再等 iframe 加载
        self.page.wait_for_timeout(3000)

    def _try_extract_from_api(self) -> Dict[str, Any]:
        """从拦截到的 API 响应中智能提取数据
        hiCustom API 结构：
        - spu-itg/uct/v1/spus/{spu_id} → data.data.{name, skus, attribute_items, extra.spu_features, images, factory, ...}
        - product/uct/v1/styles/{style_id} → data.data.skus[{weight, length, width, height, ...}]
        - spu/v1/spu_freight/lowest_freight → data[{freight, method_name, country_code}]
        """
        result = {}

        if self.debug:
            print(f"   📡 拦截到 {len(self.api_responses)} 个 API 响应")

        spu_data = None
        style_data = None
        freight_data = None

        for resp in self.api_responses:
            if not isinstance(resp, dict):
                continue

            url = resp.get("_url", "")
            data = resp.get("data", resp)

            # SPU 商品详情: r.data 直接就是 SPU 数据
            if isinstance(data, dict) and "skus" in data and "name" in data and "attribute_items" in data:
                spu_data = data

            # Style 工艺数据: r.data 直接就是 style 数据
            if isinstance(data, dict) and "skus" in data and "code" in data and "material" in data:
                style_data = data

            # 运费数据: r.data 是 list
            if isinstance(data, list) and len(data) > 0:
                first = data[0]
                if isinstance(first, dict) and "freight" in first and "country_code" in first:
                    freight_data = data

        # ── 从 SPU 数据提取 ──
        if spu_data:
            result["_api_source"] = True

            # 货币: currency_id 1=CNY(¥), 2=USD($)
            currency_id = spu_data.get("source_currency_id", spu_data.get("currency_id", 1))
            result["currency_symbol"] = "¥" if currency_id == 1 else "$"
            result["currency_id"] = currency_id

            # 商品名称
            result["product_name"] = spu_data.get("name", "")
            result["blank_code"] = spu_data.get("blank_product", {}).get("code", "")
            result["blank_name"] = spu_data.get("blank_product", {}).get("name", "")
            result["style_code"] = spu_data.get("style_info", {}).get("code", "")
            result["category"] = spu_data.get("category_name", "")
            result["factory"] = spu_data.get("factory", {}).get("name", "")
            result["delivery_period_hours"] = spu_data.get("delivery_period")
            result["process_route"] = spu_data.get("process_route", {}).get("name", "")

            # 印花类型
            print_type_map = {
                1: "全幅印花",
                2: "局部印花",
                3: "无印花",
            }
            result["print_type"] = print_type_map.get(
                spu_data.get("blank_product", {}).get("print_type"), "")

            # SKU 数据（当前选中的第一个 SKU）
            skus = spu_data.get("skus", [])
            if skus:
                sku = skus[0]

                # 价格 — 从 price_template 中取 C 级（1件）价格
                price_template = sku.get("sku_price_template", {})
                factors = price_template.get("price_level_factors", [])
                if factors:
                    result["unit_price"] = factors[0].get("calculate_value", "")
                    result["price_tiers"] = [
                        {
                            "level": f.get("name", ""),
                            "level_name": f.get("member_level_name", ""),
                            "min_qty": f.get("min_num"),
                            "max_qty": f.get("max_num"),
                            "price": f.get("calculate_value", ""),
                        }
                        for f in factors
                    ]

                result["sales_price"] = sku.get("sales_price", "")
                result["weight_g"] = sku.get("weight")

            # 变体属性
            colors = []
            sizes = []
            for item in spu_data.get("attribute_items", []):
                if item.get("type") == 1:
                    colors.append({"name": item.get("name", ""), "id": item.get("reference_id", ""), "selected": False})
                elif item.get("type") == 2:
                    sizes.append({"name": item.get("name", ""), "id": item.get("reference_id", ""), "selected": False})
            result["color_variants"] = colors
            result["size_variants"] = sizes

            # 商品详情（从 extra.spu_features — 包含截图中的所有字段）
            features = spu_data.get("extra", {}).get("spu_features", {})
            detail_lines = []

            # 基础信息（已剔除：商品编码、底款编码、默认工艺路线）
            blank_product = spu_data.get("blank_product", {})
            print_type_map = {1: "全幅印花", 2: "局部印花", 3: "无印花"}
            pt = blank_product.get("print_type")
            if pt:
                detail_lines.append(f"印花类型: {print_type_map.get(pt, str(pt))}")
            process = spu_data.get("process_route", {})
            if process.get("name") and process["name"] != "默认工艺路线":
                detail_lines.append(f"生产工艺: {process['name']}")
            if features.get("design_description"):
                detail_lines.append(f"设计说明: {features['design_description']}")
            if features.get("material_description"):
                detail_lines.append(f"材质说明: {features['material_description']}")
            if features.get("performance"):
                detail_lines.append(f"商品性能: {features['performance']}")
            if features.get("suitable_occasions"):
                detail_lines.append(f"适用场景: {features['suitable_occasions']}")
            if features.get("washing_instructions"):
                detail_lines.append(f"洗涤说明: {features['washing_instructions']}")
            if features.get("special_notes"):
                detail_lines.append(f"特别说明: {features['special_notes']}")
            if features.get("packaging_description"):
                detail_lines.append(f"包装说明: {features['packaging_description']}")
            if features.get("fba_information"):
                detail_lines.append(f"FBA说明: {features['fba_information']}")

            result["product_details"] = "\n".join(detail_lines)

            # 图片 — 颜色去重: 每种颜色只保留 1 张（尺码差异从主图看不出）
            raw_images = [
                {
                    "url": f"https://obs-hm104.hihumbird.com/{img['file_path']}?x-image-process=style/s500",
                    "sku_id": img.get("sku_id") or img.get("spu_sku_id") or img.get("reference_id", ""),
                }
                for img in spu_data.get("images", [])
            ]
            # 构建 SKU_ID → 颜色映射
            sku_color_map = {}
            for sku in spu_data.get("skus", []):
                sku_id = sku.get("id") or sku.get("spu_sku_id", "")
                # 通过 sku 的 attribute_items 找到颜色
                for attr in sku.get("attribute_items", []):
                    if attr.get("type") == 1:
                        sku_color_map[str(sku_id)] = attr.get("name", "") or attr.get("value", "")
                        break

            # 按颜色分组，每组只取第一张
            seen_colors = set()
            filtered_images = []
            for img in raw_images:
                color = sku_color_map.get(str(img["sku_id"]), "")
                if color:
                    if color not in seen_colors:
                        seen_colors.add(color)
                        filtered_images.append(img["url"])
                elif len(colors) <= 1:
                    # 仅 1 种颜色（或无颜色）→ 只取首张，尺码变体不额外加图
                    if not filtered_images:
                        filtered_images.append(img["url"])
                else:
                    # 无颜色映射但有多种颜色 → 保守保留
                    filtered_images.append(img["url"])

            # 如果过滤后为空，回退为全部图片
            result["images"] = filtered_images if filtered_images else [img["url"] for img in raw_images]
            if len(raw_images) != len(result["images"]):
                print(f"   🖼️  图片去重: {len(raw_images)}→{len(result['images'])} 张（每颜色保留1张）")

        # ── 从 Style 数据提取包装规格 ──
        if style_data:
            style_skus = style_data.get("skus", [])
            if style_skus:
                # 找到匹配当前尺码的 SKU
                style_sku = style_skus[0]  # fallback
                if spu_data and spu_data.get("skus"):
                    current_sku_id = spu_data["skus"][0].get("style_product_sku_id")
                    for ss in style_skus:
                        if ss.get("id") == current_sku_id:
                            style_sku = ss
                            break

                # API 返回的尺寸单位是 cm
                result["package_specs"] = {
                    "length_cm": style_sku.get("length", ""),
                    "width_cm": style_sku.get("width", ""),
                    "height_cm": style_sku.get("height", ""),
                    "gross_weight_g": style_sku.get("weight"),
                    "color_name": style_sku.get("color_name", ""),
                    "size_name": style_sku.get("size_name", ""),
                }

        # ── 从运费 API 提取 ──
        if freight_data:
            lowest_freight = freight_data[0]
            result["shipping_cost"] = lowest_freight.get("freight")
            result["shipping_method"] = lowest_freight.get("method_name")
            result["country_code"] = lowest_freight.get("country_code")

        return result

    def _extract_field(self, selectors: List[str], max_len: int = 500,
                       extract_images: bool = False, from_api=None,
                       search_root=None) -> Any:
        """依次尝试选择器提取文本或图片"""
        root = search_root or self._get_search_root()

        # 优先使用 API 数据
        if from_api is not None and from_api != "" and from_api != []:
            if isinstance(from_api, list):
                if extract_images:
                    return from_api
                return ", ".join(str(x) for x in from_api)
            return str(from_api)[:max_len]

        if extract_images:
            return self._extract_image_urls(root)

        for sel in selectors:
            try:
                # 处理 text= 选择器
                if sel.startswith("text="):
                    match = root.locator(sel).first
                    if match.count() > 0:
                        txt = match.text_content().strip()
                        if txt:
                            return txt[:max_len]
                    continue

                el = root.locator(sel).first
                if el.count() > 0:
                    txt = el.text_content().strip()
                    if txt:
                        return txt[:max_len]
            except:
                continue
        return ""

    def _extract_regex_from_page(self, root, pattern: str,
                                  case_sensitive: bool = True) -> str:
        """从页面全文本中用正则提取"""
        try:
            text = root.locator("body").first.inner_text(timeout=3000)
            flags = 0 if case_sensitive else re.IGNORECASE
            match = re.search(pattern, text, flags)
            if match:
                return match.group(0)
        except:
            pass
        return ""

    def _extract_image_urls(self, root) -> List[str]:
        """提取商品主图"""
        urls = set()
        for sel in [
            'img[src*="effect_image"]',
            '[class*="gallery"] img',
            '[class*="product-img"] img',
            '[class*="main-img"] img',
            '.swiper-slide img',
            '[class*="preview"] img',
            'img[src*="obs-"]',
        ]:
            try:
                for el in root.locator(sel).all():
                    src = el.get_attribute("src") or el.get_attribute("data-src") or ""
                    if src and not src.startswith("data:"):
                        urls.add(src)
            except:
                continue
        return list(urls)[:20]


    def _interactive_freight_estimation(
        self, zip_code: str = "33101", timeout_ms: int = 15000
    ) -> Dict[str, Any]:
        """通过浏览器交互试算运费：点击"更多成本试算" → 输入邮编 → 试算 → 提取结果

        作为 API 拦截的备选方案。当 API 没有返回运费数据时自动触发，
        也可通过 --interactive-freight 强制使用。

        返回: {"success": bool, "shipping_methods": [...], "lowest_freight": str, ...}
        """
        result = {"success": False, "shipping_methods": [], "error": None}

        root = self._get_search_root()

        # ═══ Step 1: 查找并点击运费试算入口 ═══
        trigger_selectors = [
            "text=更多成本试算",
            "text=成本试算",
            "text=运费试算",
            "text=预估运费",
            '[class*="cost-trial"]',
            '[class*="freight"]',
            '[class*="shipping-cost"]',
            'a:has-text("试算")',
            'span:has-text("试算")',
            'button:has-text("试算")',
            'div:has-text("成本试算")',
        ]

        clicked = False
        for sel in trigger_selectors:
            try:
                el = root.locator(sel).first
                if el.count() > 0:
                    el.click(timeout=3000)
                    print(f"   👆 已点击运费试算入口: {sel}")
                    clicked = True
                    break
            except Exception:
                continue

        if not clicked:
            # 尝试在主页面（外层）搜索
            for frame in self.page.frames:
                if frame == self.iframe_page:
                    continue
                try:
                    for sel in trigger_selectors:
                        el = frame.locator(sel).first
                        if el.count() > 0:
                            el.click(timeout=3000)
                            print(f"   👆 已在外层 frame 点击运费试算入口: {sel}")
                            clicked = True
                            break
                except Exception:
                    continue
                if clicked:
                    break

        if not clicked:
            result["error"] = "未找到运费试算入口按钮"
            return result

        # ═══ Step 2: 等待弹层/对话框出现 ═══
        self.page.wait_for_timeout(2000)

        dialog_selectors = [
            '[class*="modal"]',
            '[class*="dialog"]',
            '[class*="drawer"]',
            '[class*="popup"]',
            '[class*="ant-modal"]',
            '[class*="ant-drawer"]',
            '[class*="el-dialog"]',
            '[role="dialog"]',
            '.ant-modal-body',
        ]

        dialog_root = None
        for frame in self.page.frames:
            try:
                for sel in dialog_selectors:
                    el = frame.locator(sel).first
                    if el.count() > 0 and el.is_visible():
                        dialog_root = frame
                        print(f"   📦 发现运费试算弹层 (frame={frame.name}, sel={sel})")
                        break
            except Exception:
                continue
            if dialog_root:
                break

        if not dialog_root:
            # fallback: 使用主 page
            try:
                for sel in dialog_selectors:
                    el = self.page.locator(sel).first
                    if el.count() > 0 and el.is_visible():
                        dialog_root = self.page
                        print(f"   📦 发现运费试算弹层 (main page, sel={sel})")
                        break
            except Exception:
                pass

        if not dialog_root:
            dialog_root = self.page  # 最后兜底
            print("   ⚠️ 未确认弹层元素，使用主页面")

        # ═══ Step 3: 查找邮编输入框并填写 ═══
        input_selectors = [
            'input[placeholder*="邮编"]',
            'input[placeholder*="zip"]',
            'input[placeholder*="ZIP"]',
            'input[placeholder*="邮政编码"]',
            'input[name*="zip"]',
            'input[name*="postal"]',
            'input[name*="postcode"]',
            'input[name*="code"]',
            '[class*="zip"] input',
            '[class*="postal"] input',
            '[class*="postcode"] input',
        ]

        zip_filled = False
        for sel in input_selectors:
            try:
                el = dialog_root.locator(sel).first
                if el.count() > 0 and el.is_visible():
                    el.click(timeout=2000)
                    el.fill("", timeout=2000)  # 先清空
                    el.fill(zip_code, timeout=2000)
                    print(f"   ⌨️  已填写邮编: {zip_code} (sel={sel})")
                    zip_filled = True
                    break
            except Exception:
                continue

        if not zip_filled:
            # 模糊搜索: 找任何可见的 input 并尝试填写
            try:
                inputs = dialog_root.locator("input:visible")
                count = inputs.count()
                for i in range(min(count, 5)):
                    try:
                        inp = inputs.nth(i)
                        inp.click(timeout=1000)
                        inp.fill(zip_code, timeout=2000)
                        print(f"   ⌨️  已填写邮编到第 {i+1} 个 input: {zip_code}")
                        zip_filled = True
                        break
                    except Exception:
                        continue
            except Exception:
                pass

        if not zip_filled:
            result["error"] = "未找到邮编输入框"
            return result

        self.page.wait_for_timeout(500)

        # ═══ Step 4: 点击试算按钮 ═══
        # 注意：不能用 a:has-text("试算")，会命中"查看完整运费试算方案"链接
        calc_selectors = [
            'button:has-text("试 算")',
            'button:has-text("试算")',
            '[role="button"]:has-text("试 算")',
            '[role="button"]:has-text("试算")',
            'span:has-text("试 算")',
            'div:has-text("试 算")',
            'button:has-text("计算")',
            'button:has-text("查询")',
            'button:has-text("估算")',
            'button:has-text("确认")',
            '.ant-btn-primary',
            '[class*="calc"]',
            '[class*="submit"]',
        ]

        calc_clicked = False
        for sel in calc_selectors:
            try:
                el = dialog_root.locator(sel).first
                if el.count() > 0 and el.is_visible():
                    el.click(timeout=3000)
                    print(f"   🔘 已点击试算按钮: {sel}")
                    calc_clicked = True
                    break
            except Exception:
                continue

        if not calc_clicked:
            # 尝试按 Enter 键
            try:
                dialog_root.locator("input:visible").first.press("Enter", timeout=2000)
                print("   🔘 已按 Enter 键提交")
                calc_clicked = True
            except Exception:
                pass

        if not calc_clicked:
            result["error"] = "未找到试算按钮"
            return result

        # ═══ Step 5: 等待结果并提取 ═══
        self.page.wait_for_timeout(3000)

        # 尝试提取运费结果表格/列表
        freight_items = []
        result_selectors = [
            'table tbody tr',           # 表格行
            '[class*="freight"] tr',    # 运费表格
            '[class*="result"] tr',     # 结果表格
            'tr:has-text("$")',         # 包含价格的 tr
            'tr:has-text("¥")',         # 包含价格的 tr
            'tr:has-text("€")',         # 欧元价格
            'tr:has-text("EUR")',       # EUR
            '[class*="shipping"] li',   # 物流项
            '[class*="method"] li',     # 物流方式
        ]

        extracted_texts = []
        for sel in result_selectors:
            try:
                els = dialog_root.locator(sel)
                count = els.count()
                if count > 0:
                    for i in range(min(count, 20)):
                        try:
                            txt = els.nth(i).text_content().strip()
                            if txt and len(txt) > 3:
                                # 过滤无效行：尺寸数据、暂无数据
                                if re.match(r'^[\d.*]+$', txt):
                                    continue
                                if '暂无数据' in txt or 'no data' in txt.lower():
                                    continue
                                if '单尺码' in txt or '多尺码' in txt or '尺码' in txt[:5]:
                                    continue
                                if '暂无数据' in txt or 'no data' in txt.lower():
                                    continue
                                # 过滤纯尺寸行、表头行（无价格符号）
                                if re.search(r'\d+\.?\d*\*\d+\.?\d*\*\d+', txt) and len(txt.split('￥')) < 2 and len(txt.split('$')) < 2 and len(txt.split('€')) < 2:
                                    continue
                                # 过滤运费对话框表头文本（含"目的地""序号""试算"等）
                                if '序号' in txt and '物流方式' in txt:
                                    continue
                                # 无任何价格符号的纯文本 → 不是运费数据（过滤表头/提示/空行）
                                if not re.search(r'[￥¥$€£]', txt) and not re.search(r'\b\d+\.\d{2}\b', txt):
                                    continue
                                extracted_texts.append(txt)
                        except Exception:
                            continue
                    if extracted_texts:
                        break
            except Exception:
                continue

        # 如果表格提取失败，尝试获取弹层全文本
        if not extracted_texts:
            try:
                for sel in dialog_selectors:
                    el = dialog_root.locator(sel).first
                    if el.count() > 0:
                        full_text = el.text_content().strip()
                        if full_text:
                            extracted_texts.append(full_text)
                            break
            except Exception:
                pass

        if extracted_texts:
            # 验证: 至少有一条包含有效的价格符号
            has_valid_price = any(re.search(r'[￥¥$€£]\s*\d+', t) for t in extracted_texts)
            if has_valid_price:
                result["success"] = True
                result["shipping_methods"] = extracted_texts
            else:
                result["success"] = False
                result["error"] = "未提取到有效运费数据"
            # 尝试提取最低运费
            for txt in extracted_texts:
                price_match = re.search(r'[￥¥$€]\s*(\d+\.?\d*)', txt)
                if price_match:
                    if not result.get("lowest_freight"):
                        result["lowest_freight"] = price_match.group(1)
                    freight_items.append({
                        "text": txt,
                        "price": price_match.group(1)
                    })
            if freight_items:
                result["shipping_methods"] = freight_items

        # ═══ Step 6: 关闭弹层 ═══
        close_selectors = [
            'button:has-text("关闭")',
            'button:has-text("取消")',
            '[class*="close"]',
            '[class*="cancel"]',
            '.ant-modal-close',
            '[aria-label="Close"]',
            '[aria-label="close"]',
        ]
        for sel in close_selectors:
            try:
                el = dialog_root.locator(sel).first
                if el.count() > 0 and el.is_visible():
                    el.click(timeout=2000)
                    break
            except Exception:
                continue

        # ESC 兜底
        try:
            self.page.keyboard.press("Escape")
        except Exception:
            pass

        self.page.wait_for_timeout(500)
        return result

    def _select_best_freight(
        self, freight_result: Dict[str, Any], is_overseas: Optional[bool] = None
    ) -> Dict[str, Any]:
        """从交互式运费结果中按规则选取最优运费

        规则:
        - 中国发货: 在「递四方」或「云途」中取最便宜的（跨境小包）
        - 国外发货: 筛选本土物流（排除跨境小包渠道），取最便宜的
        - is_overseas=None 时自动检测（含"跨境小包"→中国发货）
        - 返回: {"selected": True/False, "method": str, "price": float, "all_parsed": [...]}
        """
        # 跨境渠道关键词（国外发货时排除）
        # 注意：不以物流公司名判断，只以平台文本标签"跨境小包"为准
        CROSS_BORDER_KEYWORDS = [
            "跨境小包", "跨境",
        ]

        parsed = self._parse_freight_texts(freight_result.get("shipping_methods", []))

        if not parsed:
            return {"selected": False, "method": "", "price": 0.0, "all_parsed": [], "note": "无可解析的运费数据"}

        # 自动检测发货地
        if is_overseas is None:
            raw_texts = freight_result.get("shipping_methods", [])
            all_text = " ".join(
                m.get("text", "") if isinstance(m, dict) else str(m)
                for m in raw_texts
            )
            is_overseas = "跨境小包" not in all_text and "跨境" not in all_text
            print(f"   🌍 自动检测发货地: {'国外' if is_overseas else '中国'}")

        if is_overseas:
            # 国外发货：排除跨境渠道，只选本土物流中最便宜
            local_candidates = [
                p for p in parsed
                if not any(kw in p["method"] for kw in CROSS_BORDER_KEYWORDS)
            ]
            if local_candidates:
                candidates = local_candidates
                print(f"   🏠 本土物流: {len(local_candidates)} 条 (排除 {len(parsed)-len(local_candidates)} 条跨境)")
            else:
                # 兜底：全部都是跨境渠道时，取所有中最便宜的
                candidates = parsed
                print("   ⚠️ 未找到本土物流，降级为全渠道最低价")
        else:
            # 中国发货：只考虑递四方或云途
            candidates = [
                p for p in parsed
                if "递四方" in p["method"] or "云途" in p["method"]
            ]
            if not candidates:
                # 兜底：没有递四方/云途时，取所有中最便宜的
                candidates = parsed
                print("   ⚠️ 未找到递四方/云途，降级为所有渠道最低价")

        # 排序取最低价
        candidates.sort(key=lambda x: x["price_min"])
        best = candidates[0]

        return {
            "selected": True,
            "method": best["method"],
            "price": best["price_min"],
            "price_max": best["price_max"],
            "rate": best.get("rate"),
            "note": f"{'国外发货-本土物流最低价' if is_overseas else '中国发货-递四方/云途最低价'}",
            "all_parsed": parsed,
        }

    def _parse_freight_texts(self, raw_texts: list) -> List[Dict[str, Any]]:
        """解析运费文本，提取物流方式名称和价格

        文本格式（单行）:
          递四方标准商派专线-普货「886991557499056384」跨境小包6.19~7.0599.15%118.000￥ 33.85税
          云途全球专线挂号（特惠普货）「276737253559069440」跨境小包6.55~7.6697.00%118.000￥ 38.89税
          菜鸟无忧物流-标准线上「464465400805990016」销售平台物流7.19~8.4998.03%118.000处理费:0.30销售平台"在线物流下单"获取预估运费

        提取: method_name, price (￥ 后数字), 价格区间, 妥投率
        """
        parsed = []
        for entry in raw_texts:
            text = entry.get("text", "") if isinstance(entry, dict) else str(entry)
            if not text:
                continue

            # 跳过变体尺寸行（无物流信息）
            if text.startswith("单尺码"):
                continue

            # 提取物流方式名称（「」之前的部分）
            method_name = text.split("「")[0].strip() if "「" in text else text[:30].strip()

            # 提取价格: 行内可能有多处 ￥，取最后一个（通常为总运费）
            # 支持 ¥, $, € 三种货币符号
            prices = re.findall(r'[￥¥\$€]\s*(\d+\.?\d*)', text)
            if prices:
                price = float(prices[-1])  # 最后一个 ￥ 才是总价
                price_min = price
                price_max = price
            else:
                # 备用：提取 处理费:0.30 格式
                fee_match = re.search(r'处理费:(\d+\.?\d*)', text)
                if fee_match:
                    price = float(fee_match.group(1))
                    price_min = price
                    price_max = price
                else:
                    continue  # 无法提取价格，跳过

            # 提取物流时效（天数范围，通常在价格之前）: X.XX~Y.YY
            # 时效格式: "6.19~7.0599.15%" → 时效 6.19~7.05 天，率 99.15%
            range_match = re.search(r'(\d+\.?\d*)~(\d+\.?\d*)', text)
            if range_match:
                # 如果 ￥ 价格 > 100，range 可能是价格；否则是时效
                r1 = float(range_match.group(1))
                r2 = float(range_match.group(2))
                if r1 > 100 or r2 > 100:
                    # range 是另一个价格，用较低值
                    price_min = min(r1, price)
                    price_max = max(r2, price)
                # else: range 是时效天数，忽略

            # 提取妥投率: XX.XX%
            rate_match = re.search(r'(\d+\.?\d*)%', text)
            rate = float(rate_match.group(1)) if rate_match else None

            parsed.append({
                "method": method_name,
                "price": price,
                "price_min": price_min,
                "price_max": price_max,
                "rate": rate,
                "raw_text": text[:120],
            })

        return parsed

def extract_product(
    url: str,
    zip_code: str = "33101",
    headless: Optional[bool] = None,
    force_login: bool = False,
    timeout: int = 60,
    debug: bool = False,
    interactive_freight: bool = True,
    session_name: str = "default",
) -> ProductInfo:
    """提取商品信息主函数"""

    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        has_session = SESSION_FILE.exists() and not force_login

        # 决定是否无头
        if headless is None:
            use_headless = has_session
        else:
            use_headless = headless

        if not use_headless:
            print("🖥️  打开浏览器窗口（非无头模式）...")

        # 反检测参数（绕过滑块验证等反爬机制）
        anti_detect_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-features=TranslateUI,BlinkGenPropertyTrees",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-dev-shm-usage",
        ]
        launch_args = (
            anti_detect_args + ["--start-maximized"]
            if not use_headless
            else anti_detect_args
        )
        browser = p.chromium.launch(
            headless=use_headless,
            args=launch_args,
        )

        # 会话加载
        if has_session:
            context = browser.new_context(storage_state=str(SESSION_FILE))
            print("📂 已加载登录会话")
        else:
            context = browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
            )

        page = context.new_page()

        # 隐藏自动化特征（绕过滑块验证）
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN','zh','en']});
        """)

        # ── API 拦截 ──
        extractor = ProductExtractor(page, debug=debug)

        def capture_response(response):
            """捕获所有 JSON API 响应"""
            ct = response.headers.get("content-type", "")
            if "json" in ct:
                try:
                    body = response.json()
                    body["_url"] = response.url
                    extractor.api_responses.append(body)
                except:
                    pass

        page.on("response", capture_response)

        # ── 导航 ──
        try:
            print(f"🌐 访问: {url}")
            page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")

            # 登录检测
            need_login = is_login_page(page) or not has_session

            if need_login:
                if has_session:
                    print("⚠️  会话已过期，需要重新登录")

                # 非无头模式等待用户登录
                if use_headless:
                    print("❌ 需要登录但运行在无头模式")
                    print("   请先运行一次非无头模式进行登录:")
                    print(f"   python extract_product.py \"{url}\" --headless false")
                    raise RuntimeError("登录会话不存在，请先以非无头模式运行")

                if not wait_for_user_login(page, timeout=120):
                    raise TimeoutError("登录超时，请重试")

                # 保存会话
                context.storage_state(path=str(SESSION_FILE))
                print("💾 登录会话已保存")
                page.wait_for_timeout(3000)  # 等 API 加载完

            elif is_on_product_page(page):
                print("✅ 会话有效，已进入商品页")
                page.wait_for_timeout(3000)  # 等 API 加载完
            else:
                print(f"⚠️  页面状态异常（当前 URL: {page.url}），尝试提取...")

            # ── 提取 ──
            info = extractor.extract(
                zip_code=zip_code,
                interactive_freight=interactive_freight,
            )
            return info

        finally:
            context.close()
            browser.close()


# ── CLI ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="从 hicustom.com / jit.hicustom.com 提取商品信息")
    parser.add_argument("url", help="商品详情页 URL")
    parser.add_argument("--zip", default="33101", help="预估运费邮编 (默认: 33101)")
    parser.add_argument("--output", choices=["json", "text", "csv"], default="text",
                        help="输出格式 (默认: text)")
    parser.add_argument("--headless", choices=["true", "false"], default=None,
                        help="是否无头模式 (默认: 有会话时为 true)")
    parser.add_argument("--force-login", action="store_true", help="强制重新登录")
    parser.add_argument("--timeout", type=int, default=60, help="页面加载超时(秒)")
    parser.add_argument("--debug", action="store_true",
                        help="导出页面 HTML/截图用于调试")
    parser.add_argument("--session", default="default",
                        help="会话名称（多账户时使用）")
    parser.add_argument("--download-image", action="store_true",
                        help="下载首张商品图片到当前目录")
    parser.add_argument("--no-interactive-freight", action="store_false",
                        dest="interactive_freight", default=True,
                        help="关闭交互式运费试算，使用API拦截模式（默认：开启交互式试算）")

    args = parser.parse_args()

    headless = None
    if args.headless == "true":
        headless = True
    elif args.headless == "false":
        headless = False

    try:
        info = extract_product(
            url=args.url,
            zip_code=args.zip,
            headless=headless,
            force_login=args.force_login,
            timeout=args.timeout,
            debug=args.debug,
            interactive_freight=args.interactive_freight,
        )

        # ── 输出 ──
        if args.output == "json":
            data = asdict(info)
            # 不要输出过大的 api_data
            if data.get("api_data") and isinstance(data["api_data"], dict):
                data["api_data"] = "(truncated)"
            print(json.dumps(data, ensure_ascii=False, indent=2))
        elif args.output == "text":
            print(f"\n{'─'*55}")
            print(f"  商品名称: {info.product_name}")
            print(f"  单价 (1件): {info.unit_price}")
            print(f"  预估运费:  {info.estimated_shipping}")
            print(f"  重量:      {info.weight}")
            print()
            if info.color_variants:
                print(f"  颜色变体 ({len(info.color_variants)} 种):")
                for v in info.color_variants:
                    print(f"    ○ {v['name']}")
            if info.size_variants:
                print(f"  尺码变体 ({len(info.size_variants)} 种):")
                for v in info.size_variants:
                    print(f"    ○ {v['name']}")
            print()
            print(f"  商品详情:")
            print(f"    " + info.product_details.replace('\n', '\n    ')[:2000])
            if info.package_specs:
                print(f"  包装规格:")
                for k, v in info.package_specs.items():
                    print(f"    {k}: {v}")
            if info.images:
                print(f"  首图 URL: {info.images[0]}")
            print(f"{'─'*55}\n")
        elif args.output == "csv":
            import csv
            import io
            out = io.StringIO()
            w = csv.writer(out)
            w.writerow(["字段", "值"])
            w.writerow(["商品名称", info.product_name])
            w.writerow(["单价", info.unit_price])
            w.writerow(["预估运费", info.estimated_shipping])
            w.writerow(["重量", info.weight])
            w.writerow(["颜色变体数", len(info.color_variants)])
            w.writerow(["尺码变体数", len(info.size_variants)])
            w.writerow(["商品详情", info.product_details[:200]])
            w.writerow(["包装规格", json.dumps(info.package_specs, ensure_ascii=False)])
            print(out.getvalue())

    except Exception as e:
        print(f"❌ 错误: {e}", file=sys.stderr)
        sys.exit(1)

    # ── 下载首图 ──
    if args.download_image and info.images:
        import urllib.request
        img_url = info.images[0]
        # 用 sku_code 或 product_id 做文件名
        fname = f"product_{re.search(r'id=(\d+)', args.url).group(1) if re.search(r'id=(\d+)', args.url) else 'image'}_01.jpeg"
        try:
            print(f"📥 下载首图: {fname}")
            urllib.request.urlretrieve(img_url, fname)
            print(f"✅ 首图已保存: {os.path.abspath(fname)}")
        except Exception as e:
            print(f"⚠️ 图片下载失败: {e}")


if __name__ == "__main__":
    main()

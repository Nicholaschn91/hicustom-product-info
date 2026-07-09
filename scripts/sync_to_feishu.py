#!/usr/bin/env python3
"""
HiCustom → 飞书 一键同步 Bot

用法:
  python sync_to_feishu.py "https://jit.hicustom.com/.../productDetail?id=xxx"

流程: 提取商品信息 → 映射字段 → 上传图片 → 写入飞书多维表格

飞书表: https://tcnqenxcd30d.feishu.cn/base/ONy9bZ0oFaaiSEsf4ggcs61enRc?table=tbl75glY29VulRLm
"""

import json
import argparse
import os
import sys
import time
import tempfile
from pathlib import Path
from dataclasses import asdict

try:
    import requests
except ImportError:
    print("请先安装 requests: pip install requests")
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent))
from extract_product import extract_product, ProductInfo
from pricing_calculator import PricingEngine

# ── 飞书配置 ──────────────────────────────────────────
# 从配置文件加载（避免密钥硬编码被 GitHub 拦截）
_config_path = Path(__file__).parent.parent / "references" / "config.json"
_FEISHU_CONFIG = {}
if _config_path.exists():
    with open(_config_path) as f:
        _FEISHU_CONFIG = json.load(f).get("feishu", {})

FEISHU = {
    "app_id": _FEISHU_CONFIG.get("app_id", ""),
    "app_secret": _FEISHU_CONFIG.get("app_secret", ""),
    "base_token": _FEISHU_CONFIG.get("base_token", ""),
    "table_id": _FEISHU_CONFIG.get("table_id", ""),
}

FEISHU_API = "https://open.feishu.cn/open-apis"
TABLE_URL = (
    f"https://tcnqenxcd30d.feishu.cn/base/{FEISHU['base_token']}"
    f"?table={FEISHU['table_id']}&view=vew81hvLSx"
)


# ── 飞书 API 工具 ─────────────────────────────────────

class FeishuClient:
    def __init__(self):
        self.token = self._get_tenant_token()
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def _get_tenant_token(self) -> str:
        r = requests.post(
            f"{FEISHU_API}/auth/v3/tenant_access_token/internal",
            json={"app_id": FEISHU["app_id"], "app_secret": FEISHU["app_secret"]},
        )
        return r.json()["tenant_access_token"]

    def create_record(self, fields: dict) -> dict:
        r = requests.post(
            f"{FEISHU_API}/bitable/v1/apps/{FEISHU['base_token']}"
            f"/tables/{FEISHU['table_id']}/records",
            headers=self.headers,
            json={"fields": fields},
        )
        return r.json()

    def upsert_record(self, fields: dict) -> dict:
        """按 商品ID + 设计方案 查找已有记录，有则更新、无则创建。
        
        同一商品可以有多个设计方案（如钓鱼款、伴郎款），每条独立记录。
        无设计方案时仅按商品ID匹配（兼容旧数据和批量采集）。
        """
        product_id = fields.get("商品ID", "")
        design = fields.get("设计方案", "")
        
        if not product_id:
            return self.create_record(fields)
        
        # 构造过滤器：商品ID 必选，设计方案可选
        encoded_pid = product_id.replace('"', '\\"')
        url = (
            f"{FEISHU_API}/bitable/v1/apps/{FEISHU['base_token']}"
            f"/tables/{FEISHU['table_id']}/records"
            f"?filter=CurrentValue.[商品ID]=%22{encoded_pid}%22"
            f"&page_size=50"
        )
        r = requests.get(url, headers=self.headers)
        data = r.json()
        items = data.get("data", {}).get("items", [])
        
        # 在已有记录中匹配设计方案
        matched = None
        if design:
            for item in items:
                if item.get("fields", {}).get("设计方案") == design:
                    matched = item
                    break
        else:
            # 无设计方案时，优先匹配也没有设计方案的旧记录
            for item in items:
                if not item.get("fields", {}).get("设计方案"):
                    matched = item
                    break
            # 如果所有记录都有设计方案，创建新的
            if not matched and items:
                matched = None
        
        if matched:
            record_id = matched["record_id"]
            r = requests.put(
                f"{FEISHU_API}/bitable/v1/apps/{FEISHU['base_token']}"
                f"/tables/{FEISHU['table_id']}/records/{record_id}",
                headers=self.headers,
                json={"fields": fields},
            )
            resp = r.json()
            if resp.get("code") == 0:
                return {"code": 0, "data": {"record": {"record_id": record_id}}, "action": "updated"}
            return resp
        else:
            resp = self.create_record(fields)
            if resp.get("code") == 0:
                resp["action"] = "created"
            return resp  

    def upload_image(self, filepath: str, filename: str) -> str:
        """上传图片到飞书驱动，返回 file_token"""
        size = os.path.getsize(filepath)
        with open(filepath, "rb") as f:
            r = requests.post(
                f"{FEISHU_API}/drive/v1/medias/upload_all",
                headers={"Authorization": f"Bearer {self.token}"},
                files={"file": (filename, f, "image/jpeg")},
                data={
                    "file_name": filename,
                    "parent_type": "bitable_image",
                    "parent_node": FEISHU["base_token"],
                    "size": str(size),
                },
            )
        result = r.json()
        if result.get("code") != 0:
            raise Exception(f"图片上传失败: {result.get('msg')}")
        return result["data"]["file_token"]


# ── 字段映射 ──────────────────────────────────────────

def extract_raw_api_fields(info: ProductInfo) -> dict:
    """从 api_data 中提取工厂/品类/出货周期等原始字段"""
    raw = {}
    api = info.api_data or {}

    factory = api.get("factory", "")
    if factory:
        raw["factory"] = factory

    category = api.get("category", "")
    if category:
        raw["category"] = category

    delivery = api.get("delivery_period_hours")
    if delivery:
        if isinstance(delivery, (int, float)):
            raw["delivery_period"] = f"{int(delivery)}小时"
        else:
            raw["delivery_period"] = str(delivery)

    return raw


def _infer_category(product_name: str, product_details: str) -> str:
    """品类 LLM 兜底: 根据商品名称和详情推断品类"""
    text = f"{product_name}\n{product_details[:500]}"
    # 关键词匹配 → 品类映射
    CATEGORY_MAP = [
        ("帽|帽子|cap|hat|棒球帽|鸭舌帽|渔夫帽", "服饰配件 > 帽子"),
        ("包|bag|tote|wallet|钱包|收纳|organizer|手提|背包", "服饰配件 > 包袋"),
        ("钥匙扣|keychain|挂件", "服饰配件 > 钥匙扣"),
        ("手表|腕表|watch", "服饰配件 > 手表"),
        ("杯垫|coaster|托盘", "生活家居 > 桌面用品"),
        ("存钱罐|piggy bank|coin bank", "生活家居 > 收纳摆件"),
        ("开瓶器|opener", "生活家居 > 厨房工具"),
        ("戒指|ring|戒指盒|ring box|首饰|jewelry", "生活家居 > 首饰收纳"),
        ("烛台|candle holder|香薰", "生活家居 > 家居装饰"),
        ("糖果罐|candy jar|收纳罐", "生活家居 > 厨房收纳"),
        ("挂牌|sign|门牌|plaque", "生活家居 > 家居装饰"),
        ("相框|frame|照片", "生活家居 > 装饰摆件"),
        ("摆件|夜灯|light|灯|ornament", "生活家居 > 装饰摆件"),
        ("冰箱贴|magnet", "生活家居 > 装饰摆件"),
        ("书签|bookmark", "生活家居 > 文具"),
        ("圆珠笔|pen|笔", "生活家居 > 文具"),
        ("拼图|puzzle|益智|鲁班锁", "生活家居 > 玩具"),
        ("十字架|cross|耶稣", "生活家居 > 宗教用品"),
        ("挂钟|clock|钟", "生活家居 > 钟表"),
        ("圣诞|christmas|装饰品", "生活家居 > 节庆装饰"),
        ("风铃|wind chime", "生活家居 > 家居装饰"),
        ("车牌|license plate", "生活家居 > 装饰摆件"),
        ("标牌|signage", "生活家居 > 标牌"),
        ("鼠标垫|mouse pad|桌垫", "生活家居 > 办公用品"),
        ("地垫|mat|rug|door mat", "生活家居 > 地垫"),
        ("无框画|canvas|画|装饰画|铁皮画|wall art|poster", "生活家居 > 墙面装饰"),
        ("宠物|pet|dog|cat|骨灰|ashes|纪念|memorial", "宠物用品 > 纪念品"),
        ("口红|lipstick", "服饰配件 > 化妆收纳"),
        ("工牌|badge|伸缩扣|retractable", "工牌配件"),
        ("杯|mug|cup|tumbler|水瓶", "生活家居 > 杯具"),
    ]
    import re
    text_lower = text.lower()
    for pattern, category in CATEGORY_MAP:
        if re.search(pattern, text_lower):
            return category
    return ""


def map_fields(info: ProductInfo, design: str = "") -> dict:
    """将 ProductInfo 映射为飞书表格字段"""
    raw = extract_raw_api_fields(info)
    fields = {}

    # 设计方案（横向扩展：同一商品可有多条记录，按设计方案区分）
    if design:
        fields["设计方案"] = design

    # 商品名称
    if info.product_name:
        fields["商品名称"] = info.product_name

    # 商品 ID
    if info.product_id:
        fields["商品ID"] = info.product_id

    # 单价 — 去除 ¥，保留数字
    price_str = info.unit_price.replace("¥", "").replace("$", "").strip()
    try:
        price = round(float(price_str), 2)
        fields["单价 (1件)"] = price
    except (ValueError, TypeError):
        pass

    # 预估运费
    ship_str = info.estimated_shipping.replace("¥", "").replace("$", "").strip()
    try:
        shipping = round(float(ship_str), 2)
        fields["预估运费"] = shipping
    except (ValueError, TypeError):
        pass

    # 重量 — 保留克数（直接数字）
    weight_str = info.weight.replace("g", "").replace("kg", "").strip()
    try:
        weight = float(weight_str)
        fields["重量(g)"] = weight
    except (ValueError, TypeError):
        pass

    # 变体 — type=1→颜色, type=2→尺码（始终固定映射）
    if info.color_variants:
        fields["颜色"] = ", ".join(v["name"] for v in info.color_variants)
    if info.size_variants:
        fields["尺码"] = ", ".join(v["name"] for v in info.size_variants)

    # 包装规格 — 仅英制 + 毛重
    pkg = []
    if info.package_specs:
        imp = info.package_specs.get("imperial", "")
        gw = info.package_specs.get("gross_weight", "")
        if imp:
            pkg.append(imp)
        if gw:
            pkg.append(gw)
    fields["包装规格"] = " / ".join(pkg) if pkg else ""

    # 工厂
    if raw.get("factory"):
        fields["工厂"] = raw["factory"]

    # 出货周期
    if raw.get("delivery_period"):
        fields["出货周期"] = raw["delivery_period"]

    # 品类 — 页面 API > DOM > LLM 兜底
    category = raw.get("category", "")
    if not category or not category.strip():
        category = _infer_category(info.product_name, info.product_details)
        if category:
            print(f"   🏷️  LLM 品类补全: {category}")
        else:
            print("   ⚠️ 无法推断品类，留空")
    if category:
        fields["品类"] = category

    # 商品详情 — 去价格阶梯，截 5000 字
    details = info.product_details
    if "📊 价格阶梯" in details:
        details = details.split("📊 价格阶梯")[0].strip()
    if "📊 价格阶梯" in details:  # 兼容 emoji 变体
        details = details.split("📊 价格阶梯")[0].strip()
    fields["商品详情"] = details[:5000]

    # ── 定价计算 ──
    price_val = fields.get("单价 (1件)")
    ship_val = fields.get("预估运费")
    if price_val is not None and ship_val is not None:
        try:
            engine = PricingEngine()
            pricing = engine.calculate(cny_unit=float(price_val), cny_ship=float(ship_val))
            fp = pricing["final_pricing"]
            calc = pricing["calculations"]
            fields["最终售价_USD"] = fp["final_price_usd"]
            fields["成本_USD"] = calc["cost_usd"]
            fields["预估利润_USD"] = fp["profit_usd"]
            fields["实际利润率"] = fp["actual_profit_margin"]
            print(f"   💰 定价: ${fp['final_price_usd']} (成本${calc['cost_usd']:.2f}, "
                  f"利润${fp['profit_usd']}, 利润率{fp['actual_profit_margin']})")
        except Exception as e:
            print(f"   ⚠️ 定价计算失败: {e}")

    return fields


# ── 图片处理 ──────────────────────────────────────────

def download_image(url: str, save_dir: str, idx: int) -> str | None:
    """从 OBS URL 下载图片到本地（带重试 + HiCustom session cookie）"""
    session = requests.Session()
    session.headers.update({
        "Referer": "https://jit.hicustom.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0",
        "Accept": "image/avif,image/webp,image/apng,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    # 加载 HiCustom 登录会话的 cookies（解决 CDN 403 问题）
    session_file = Path.home() / ".hicustom_session" / "state.json"
    if session_file.exists():
        try:
            with open(session_file) as f:
                state = json.load(f)
            for cookie in state.get("cookies", []):
                if "hicustom" in cookie.get("domain", "") or "hihumbird" in cookie.get("domain", ""):
                    session.cookies.set(cookie["name"], cookie["value"], domain=cookie.get("domain", ""))
        except Exception:
            pass
    for attempt in range(3):
        try:
            resp = session.get(url, timeout=15)
            if resp.status_code == 200:
                filepath = os.path.join(save_dir, f"img_{idx}.jpeg")
                with open(filepath, "wb") as f:
                    f.write(resp.content)
                return filepath
            else:
                if attempt < 2:
                    time.sleep(1)
                    continue
                print(f"   ⚠️ 图片 {idx+1} 下载失败: HTTP {resp.status_code}")
                return None
        except Exception as e:
            if attempt < 2:
                time.sleep(1)
                continue
            print(f"   ⚠️ 图片 {idx+1} 下载异常: {e}")
            return None
    return None


def upload_images(client: FeishuClient, image_urls: list[str], product_name: str) -> list[dict]:
    """下载并上传所有图片，返回 file_token 列表"""
    attachments = []
    with tempfile.TemporaryDirectory() as tmpdir:
        for i, url in enumerate(image_urls[:5]):  # 最多 5 张
            print(f"   📷 图片 {i+1}/{min(len(image_urls), 5)}: 下载中...")
            local = download_image(url, tmpdir, i)
            if not local:
                continue

            safe_name = product_name[:20].replace("/", "-").replace(" ", "_")
            filename = f"{safe_name}_{i+1}.jpeg"

            try:
                token = client.upload_image(local, filename)
                attachments.append({"file_token": token})
                print(f"   ✅ 图片 {i+1} 上传成功")
            except Exception as e:
                print(f"   ❌ 图片 {i+1} 上传失败: {e}")

    return attachments


# ── 主流程 ────────────────────────────────────────────

def sync(
    product_url: str,
    zip_code: str = "33101",
    headless: bool | None = None,
    force_login: bool = False,
    force_update: bool = False,
    skip_images: bool = False,
    timeout: int = 60,
    debug: bool = False,
    interactive_freight: bool = True,
    design: str = "",
) -> dict:
    """
    一键提取 + 同步

    返回: {"ok": bool, "action": str, "record_id": str, "table_url": str, "error": str}
    """
    result = {
        "ok": False,
        "action": "error",
        "record_id": None,
        "table_url": TABLE_URL,
        "error": None,
    }

    # ═══ Step 1: 提取商品信息 ═══
    print("📦 [1/3] 提取商品信息...")
    try:
        info = extract_product(
            url=product_url,
            zip_code=zip_code,
            headless=headless,
            force_login=force_login,
            timeout=timeout,
            debug=debug,
            interactive_freight=interactive_freight,
        )
    except Exception as e:
        result["error"] = f"提取失败: {e}"
        return result

    if not info.product_name:
        result["error"] = "未提取到商品名称（可能需要登录）"
        return result

    # ── 数据质量门禁：拒绝明显异常的提取结果 ──
    LOGIN_KEYWORDS = ["登录", "注册", "扫码", "login", "register", "verify", "二维码"]
    if any(kw in info.product_name for kw in LOGIN_KEYWORDS):
        result["error"] = f"疑似未登录（商品名含敏感词: {info.product_name}）"
        return result
    if not info.unit_price or info.unit_price in ("¥", "$", "¥0", "$0"):
        result["error"] = f"未提取到有效单价: {info.unit_price}"
        return result

    result["product"] = {
        "name": info.product_name,
        "price": info.unit_price,
        "shipping": info.estimated_shipping,
        "weight": info.weight,
        "colors": [c["name"] for c in info.color_variants],
        "sizes": [s["name"] for s in info.size_variants],
        "package": info.package_specs.get("imperial", ""),
    }

    # ═══ Step 2: 映射字段 ═══
    print("🔄 [2/3] 映射字段...")
    fields = map_fields(info, design)

    # ═══ Step 3: 上传图片 & 写飞书 ═══
    print("📝 [3/3] 写入飞书...")
    client = FeishuClient()

    # 上传图片
    if info.images and not skip_images:
        attachments = upload_images(client, info.images, info.product_name)
        if attachments:
            fields["图片"] = attachments
            print(f"   🖼️  共上传 {len(attachments)} 张图片")
    elif skip_images:
        print("   ⏭️  跳过图片上传")

    # 写入记录（upsert：有则更新、无则创建）
    resp = client.upsert_record(fields)
    if resp.get("code") == 0:
        record_id = resp["data"]["record"]["record_id"]
        result["ok"] = True
        result["action"] = resp.get("action", "created")
        result["record_id"] = record_id
        print(f"   ✅ 记录已{result['action']}: {record_id}")
    else:
        result["error"] = f"飞书 API 错误: {resp.get('msg', resp)}"
        return result

    print(f"\n   📊 查看: {TABLE_URL}")
    return result


# ── CLI ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="HiCustom → 飞书 一键同步 Bot"
    )
    parser.add_argument("url", help="商品详情页 URL")
    parser.add_argument("--zip", default="33101", help="预估运费邮编")
    parser.add_argument("--output", choices=["json", "text"], default="text")
    parser.add_argument("--force-login", action="store_true", help="强制重新登录")
    parser.add_argument("--no-images", action="store_true", help="跳过图片上传")
    parser.add_argument("--timeout", type=int, default=60, help="加载超时(秒)")
    parser.add_argument("--debug", action="store_true", help="导出调试文件")
    parser.add_argument("--no-interactive-freight", action="store_false",
                        dest="interactive_freight", default=True,
                        help="关闭交互式运费试算，使用API拦截模式（默认：开启交互式试算）")
    parser.add_argument("--design", default="", help="设计方案（如：钓鱼款、伴郎款），同一商品可有多条设计方案")

    args = parser.parse_args()

    # 判断是否无头模式：有会话默认无头，无会话弹窗登录
    session_file = Path.home() / ".hicustom_session" / "state.json"
    if args.force_login:
        headless = False
    else:
        headless = session_file.exists()

    result = sync(
        product_url=args.url,
        zip_code=args.zip,
        headless=headless,
        force_login=args.force_login,
        skip_images=args.no_images,
        timeout=args.timeout,
        debug=args.debug,
        interactive_freight=args.interactive_freight,
        design=args.design,
    )

    if args.output == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print()
        if result["ok"]:
            print(f"✅ 同步成功!")
            p = result["product"]
            print(f"   商品: {p['name']}")
            print(f"   价格: {p['price']}")
            print(f"   运费: {p['shipping']}")
            print(f"   重量: {p['weight']}")
            print(f"   颜色: {'、'.join(p['colors'])}")
            print(f"   尺码: {'、'.join(p['sizes'])}")
            print(f"   记录: {result['record_id']}")
            print(f"   表格: {result['table_url']}")
        else:
            print(f"❌ 错误: {result.get('error', '未知错误')}")
        print()


if __name__ == "__main__":
    main()

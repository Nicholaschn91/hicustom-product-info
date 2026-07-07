#!/usr/bin/env python3
"""
HiCustom 分类页 → 飞书 批量同步（每品独立进程，数据一致）

用法:
  python batch_sync_to_feishu.py "https://jit.hicustom.com/.../chooseProduct?recommend_id=xxx"
"""
import argparse, sys, time, subprocess, json
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from playwright.sync_api import sync_playwright

APIGW = "https://apigw.hihumbird.com"
SCRIPTS_DIR = Path(__file__).parent
SYNC_SCRIPT = SCRIPTS_DIR / "sync_to_feishu.py"
TABLE_URL = (
    "https://tcnqenxcd30d.feishu.cn/base/ONy9bZ0oFaaiSEsf4ggcs61enRc"
    "?table=tbl75glY29VulRLm&view=vew81hvLSx"
)
PYTHON = "C:/Users/nicho/.workbuddy/binaries/python/envs/default/Scripts/python.exe"

# 从配置文件加载飞书密钥
_config_path = SCRIPTS_DIR.parent / "references" / "config.json"
_feishu_cfg = {}
if _config_path.exists():
    import json
    with open(_config_path) as f:
        _feishu_cfg = json.load(f).get("feishu", {})
FEISHU_APP_ID = _feishu_cfg.get("app_id", "")
FEISHU_APP_SECRET = _feishu_cfg.get("app_secret", "")
FEISHU_BASE = _feishu_cfg.get("base_token", "")
FEISHU_TABLE = _feishu_cfg.get("table_id", "")


def get_existing_product_ids() -> set:
    """查询飞书中已有的商品ID，返回集合"""
    import requests
    try:
        r = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        )
        token = r.json()["tenant_access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        existing = set()
        page_token = None
        while True:
            params = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            r = requests.get(
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_BASE}"
                f"/tables/{FEISHU_TABLE}/records",
                headers=headers, params=params,
            )
            data = r.json().get("data", {})
            for rec in data.get("items", []):
                pid = rec.get("fields", {}).get("商品ID", "")
                if pid:
                    existing.add(str(pid))
            if not data.get("has_more"):
                break
            page_token = data.get("page_token")
        return existing
    except Exception:
        return set()


def get_product_ids(category_url: str) -> list[dict]:
    parsed = urlparse(category_url)
    qs = parse_qs(parsed.query)
    recommend_id = qs.get("recommend_id", [None])[0]
    if not recommend_id:
        return []

    session_file = Path.home() / ".hicustom_session" / "state.json"
    if not session_file.exists():
        return []

    # 用副本，避免污染原session
    import shutil, tempfile
    tmp_session = Path(tempfile.gettempdir()) / "hicustom_batch_session.json"
    shutil.copy2(session_file, tmp_session)
    
    list_data = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(storage_state=str(tmp_session), viewport={"width": 1920, "height": 1080})
        page = context.new_page()

        # 只打开页面获取 cookies，不依赖 interceptor
        try:
            page.goto(category_url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass
        page.wait_for_timeout(5000)

        # 所有页面统一用 page.evaluate 调 API，避免 interceptor 重复捕获
        result = page.evaluate("""
            async (p) => {
                const r = await fetch(p.url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(p.body)});
                return await r.json();
            }
        """, {
            "url": f"{APIGW}/spu-itg/uct/v1/spus/page",
            "body": {"page": 1, "page_size": 20, "is_filter_reference": 1,
                     "accept_supply": True, "accept_all_group_sort_center": True,
                     "recommend_id": recommend_id, "accept_factory_app": True,
                     "label_intersect": True, "price_filters": [], "weight_filters": [],
                     "delivery_period_filters": [], "platform_label_ids": [], "label_ids": [],
                     "urgent_period_filters": [], "sort_center_app_ids": [],
                     "name_filter_type": "1", "types": ["1"],
                     "sort": [{"sort_by": "position", "sort_type": 2},
                              {"sort_by": "sale_time", "sort_type": 2}],
                     "sort_type": 1, "currency_id": 1, "app_id": 2483999,
                     "is_filter_group": True, "accept_multi_currency": True,
                     "accept_currency_support": True},
        })
        d = result.get("data", {})
        if d.get("list"):
            list_data.append(d)
            total = d.get("total", 0)
            page_size = d.get("page_size", 20)
            total_pages = (total + page_size - 1) // page_size
            print(f"📊 {total} 件, {total_pages} 页")

            for pg in range(2, total_pages + 1):
                result = page.evaluate("""
                    async (p) => {
                        const r = await fetch(p.url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(p.body)});
                        return await r.json();
                    }
                """, {
                    "url": f"{APIGW}/spu-itg/uct/v1/spus/page",
                    "body": {"page": pg, "page_size": page_size, "is_filter_reference": 1,
                             "accept_supply": True, "accept_all_group_sort_center": True,
                             "recommend_id": recommend_id, "accept_factory_app": True,
                             "label_intersect": True, "price_filters": [], "weight_filters": [],
                             "delivery_period_filters": [], "platform_label_ids": [], "label_ids": [],
                             "urgent_period_filters": [], "sort_center_app_ids": [],
                             "name_filter_type": "1", "types": ["1"],
                             "sort": [{"sort_by": "position", "sort_type": 2},
                                      {"sort_by": "sale_time", "sort_type": 2}],
                             "sort_type": 1, "currency_id": 1, "app_id": 2483999,
                             "is_filter_group": True, "accept_multi_currency": True,
                             "accept_currency_support": True},
                })
                if result.get("data", {}).get("list"):
                    list_data.append(result["data"])
                time.sleep(0.3)

        browser.close()

    items = []
    for ld in list_data:
        items.extend(ld.get("list", []))

    # 筛选：只保留中国(CN)和美国(US)发货的商品
    filtered = []
    skipped = 0
    for item in items:
        sf = item.get("supply_factory", [])
        if sf:
            country = sf[0].get("sort_center_app_country_code", "")
            if country in ("CN", "US"):
                filtered.append(item)
            else:
                skipped += 1
        else:
            # 没有工厂信息的保守保留
            filtered.append(item)
    if skipped:
        print(f"   ⏭️  跳过 {skipped} 件非中美仓商品")
    return filtered


def main():
    parser = argparse.ArgumentParser(description="HiCustom 分类页 → 飞书 批量同步（独立进程）")
    parser.add_argument("url", help="分类页 URL")
    parser.add_argument("--zip", default="33101", help="邮编")
    args = parser.parse_args()

    items = get_product_ids(args.url)
    if not items:
        print("❌ 未获取到商品列表")
        return

    print(f"\n📦 {len(items)} 件，逐品独立进程采集...\n")

    # 去重：查飞书中已有商品ID
    print("🔍 检查已有记录...", end=" ", flush=True)
    existing_ids = get_existing_product_ids()
    print(f"{len(existing_ids)} 条已有")

    ok = 0
    fail = 0
    skip = 0

    for i, item in enumerate(items):
        spu_id = item["id"]
        define_id = item.get("define_id", "")
        name = item.get("name", "?")

        if str(spu_id) in existing_ids:
            print(f"[{i+1}/{len(items)}] {name} ...", end=" ", flush=True)
            # 已存在也重新跑（sync_to_feishu 会用 upsert 更新运费）

        detail_url = (
            f"https://jit.hicustom.com/merchant/fnsz-sale/productDetail"
            f"?id={spu_id}&isFenxiaoMerchant=-1&rel_app_id=2483999"
            f"&define_id={define_id}&fromPool=1&currency_id=1&appId=2607679"
        )
        print(f"[{i+1}/{len(items)}] {name} ...", end=" ", flush=True)

        cmd = [PYTHON, str(SYNC_SCRIPT), detail_url, "--zip", args.zip]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                                cwd=str(SCRIPTS_DIR))

        output = result.stdout + result.stderr
        if "同步成功" in output:
            ok += 1
            # 提取关键信息
            for line in output.split("\n"):
                line = line.strip()
                if "记录:" in line:
                    print(f"✅ {line.split(':')[1].strip()}")
                elif "价格:" in line:
                    price = line.split(":")[1].strip()
                elif "运费:" in line:
                    ship = line.split(":")[1].strip()
            # 也打印价格运费
            for line in output.split("\n"):
                if "价格:" in line:
                    print(f"   {line.strip()}")
                elif "运费:" in line:
                    print(f"   {line.strip()}")
        else:
            fail += 1
            err = "?".join([l for l in output.split("\n") if "错误" in l][:1]) or "?"
            print(f"❌ {err[:80]}")

        time.sleep(2)  # 间隔，避免浏览器资源冲突

    print(f"\n{'='*50}")
    print(f"✅ {ok} | ❌ {fail} | ⏭️  {skip} (已存在)")
    print(f"📊 {TABLE_URL}")


if __name__ == "__main__":
    main()

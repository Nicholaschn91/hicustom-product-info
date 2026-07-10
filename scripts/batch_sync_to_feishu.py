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
                fields = rec.get("fields", {})
                pid = fields.get("商品ID", "")
                if pid:
                    existing.add(str(pid))
                # 也收集「同款商品ID」中的商品ID
                tongkuan = fields.get("同款商品ID", "")
                if tongkuan:
                    for tk in str(tongkuan).split(","):
                        tk = tk.strip()
                        if tk:
                            existing.add(tk)
            if not data.get("has_more"):
                break
            page_token = data.get("page_token")
        return existing
    except Exception:
        return set()


def get_product_ids(category_url: str) -> list[dict]:
    """用 page.route 拦截页面真实 API 请求体，再用 evaluate 重放翻页拉取全量商品"""
    session_file = Path.home() / ".hicustom_session" / "state.json"
    if not session_file.exists():
        return []
    import shutil, tempfile
    tmp_session = Path(tempfile.gettempdir()) / "hicustom_batch_session.json"
    shutil.copy2(session_file, tmp_session)
    list_data = []
    cap_body = {"v": None}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(storage_state=str(tmp_session), viewport={"width": 1920, "height": 1080})
        page = context.new_page()
        def on_route(route):
            if cap_body["v"] is None:
                u = route.request.url
                if ("spus" in u or "spu-itg" in u) and route.request.method == "POST":
                    try:
                        cap_body["v"] = json.loads(route.request.post_data or "{}")
                    except: pass
            route.continue_()
        page.route("**/*", on_route)
        try: page.goto(category_url, wait_until="load", timeout=60000)
        except: pass
        page.wait_for_timeout(10000)
        browser.close()
    real_body = cap_body["v"]
    if not real_body: return []
    real_body["page"], real_body["page_size"] = 1, 20
    with sync_playwright() as p:
        b2 = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = b2.new_context(storage_state=str(session_file))
        pg = ctx.new_page()
        try: pg.goto(category_url, wait_until="domcontentloaded", timeout=30000)
        except: pass
        pg.wait_for_timeout(3000)
        r = pg.evaluate("""async (b) => { const res = await fetch('https://apigw.hihumbird.com/spu-itg/uct/v1/spus/page',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}); return await res.json(); }""", real_body)
        d = r.get("data",{})
        if d.get("list"): list_data.append(d)
        total = d.get("total", 0)
        print(f"📊 {total} 件, {(total+19)//20} 页")
        for pg_num in range(2, (total+19)//20+1):
            real_body["page"] = pg_num
            r = pg.evaluate("""async (b) => { const res = await fetch('https://apigw.hihumbird.com/spu-itg/uct/v1/spus/page',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}); return await res.json(); }""", real_body)
            if r.get("data",{}).get("list"): list_data.append(r["data"])
        b2.close()
    items = []
    for ld in list_data: items.extend(ld.get("list", []))
    return items
    items = []
    for ld in list_data: items.extend(ld.get("list", []))
    return items
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

        detail_url = (
            f"https://jit.hicustom.com/merchant/fnsz-sale/productDetail"
            f"?id={spu_id}&isFenxiaoMerchant=-1&rel_app_id=2483999"
            f"&define_id={define_id}&fromPool=1&currency_id=1&appId=2607679"
        )
        print(f"[{i+1}/{len(items)}] {name} ...", end=" ", flush=True)

        # 文本模式 + 失败重试 1 次
        success = False
        last_err = ""
        for attempt in range(2):
            cmd = [PYTHON, str(SYNC_SCRIPT), detail_url, "--zip", args.zip]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                                    cwd=str(SCRIPTS_DIR))
            output = result.stdout + result.stderr
            if "同步成功" in output:
                success = True
                # 提取记录ID和运费摘要
                for line in output.split("\n"):
                    if "记录:" in line:
                        print(f"✅ {line.split('记录:')[1].strip()}", end=" ")
                    elif "运费:" in line:
                        print(f"| {line.strip()}")
                        break
                break
            else:
                # 提取错误信息
                errs = [l for l in output.split("\n") if "错误" in l]
                last_err = errs[0][:60] if errs else "无输出"
                if attempt == 0:
                    print(f"⚠️ 重试", end=" ", flush=True)
                time.sleep(3)

        if success:
            ok += 1
        else:
            fail += 1
            print(f"❌ {last_err}")

        time.sleep(2)  # 间隔，避免浏览器资源冲突

    print(f"\n{'='*50}")
    print(f"✅ {ok} | ❌ {fail} | ⏭️  {skip} (已存在)")
    
    # ── 自检核验：比对分类页商品数 vs 飞书实际覆盖数 ──
    print("\n🔍 自检...")
    cat_product_ids = {str(it["id"]) for it in items}
    feishu_all = get_existing_product_ids()  # 含主ID + 同款ID
    covered = cat_product_ids & feishu_all
    missing = cat_product_ids - feishu_all
    print(f"   分类页: {len(cat_product_ids)} 件")
    print(f"   飞书覆盖: {len(covered)}/{len(cat_product_ids)}")
    if missing:
        preview = ", ".join(list(missing)[:5])
        print(f"   ⚠️ 遗漏 {len(missing)}: {preview}{'...' if len(missing)>5 else ''}")
    else:
        print(f"   ✅ 全覆盖")
    print(f"📊 {TABLE_URL}")


if __name__ == "__main__":
    main()

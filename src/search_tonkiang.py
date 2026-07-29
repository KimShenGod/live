#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
search_tonkiang.py —— 根据 alias2.txt 中的频道名称，从 tonkiang.us 搜索并收集直播源。

特性：
  1. 读取 alias2.txt（主名 + 别名），对每个频道用主名及普通别名作为搜索词去 tonkiang.us 搜索；
  2. 由于 tonkiang.us 有 Cloudflare + reCAPTCHA 保护，本脚本使用 Selenium 真实浏览器：
       - 首次运行会弹出浏览器，请手动点一下“我不是机器人”/reCAPTCHA，按回车继续；
       - 解决后可把 cookie 存到文件，后续复用（--cookies），无需再次人工验证；
  3. 正确的搜索方式：在首页搜索框（#search）输入关键词并提交表单（POST /?，参数名 seerch）；
  4. 直播源地址位于页面 onlick=nliujb("真实URL") 中，且每条源都带“校验日期”
     （如 “07-23-2026 Shanghai checked”）。脚本按该校验日期过滤，只保留
     最近 --days 天（默认 30）内被校验过的源 —— 即“最近一个月”的直播源；
  5. 自动翻页收集所有分页结果；输出标准 m3u，按频道分组（group-title 用主名）。

依赖：
  pip install selenium undetected-chromedriver
  需要本机已安装 Chrome 与匹配的 chromedriver（同 itv_all.py 环境）。
  说明：tonkiang.us 受 Cloudflare 保护，会用“静默拦截”拒绝自动化浏览器（不弹验证码、
  但搜不到源）。undetected-chromedriver 可绕过该检测；若未安装则回退普通 Selenium，
  此时多半仍搜不到，请按提示安装。

用法示例：
  python src/search_tonkiang.py
  python src/search_tonkiang.py -i alias2.txt -o tonkiang_channels.m3u --days 30
  python src/search_tonkiang.py --cookies tonkiang_cookies.json   # 复用已保存的 cookie，跳过验证码
"""
import os
import re
import sys
import json
import time
import argparse
import datetime

# --------------------------------------------------------------------------- #
# 直播源链接识别
# --------------------------------------------------------------------------- #
# 明确排除的非直播源域名（站点自身、广告、统计、验证码、播放器跳转等）
BAD_DOMAINS = (
    'tonkiang.us', 'otieu.com', 'recaptcha', 'gstatic', 'cloudflare',
    'google.com', 'googleapis', 'doubleclick', 'histats', 'facebook',
    'twitter', 'youtube', 'w3.org', 'schema.org', 'zqjy.info',
)


def is_stream_url(u: str) -> bool:
    """nliujb() 里抓到的已是真实直链，这里只拦掉广告/统计等脏域名。"""
    low = u.lower()
    if any(b in low for b in BAD_DOMAINS):
        return False
    if low.startswith(('http://', 'https://', 'rtmp://', 'rtsp://')):
        return True
    return False


def normalize_url(u: str) -> str:
    # 去掉 nliujb("...") 里偶尔带出的首尾空格/引号
    return u.strip().strip('"\' ')


def extract_results(html: str):
    """从单页结果 HTML 中抽取 [(url, check_date_str_or_None), ...]。

    真实页面结构（实测，class 名会随请求随机变化）：
      <tba class="nliujb">  http://178.219.128.68:64888/AXN</tba>
      <tba class="dvnliu">  http://181.143.237.211:85/tsfile/live/1026_1.m3u8?key=txiptv</tba>
      ...
      <i>07-23-2026 Shanghai checked</i>
    即源 URL 在某个 <tba class="..."> ... </tba> 文本内（class 名不定，用通配），
    URL 可能含 HTML 实体（如 &amp;）。其后的 <i>MM-DD-YYYY ... checked</i> 是校验日期。
    """
    import html as _html
    out = []
    # class 名无关：只要 <tba ...> 里包着一个 http(s) 链接即视为源
    for m in re.finditer(r'<tba class="[^"]*">\s*(https?://[^\s<]+?)\s*</tba>', html):
        raw = m.group(1).strip()
        if not raw:
            continue
        url = normalize_url(_html.unescape(raw))  # 解码 &amp; -> &
        if not is_stream_url(url):
            continue
        # 在该源之后寻找最近的校验日期 <i>MM-DD-YYYY ... checked</i>
        after = html[m.end():]
        dm = re.search(r'<i>\s*(\d{1,2}-\d{1,2}-\d{4})\s[^<]*checked', after)
        date_str = dm.group(1) if dm else None  # "MM-DD-YYYY"
        out.append((url, date_str))
    return out


# --------------------------------------------------------------------------- #
# alias2.txt 解析
# --------------------------------------------------------------------------- #
def load_aliases(path: str):
    """返回 [(主名, [搜索词1, 搜索词2, ...]), ...]。
    搜索词 = 主名 + 非正则别名（re: 开头的正则别名不用于搜索）。"""
    channels = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = [p.strip() for p in line.split(',') if p.strip()]
            if not parts:
                continue
            main = parts[0]
            search_terms = [main]
            for alias in parts[1:]:
                if alias.lower().startswith('re:'):
                    continue
                search_terms.append(alias)
            channels.append((main, search_terms))
    return channels


# --------------------------------------------------------------------------- #
# 浏览器 / 搜索
# --------------------------------------------------------------------------- #
def create_chrome_driver(headless: bool = False):
    """创建 Chrome 驱动。

    优先使用 undetected-chromedriver（uc），它专门对 ChromeDriver 打补丁以绕过
    Cloudflare 的机器人识别（静默拦截不会弹验证码，但会悄悄拒绝自动化请求）。
    未安装 uc 时回退到普通 Selenium，并尽量隐藏自动化特征。
    """
    from selenium.webdriver.chrome.options import Options

    # 注意：不要手动覆盖 UA。脚本里若写死 Chrome/120，而本机其实是 Chrome/150，
    # UA 与真实浏览器版本不符，反而会被 Cloudflare 当成伪造流量拦截。
    # 让 Chrome 使用其原生 UA 最稳妥。
    base = Options()
    base.add_argument("--disable-gpu")
    base.add_argument("--no-sandbox")
    base.add_argument("--disable-dev-shm-usage")

    try:
        import undetected_chromedriver as uc

        def _uc_opts():
            o = Options()
            for a in base.arguments:
                o.add_argument(a)
            if headless:
                o.add_argument("--headless=new")
            return o

        try:
            driver = uc.Chrome(options=_uc_opts(), headless=headless)
        except Exception as e:
            # uc 自动下载的 driver 大版本可能与已装 Chrome 不匹配，报错形如：
            # "This version of ChromeDriver only supports Chrome version 151
            #  Current browser version is 150.0.7871.127"
            # 从报错里解析真实 Chrome 大版本，显式指定 version_main 重试一次。
            # 注意：重试必须传入“新的” Options 对象，不能复用上一次的，
            # 否则 uc 会报 "you cannot reuse the ChromeOptions object"。
            m = re.search(r'browser version is (\d+)', str(e))
            if m:
                vm = int(m.group(1))
                print(f"[WARN] uc driver 版本不匹配，改用 version_main={vm} 重试")
                driver = uc.Chrome(options=_uc_opts(), headless=headless, version_main=vm)
            else:
                raise
        print("[INFO] 使用 undetected-chromedriver（增强反爬 / 绕过 Cloudflare 静默拦截）")
    except Exception as e:
        print(f"[WARN] 未使用 undetected-chromedriver（{e}），回退普通 Selenium。")
        print("       若仍搜不到源，请执行: pip install undetected-chromedriver")
        if headless:
            base.add_argument("--headless=new")
        base.add_experimental_option("excludeSwitches", ["enable-automation"])
        base.add_experimental_option('useAutomationExtension', False)
        base.add_argument("--disable-blink-features=AutomationControlled")
        try:
            from selenium import webdriver
            driver = webdriver.Chrome(options=base)
        except Exception as e2:
            print(f"[ERROR] 创建 Chrome WebDriver 失败: {e2}")
            print("请确认已安装 Chrome 以及与之匹配的 chromedriver，并已加入 PATH。")
            sys.exit(1)

    # 额外反检测：抹掉 navigator.webdriver 指纹（部分站点据此判定 Selenium）
    try:
        driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
            'source': 'Object.defineProperty(navigator, "webdriver", {get: () => undefined});'
        })
    except Exception:
        pass
    return driver


def load_cookies(driver, cookie_file: str):
    if not os.path.exists(cookie_file):
        return False
    try:
        with open(cookie_file, 'r', encoding='utf-8') as f:
            cookies = json.load(f)
        driver.get("https://tonkiang.us/")
        for c in cookies:
            try:
                driver.add_cookie(c)
            except Exception:
                pass
        print(f"[INFO] 已从 {cookie_file} 加载 {len(cookies)} 个 cookie")
        return True
    except Exception as e:
        print(f"[WARN] 加载 cookie 失败: {e}")
        return False


def save_cookies(driver, cookie_file: str):
    try:
        cookies = driver.get_cookies()
        with open(cookie_file, 'w', encoding='utf-8') as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
        print(f"[INFO] 已将当前 cookie 保存到 {cookie_file}（下次可用 --cookies 复用）")
    except Exception as e:
        print(f"[WARN] 保存 cookie 失败: {e}")


def _is_captcha_page(driver) -> bool:
    """检测真正的人机验证/拦截页。

    注意：tonkiang.us 受 Cloudflare 保护，普通页面源码里就带有 /cdn-cgi/、cf-ray 等
    字样，不能作为“被拦截”的依据，否则会误报并反复弹验证。这里只认真正的验证页特征
    （特定标题，或“验证你是人类 / 检查浏览器 / 启用 JS”等专属文案）。
    """
    try:
        src = (driver.page_source or '').lower()
    except Exception:
        src = ''
    try:
        title = (driver.title or '').lower()
    except Exception:
        title = ''
    # 仅这些强特征才算“被拦截”；普通 Cloudflare 页面不会命中
    if 'just a moment' in title or 'attention required' in title:
        return True
    strong = ('verify you are human', 'checking your browser',
              'enable javascript and cookies', 'recaptcha', 'not a robot')
    return any(m in src for m in strong)


def wait_pass_captcha(driver, cookie_file):
    """该站点已确认无需人机验证，这里仅做无害提示，不再阻塞等待人工操作。"""
    print("[WARN] 疑似遇到验证/拦截页，但按配置不做人工验证，将继续尝试"
          "（若长期搜不到，请检查 UA/网络）。")
    return


def _wait_results(driver, timeout, cookie_file):
    """等待结果加载（出现 nliujb 或 resultplus），期间若遇验证码则提示人工处理。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _is_captcha_page(driver):
            wait_pass_captcha(driver, cookie_file)
            continue
        html = driver.page_source or ''
        if 'nliujb(' in html or 'resultplus' in html:
            return html
        time.sleep(1.5)
    return driver.page_source or ''


def _dump_debug(driver, query, html):
    """打印当前页面的关键标记，便于排查“搜不到源”的原因（不落盘 HTML 文件）。"""
    print(f"[DEBUG] [{query}] 页面长度 {len(html or '')}")
    low = (html or '').lower()
    try:
        title = driver.title
    except Exception:
        title = ''
    print(f"[DEBUG] 标记 -> resultplus={'resultplus' in low}, "
          f"nliujb={'nliujb(' in low}, blocked={_is_captcha_page(driver)}, title={title!r}")


def search_channel(driver, query, days, timeout=25, cookie_file=None, debug=False):
    """在 tonkiang.us 搜索某词，返回 {url: check_date_str}（已按日期过滤到近 days 天）。"""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys

    def _goto_home():
        driver.get("https://tonkiang.us/")
        time.sleep(1.5)
        if _is_captcha_page(driver):
            wait_pass_captcha(driver, cookie_file)

    # 优先在当前页面用搜索框（保留会话，避免每次重载首页重新触发验证）
    try:
        inp = driver.find_element(By.ID, 'search')
    except Exception:
        _goto_home()
        try:
            inp = driver.find_element(By.ID, 'search')
        except Exception as e:
            print(f"[WARN] 找不到搜索框（{query}）: {e}")
            return {}

    inp.clear()
    inp.send_keys(query)
    inp.send_keys(Keys.ENTER)  # 回车提交，兼容被 JS 拦截的表单

    html = _wait_results(driver, timeout, cookie_file)
    if debug:
        _dump_debug(driver, query, html)

    # 结果页若仍是拦截页，再提示一次并等待
    if _is_captcha_page(driver):
        wait_pass_captcha(driver, cookie_file)
        html = _wait_results(driver, timeout, cookie_file)
        if debug:
            _dump_debug(driver, query + '_after_captcha', html)

    # 重要：绝不能用 GET ?chname= 兜底！那样服务端返回 l=0 的“降级示例页”，
    # 无论哪个频道、翻多少页都只回同样 3 条陈旧源（实测 CCTV5 因此只剩 2-3 条老源）。
    # 只有通过搜索框提交拿到带会话令牌 l=xxxx 的结果页才是完整数据。
    # 若首次提交没拿到结果页，就回首页重试一次搜索框流程。
    if 'resultplus' not in (html or ''):
        print(f"[WARN] [{query}] 首次提交未到结果页，回首页重试")
        _goto_home()
        try:
            inp = driver.find_element(By.ID, 'search')
            inp.clear()
            inp.send_keys(query)
            inp.send_keys(Keys.ENTER)
            html = _wait_results(driver, timeout, cookie_file)
            if debug:
                _dump_debug(driver, query + '_retry', html)
        except Exception as e:
            print(f"[WARN] 重试搜索失败（{query}）: {e}")

    # 收集第 1 页
    collected = {}  # url -> check_date_str
    for url, ds in extract_results(html):
        collected[url] = ds

    # 翻页：分页条是“滑动窗口”（第 1 页可能只显示到第 8 页，实际可能有 17+ 页），
    # 必须在访问每一页时继续发现新的页码，直到没有新页。
    # 注意：driver.page_source 保留 &amp; 实体，需还原成 & 才能解析链接。
    _page_re = re.compile(r'''href=["']\?page=(\d+)&chname=([^&"']+)&l=([^"']+)["']''')

    html = html.replace('&amp;', '&')
    links = _page_re.findall(html)
    chname_enc = links[0][1] if links else None
    l_val = links[0][2] if links else None
    if l_val == '0':
        print(f"[WARN] [{query}] 命中降级页(l=0)，结果可能不完整")

    seen_pages = {1}
    todo = sorted({int(p) for p, _, _ in links if int(p) > 1})
    MAX_PAGES = 60  # 安全上限，防止异常页码导致无限翻页
    while todo and len(seen_pages) < MAX_PAGES and chname_enc and l_val:
        p = todo.pop(0)
        if p in seen_pages:
            continue
        seen_pages.add(p)
        driver.get(f"https://tonkiang.us/?page={p}&chname={chname_enc}&l={l_val}")
        phtml = _wait_results(driver, timeout, cookie_file)
        for u, ds in extract_results(phtml):
            collected[u] = ds
        # 从当前页继续发现新的页码（滑动窗口会露出更后面的页）
        phtml = phtml.replace('&amp;', '&')
        for np_, _, _ in _page_re.findall(phtml):
            n = int(np_)
            if n not in seen_pages and n not in todo:
                todo.append(n)
        todo.sort()

    # 按校验日期过滤：只保留最近 days 天内被校验过的源
    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=days)
    kept = {}
    for url, ds in collected.items():
        if ds:
            try:
                d = datetime.datetime.strptime(ds, "%m-%d-%Y").date()
                if d < cutoff:
                    continue
            except Exception:
                pass
        kept[url] = ds
    return kept


# --------------------------------------------------------------------------- #
# 历史 / 输出
# --------------------------------------------------------------------------- #
def load_history(path: str):
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_history(path: str, history):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def build_m3u(channels, history, days: int, output: str):
    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=days)
    total = 0
    with open(output, 'w', encoding='utf-8') as f:
        f.write('#EXTM3U\n')
        for main, _ in channels:
            umap = history.get(main, {})
            if not umap:
                continue
            kept = []
            for url, ds in umap.items():
                if ds:
                    try:
                        d = datetime.datetime.strptime(ds, "%m-%d-%Y").date()
                        if d < cutoff:
                            continue
                    except Exception:
                        pass
                kept.append(url)
            if not kept:
                continue
            f.write(f'#EXTINF:-1 group-title="{main}",{main}\n')
            for u in kept:
                f.write(f'{u}\n')
            total += len(kept)
    print(f"[INFO] 已写出 {output}：{len(channels)} 个频道配置，{total} 条近 {days} 天直播源")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(description='根据 alias2.txt 从 tonkiang.us 搜索直播源')
    parser.add_argument('-i', '--input', default='alias2.txt', help='alias2.txt 路径')
    parser.add_argument('-o', '--output', default='tonkiang_channels.m3u', help='输出 m3u 路径')
    parser.add_argument('--history', default='tonkiang_history.json', help='历史 JSON 路径')
    parser.add_argument('--cookies', default='tonkiang_cookies.json', help='cookie 缓存文件路径')
    parser.add_argument('--days', type=int, default=30, help='只保留最近 N 天校验过的源（默认 30）')
    parser.add_argument('--limit', type=int, default=0, help='最多处理前 N 个频道（0=全部）')
    parser.add_argument('--delay', type=float, default=1.5, help='每次搜索之间的间隔秒数')
    parser.add_argument('--headless', action='store_true', help='无头模式（需已提供可用 cookie）')
    parser.add_argument('--no-save-cookies', action='store_true', help='不保存 cookie 文件')
    parser.add_argument('--debug', action='store_true', help='保存搜索页 HTML 快照用于排查')
    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"[ERROR] 找不到输入文件: {args.input}")
        sys.exit(1)

    channels = load_aliases(args.input)
    if args.limit:
        channels = channels[:args.limit]
    print(f"[INFO] 共解析到 {len(channels)} 个频道（来自 {args.input}）")

    history = load_history(args.history)

    driver = create_chrome_driver(headless=args.headless)
    cookie_file = None if args.no_save_cookies else args.cookies
    try:
        if not args.headless:
            load_cookies(driver, args.cookies)
        driver.get("https://tonkiang.us/")
        time.sleep(2)
        if _is_captcha_page(driver):
            # 仅当首页确实出现验证页时才停下人工处理；否则直接继续（跳过验证）
            wait_pass_captcha(driver, cookie_file)
        elif cookie_file:
            # 静默保存 cookie 供后续复用，不打断流程
            save_cookies(driver, cookie_file)

        processed = 0
        for main, terms in channels:
            merged = {}  # url -> date，跨别名合并
            for term in terms:
                try:
                    found = search_channel(driver, term, args.days, cookie_file=cookie_file, debug=args.debug)
                except Exception as e:
                    print(f"[WARN] 搜索 [{term}] 出错: {e}")
                    found = {}
                for u, ds in found.items():
                    # 同一 URL 多个别名都搜到时，保留较新的校验日期
                    if u not in merged or (ds and (not merged[u] or ds > merged[u])):
                        merged[u] = ds
                time.sleep(args.delay)
            if merged:
                history[main] = merged
                print(f"[OK] {main}: 近 {args.days} 天 {len(merged)} 个源")
            else:
                print(f"[..] {main}: 未搜到近 {args.days} 天源")
            processed += 1
            if processed % 20 == 0:
                save_history(args.history, history)
        save_history(args.history, history)
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    build_m3u(channels, history, args.days, args.output)
    print("[DONE] 完成。")


if __name__ == "__main__":
    main()

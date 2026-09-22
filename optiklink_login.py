#!/usr/bin/env python3
"""
OptikLink 自动登录脚本 v4.3-fixed（仅必要修复版）
- 修复 Discord 授权参数（使用硬编码后备值）
- 增加 /error/vpn 检测
- 增加 Quick Verification 处理（SeleniumBase UC 模式浏览器过 Turnstile）
- 保持原始 Dashboard 判断逻辑不变
- 到期时间自动从页面提取
"""

import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs, urlencode

import requests as _req  # 始终可用，cloudscraper 依赖它

# 优先使用 cloudscraper 绕过 Cloudflare
try:
    import cloudscraper
    USE_CLOUDSCRAPER = True
    print("[信息] 使用 cloudscraper 绕过 Cloudflare 人机验证")
except ImportError:
    USE_CLOUDSCRAPER = False
    print("[警告] cloudscraper 未安装，将使用普通 requests，可能无法绕过 Cloudflare")

# ─────────────────────────────────────────────────────────────
# 配置（环境变量）
# ─────────────────────────────────────────────────────────────
DISCORD_TOKEN       = os.environ.get("DISCORD_TOKEN", "")
TG_BOT_TOKEN        = os.environ.get("BOT_TOKEN", "")
TG_CHAT_ID          = os.environ.get("CHAT_ID", "")
EXPIRE_DATE_RAW     = os.environ.get("EXPIRE_DATE", "")
DISCORD_CLIENT_ID   = os.environ.get("DISCORD_CLIENT_ID", "1005764586547838976")
DISCORD_REDIRECT_URI = os.environ.get("DISCORD_REDIRECT_URI", "https://optiklink.net/callback")
PANEL_URL           = os.environ.get("PANEL_URL", "https://control.optiklink.net")
PANEL_API_KEY       = os.environ.get("PANEL_API_KEY", "")
PANEL_SERVER_ID     = os.environ.get("PANEL_SERVER_ID", "")
SERVER_START_WAIT   = int(os.environ.get("SERVER_START_WAIT", "60"))
PROXY_URL           = os.environ.get("PROXY_URL", "")

# 硬编码后备参数（从成功的登录链接中提取）
FALLBACK_CLIENT_ID = "933437142254887052"
FALLBACK_REDIRECT_URI = "https://optiklink.com/login"
FALLBACK_SCOPE = "guilds guilds.join identify email"

HEADERS_BROWSER = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

# ─────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────
def mask(value: str, keep: int = 4) -> str:
    if not value:
        return "***"
    if len(value) <= keep * 2:
        return "***"
    return value[:keep] + "***" + value[-keep:]

def mask_url(url: str) -> str:
    return re.sub(r'(code|token|access_token|refresh_token)=[^&]+', r'\1=***', url)

def create_session():
    """创建带有代理和浏览器头部的会话"""
    if USE_CLOUDSCRAPER:
        sess = cloudscraper.create_scraper()
    else:
        sess = _req.Session()
    if PROXY_URL:
        sess.proxies = {"http": PROXY_URL, "https": PROXY_URL}
        print(f"[信息] 使用代理: {PROXY_URL}")
    else:
        print("[信息] 直连（无代理）")
    sess.headers.update(HEADERS_BROWSER)
    return sess

_tg_session = None

def tg_send(title: str, content: str):
    """发送 Telegram 消息（Markdown），带自动重试（3次）"""
    global _tg_session
    if _tg_session is None:
        _tg_session = _req.Session()
        _tg_session.headers.update({"Content-Type": "application/json"})

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    text = f"*{title}*\n\n{content}"
    payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True}

    for attempt in range(3):
        try:
            resp = _tg_session.post(url, json=payload, timeout=15)
            result = resp.json()
            if result.get("ok"):
                print(f"[Telegram] 推送成功 | chat_id={mask(TG_CHAT_ID)}")
                return
            print(f"[Telegram] 推送失败: {result.get('description', 'unknown')}")
        except Exception as e:
            print(f"[Telegram] 请求异常 (第{attempt+1}次): {e}")
        if attempt < 2:
            time.sleep(2)
    print("[Telegram] 推送最终失败（重试耗尽）")

# ─────────────────────────────────────────────────────────────
# 登录核心流程
# ─────────────────────────────────────────────────────────────
def discover_oauth_params(session):
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify email guilds",
    }
    print("[A] 探测 OAuth 参数...")
    r = session.get("https://optiklink.net/auth", timeout=15, headers=HEADERS_BROWSER, allow_redirects=True)
    print(f"    状态码: {r.status_code} 最终URL: {mask_url(r.url)}")
    found = False
    for pat in [r'https?://discord\.com(?:/api)?/oauth2/authorize[^\s\'"<>\\]+']:
        m = re.search(pat, r.text)
        if m:
            raw_url = m.group(0).replace("&amp;", "&")
            qs = parse_qs(urlparse(raw_url).query)
            for k in ("client_id", "redirect_uri", "scope", "state"):
                if qs.get(k):
                    params[k] = qs[k][0]
            found = True
            break
    if not found and "discord.com" in r.url:
        qs = parse_qs(urlparse(r.url).query)
        for k in ("client_id", "redirect_uri", "scope", "state"):
            if qs.get(k):
                params[k] = qs[k][0]
        found = True
    if params["client_id"] != DISCORD_CLIENT_ID:
        new_cid = params["client_id"]
        print(f"    client_id 变更: {mask(DISCORD_CLIENT_ID)} → {mask(new_cid)}")
        github_output = os.environ.get("GITHUB_OUTPUT")
        if github_output:
            with open(github_output, "a") as f:
                f.write(f"new_client_id={new_cid}\n")
    return params

def discord_authorize(session, oauth_params):
    """Discord授权 - 使用探测参数或硬编码后备值"""
    print("[B] Discord 授权...")
    
    # 优先使用探测到的参数，如果无效则使用硬编码后备值
    client_id = oauth_params.get("client_id") or FALLBACK_CLIENT_ID
    redirect_uri = oauth_params.get("redirect_uri") or FALLBACK_REDIRECT_URI
    scope = oauth_params.get("scope") or FALLBACK_SCOPE
    
    if oauth_params.get("client_id"):
        print(f"    [来源: 自动探测]")
    else:
        print(f"    [来源: 硬编码后备]")
    
    print(f"    client_id: {mask(client_id)}")
    print(f"    redirect_uri: {redirect_uri}")
    
    post_params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
    }
    if "state" in oauth_params:
        post_params["state"] = oauth_params["state"]
    
    headers = {
        "Authorization": DISCORD_TOKEN,
        "Content-Type": "application/json",
        "User-Agent": HEADERS_BROWSER["User-Agent"],
        "Referer": "https://discord.com/oauth2/authorize?" + urlencode(post_params),
        "X-Super-Properties": "eyJvcyI6IldpbmRvd3MiLCJicm93c2VyIjoiQ2hyb21lIn0=",
    }
    
    r = session.post("https://discord.com/api/v10/oauth2/authorize",
                     params=post_params,
                     json={"authorize": True, "permissions": "0"},
                     headers=headers, timeout=15, allow_redirects=False)
    print(f"    Discord 状态: {r.status_code}")
    
    if r.status_code == 200:
        try:
            data = r.json()
            if "location" in data:
                return data["location"]
        except:
            pass
    
    if r.status_code in (301,302,303,307,308) and "Location" in r.headers:
        return r.headers["Location"]
    
    raise RuntimeError(f"Discord 授权失败 HTTP {r.status_code}")

# ─────────────────────────────────────────────────────────────
# 浏览器登录 + Quick Verification
# ─────────────────────────────────────────────────────────────
# /login?code= 现在会先返回 Quick Verification 页面：随机数学题 + 真实
# Cloudflare Turnstile（managed 模式，服务端校验 cf-turnstile-response）。
# 纯 HTTP 造不出令牌；无头浏览器也会被拦（自动化指纹，实测令牌始终为空）。
# 必须真实有头浏览器 + OS 级鼠标点击，所以这一步用 SeleniumBase UC 模式。
# math_answer 和 cf-turnstile-response 都正确才会放行。
# 算式是多操作数链（实测 "2 + 2 + 1"），只取前两个数会把答案算错
VERIFY_STRONG_RE = re.compile(r'<strong[^>]*>([^<]*)</strong>', re.I)
VERIFY_TEXT_RE = re.compile(r'What\s+is\s+([0-9+\-×*/÷\s]+?)\s*\?', re.I)
MATH_TOKEN_RE = re.compile(r'\d+|[+\-×*/÷]')
TURNSTILE_TOKEN_JS = (
    'var e=document.querySelector(\'input[name="cf-turnstile-response"]\');'
    'return e ? String(e.value) : "";'
)
TURNSTILE_MAX_RETRY = 5      # 令牌没放行就再点一次 Turnstile 并重等，最多这些轮
TURNSTILE_POLL_STEP = 4       # 每轮轮询的等待秒数
MATH_VALUE_JS = (
    'var e=document.querySelector(\'input[name="math_answer"]\');'
    'return e ? String(e.value) : "";'
)

def set_math_answer(sb, answer):
    """填数学答案并读回确认。
    update_text 偶尔填不进去，表面看不出——提交后才被服务端判成 wrong_answer。
    读回不一致就 JS 直设 value 兜底；仍不一致则抛真错误，别等到提交才知道。
    """
    want = str(answer)
    sb.update_text('input[name="math_answer"]', want)
    got = sb.execute_script(MATH_VALUE_JS)
    if got != want:
        js = (
            'var e=document.querySelector(\'input[name="math_answer"]\');'
            'e.value="' + want + '";'
            'e.dispatchEvent(new Event("input",{bubbles:true}));'
        )
        sb.execute_script(js)
        time.sleep(1)
        got = sb.execute_script(MATH_VALUE_JS)
    print(f"    答案填入读回: {got}")
    if got != want:
        raise RuntimeError(f"数学答案未填入（期望 {want}，实际 {got!r}）")

def sb_proxy():
    """requests 用的 socks5h:// 换成 Chrome --proxy-server 认识的 socks5://"""
    return PROXY_URL.replace("socks5h://", "socks5://") or None

def solve_math(a, op, b):
    a, b = int(a), int(b)
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op in ("*", "×"):
        return a * b
    if op in ("/", "÷"):
        return a // b   # 题面限制 0..100，整除即可
    raise ValueError(f"未知运算符 {op}")

def read_math_question(html):
    """取出算式原文。<strong> 整段优先，纯文本兜底。"""
    m = VERIFY_STRONG_RE.search(html) or VERIFY_TEXT_RE.search(re.sub(r'<[^>]+>', ' ', html))
    if not m:
        raise RuntimeError("验证页未找到数学题")
    return re.sub(r'\s+', ' ', m.group(1)).strip()

def solve_math_expr(expr):
    """按出现顺序左到右求值：'2 + 2 + 1' → 5。单步运算复用 solve_math。"""
    tokens = MATH_TOKEN_RE.findall(expr)
    if len(tokens) < 3 or not tokens[0].isdigit():
        raise ValueError(f"算式不完整: {expr!r}")
    acc = int(tokens[0])
    i = 1
    while i < len(tokens):
        if tokens[i].isdigit() or i + 1 >= len(tokens) or not tokens[i + 1].isdigit():
            raise ValueError(f"算式不完整: {expr!r}")
        acc = solve_math(acc, tokens[i], int(tokens[i + 1]))
        i += 2
    return acc

def browser_login(callback_url):
    """浏览器打开回调链接、解掉 Quick Verification，返回 (首页HTML, 最终URL)"""
    try:
        from seleniumbase import SB
    except ImportError:
        raise RuntimeError("缺少 seleniumbase：pip install seleniumbase（并需本机装有 Chrome）")
    print(f"[C] 浏览器登录: {mask_url(callback_url)}")

    # raise_test_failure=True：test 模式下 SB 默认吞掉块内异常（只打印 traceback），
    # 真错误会被换成 "cannot unpack non-iterable NoneType object"，TG 报告全是错的。
    kwargs = {"uc": True, "test": True, "locale": "en", "raise_test_failure": True}
    proxy = sb_proxy()
    if proxy:
        print(f"    代理: {proxy}")
        kwargs["proxy"] = proxy

    with SB(**kwargs) as sb:
        sb.uc_open_with_reconnect(callback_url, reconnect_time=5.0)
        time.sleep(3)

        for attempt in range(1, 4):
            if not sb.is_element_present('input[name="math_answer"]'):
                break          # 没有验证页，登录已直接完成
            print(f"[C2] Quick Verification（第 {attempt} 次）...")

            expr = read_math_question(sb.get_page_source())
            answer = solve_math_expr(expr)
            print(f"    题目 {expr} = {answer}")

            set_math_answer(sb, answer)
            sb.execute_script("document.querySelector('.cf-turnstile').scrollIntoView({block:'center'})")
            time.sleep(1)

            # 令牌失败就重试：CF 偶发不给放行（今天两次 schedule 都死在这），
            # 一次拿不到直接 raise 会让整次运行白挂。重试 N 轮，全失败才抛。
            token = ""
            for t in range(1, TURNSTILE_MAX_RETRY + 1):
                sb.uc_gui_click_captcha()
                for _ in range(10):
                    time.sleep(TURNSTILE_POLL_STEP)
                    token = sb.execute_script(TURNSTILE_TOKEN_JS)
                    if len(token) > 50:
                        break
                if len(token) > 50:
                    break
                print(f"    ⚠️ Turnstile 未放行（第 {t}/{TURNSTILE_MAX_RETRY} 次），再点一次...")
            if len(token) <= 50:
                raise RuntimeError("Turnstile 令牌未获取")
            print(f"    Turnstile 令牌长度 {len(token)}，提交中...")
            # 错误页把「答案错」和「令牌被拒」混在同一页面，提交前再读一次答案好区分
            before = sb.execute_script(MATH_VALUE_JS)
            if before != str(answer):
                print(f"    ⚠️ 提交前答案已不是 {answer}: {before!r}")

            sb.click('button[type="submit"]')
            time.sleep(3)

        final_url = sb.get_current_url()
        print(f"    验证后URL: {mask_url(final_url)}")
        if '/error/vpn' in final_url:
            raise RuntimeError("访问被拦截 (VPN error page)")
        if '/error/' in final_url:
            raise RuntimeError(f"登录失败: {final_url}")

        sb.open("https://optiklink.net/")
        sb.wait_for_ready_state_complete()
        time.sleep(2)
        return sb.get_page_source(), sb.get_current_url()

def check_dashboard_html(html, final_url):
    """Dashboard检测 - 判断逻辑保持原始不变"""
    info = {"logged_in": False, "username": "N/A", "expire_date": EXPIRE_DATE_RAW, "running_servers": "N/A"}

    # 原始判断逻辑
    if "DASHBOARD" in html.upper() and "/error/" not in final_url:
        info["logged_in"] = True
        m = re.search(r'Welcome\s+<[^>]+>([^<]+)</[^>]+>\s+to your Dashboard', html, re.I)
        if m:
            info["username"] = m.group(1)
        m2 = re.search(r'(\d+)\s+servers?', html, re.I)
        if m2:
            info["running_servers"] = m2.group(1)
        # 提取到期日期
        m3 = re.search(r'(\d{2}\.\d{2}\.\d{4})', html)
        if m3:
            info["expire_date"] = m3.group(1)
            print(f"    提取到期日期: {info['expire_date']}")
    return info

# ─────────────────────────────────────────────────────────────
# 服务器保活（Pterodactyl）
# ─────────────────────────────────────────────────────────────
def panel_headers():
    return {"Authorization": f"Bearer {PANEL_API_KEY}", "Accept": "application/json"}

def get_server_identifier(session):
    if PANEL_SERVER_ID:
        return PANEL_SERVER_ID
    r = session.get(f"{PANEL_URL}/api/client", headers=panel_headers(), timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"获取服务器列表失败 HTTP {r.status_code}")
    servers = r.json().get("data", [])
    if not servers:
        raise RuntimeError("无服务器")
    return servers[0]["attributes"]["identifier"]

def get_server_status(session, identifier):
    r = session.get(f"{PANEL_URL}/api/client/servers/{identifier}/resources",
                    headers=panel_headers(), timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"状态查询失败 HTTP {r.status_code}")
    return r.json()["attributes"]["current_state"]

def send_power_action(session, identifier, action):
    r = session.post(f"{PANEL_URL}/api/client/servers/{identifier}/power",
                     headers=panel_headers(), json={"signal": action}, timeout=15)
    if r.status_code not in (200,204):
        raise RuntimeError(f"电源指令失败 HTTP {r.status_code}")

def check_and_start_server(session):
    result = {"skipped": True, "server_id": "", "status_before": "unknown", "status_after": "unknown", "action_taken": "none"}
    if not PANEL_API_KEY:
        return result
    result["skipped"] = False
    try:
        identifier = get_server_identifier(session)
        result["server_id"] = identifier
        status = get_server_status(session, identifier)
        result["status_before"] = status
        if status.lower() == "offline":
            send_power_action(session, identifier, "start")
            result["action_taken"] = "start"
            deadline = time.time() + SERVER_START_WAIT
            while time.time() < deadline:
                time.sleep(5)
                new_status = get_server_status(session, identifier)
                if new_status.lower() in ("starting", "running"):
                    result["status_after"] = new_status
                    break
            else:
                result["status_after"] = get_server_status(session, identifier)
        else:
            result["status_after"] = status
    except Exception as e:
        result["error"] = str(e)
    return result

# ─────────────────────────────────────────────────────────────
# 构建简洁报告
# ─────────────────────────────────────────────────────────────
def build_report(info, server_result):
    now = datetime.now(timezone.utc)
    status = "✅ 登录成功" if info["logged_in"] else "❌ 登录失败"
    days_left = "N/A"
    try:
        expire = datetime.strptime(info["expire_date"], "%d.%m.%Y").replace(tzinfo=timezone.utc)
        days_left = str((expire - now).days)
    except:
        pass

    lines = [
        f"## OptikLink 自动登录报告",
        f"**状态**: {status}",
        f"**用户名**: {info['username']}",
        f"**运行服务器**: {info['running_servers']} 个",
        f"**服务到期**: {info['expire_date']}",
        f"**剩余天数**: {days_left} 天",
        f"**执行时间**: {now.strftime('%Y-%m-%d %H:%M:%S')} UTC",
    ]
    if not server_result.get("skipped"):
        if "error" in server_result:
            lines.append(f"**服务器保活**: ❌ {server_result['error'][:100]}")
        else:
            lines.append(f"**服务器ID**: {server_result['server_id']}")
            lines.append(f"**启动前状态**: {server_result['status_before']}")
            lines.append(f"**启动后状态**: {server_result['status_after']}")
            if server_result['action_taken'] == 'start':
                lines.append("**操作**: ▶️ 已自动启动")
    return "\n".join(lines)

# ─────────────────────────────────────────────────────────────
# 主函数（含重试逻辑）
# ─────────────────────────────────────────────────────────────
def main():
    print("="*55)
    print("OptikLink 自动登录 v6.0 (浏览器过 Turnstile 版)")
    print("="*55)

    print("\n========== 开始执行 ==========")
    session = create_session()
    info = {"logged_in": False, "username": "N/A", "expire_date": EXPIRE_DATE_RAW, "running_servers": "N/A"}
    server_result = {"skipped": True}

    try:
        oauth_params = discover_oauth_params(session)
        callback_url = discord_authorize(session, oauth_params)
        html, final_url = browser_login(callback_url)
        print(f"[D] Dashboard... 最终URL: {mask_url(final_url)}")
        info = check_dashboard_html(html, final_url)
        server_result = check_and_start_server(session)

        if not info["logged_in"]:
            raise RuntimeError("Dashboard 未识别为登录状态")
    except Exception as e:
        error_msg = str(e)
        print(f"❌ 执行失败: {error_msg}")
        report = build_report(info, server_result)
        tg_send("❌ OptikLink 签到失败", report)
        print("\n❌ 最终失败，退出。")
        sys.exit(1)

    print("✅ 执行成功！")
    report = build_report(info, server_result)
    tg_send("✅ OptikLink 签到成功", report)

if __name__ == "__main__":
    main()

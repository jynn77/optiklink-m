#!/usr/bin/env python3
"""
OptikLink 自动登录脚本 v4.3-plus（增强超时与错误处理）
- 基于稳定版本 v4.3
- 增加：重定向过程中一旦出现 /error/vpn 立即判定失败
- 增加：cloudscraper 初始化超时保护（30秒）
- 增加：所有网络请求超时控制
- 增加：详细的调试输出和缓冲区刷新
- 支持拦截重试（5分钟间隔，最多3次）
- 推送消息简洁
- 服务器保活（Pterodactyl API）
"""

import os
import re
import sys
import time
import signal
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs, urlencode

# 自定义超时异常
class TimeoutError(Exception):
    pass

def timeout_handler(signum, frame):
    raise TimeoutError("操作超时")

# 强制刷新输出缓冲区的打印函数
def debug_print(msg):
    print(msg)
    sys.stdout.flush()

# 尝试导入 cloudscraper，如果失败则使用 requests
USE_CLOUDSCRAPER = False
try:
    import cloudscraper
    USE_CLOUDSCRAPER = True
    debug_print("[信息] cloudscraper 模块加载成功")
except ImportError:
    import requests
    USE_CLOUDSCRAPER = False
    debug_print("[警告] cloudscraper 未安装，将使用普通 requests")

# 设置全局超时
REQUEST_TIMEOUT = 30

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

# 重试配置
MAX_RETRIES = 3
RETRY_WAIT_SEC = 300

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
    """创建带有代理和浏览器头部的会话（带超时保护）"""
    debug_print("[信息] 开始创建HTTP会话...")
    
    sess = None
    
    if USE_CLOUDSCRAPER:
        debug_print("[信息] 初始化 cloudscraper（设置30秒超时）...")
        
        # 设置超时信号
        signal.signal(signal.SIGALRM, timeout_handler)
        signal.alarm(30)
        
        try:
            sess = cloudscraper.create_scraper(
                delay=15,
                browser={
                    'browser': 'chrome',
                    'platform': 'windows',
                    'mobile': False
                }
            )
            signal.alarm(0)  # 取消超时
            debug_print("[信息] cloudscraper 初始化成功")
        except TimeoutError:
            debug_print("[错误] cloudscraper 初始化超时（30秒），将使用普通 requests")
            signal.alarm(0)
            import requests
            sess = requests.Session()
            USE_CLOUDSCRAPER = False  # 标记为已降级
        except Exception as e:
            debug_print(f"[错误] cloudscraper 初始化失败: {e}，将使用普通 requests")
            signal.alarm(0)
            import requests
            sess = requests.Session()
    else:
        debug_print("[信息] 使用普通 requests session")
        import requests
        sess = requests.Session()
    
    # 配置代理
    if PROXY_URL:
        debug_print(f"[信息] 配置代理: {PROXY_URL}")
        sess.proxies = {"http": PROXY_URL, "https": PROXY_URL}
        
        # 测试代理连接（可选，避免卡死）
        debug_print("[信息] 测试代理连接...")
        try:
            test_response = sess.get("https://1.1.1.1", timeout=10)
            debug_print(f"[信息] 代理测试成功，状态码: {test_response.status_code}")
        except Exception as e:
            debug_print(f"[警告] 代理测试失败: {e}，将忽略代理继续")
            sess.proxies = {}
    else:
        debug_print("[信息] 直连（无代理）")
    
    sess.headers.update(HEADERS_BROWSER)
    debug_print("[信息] HTTP会话创建完成")
    return sess

def tg_send(title: str, content: str):
    """发送 Telegram 消息（Markdown）"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        debug_print("[Telegram] 未配置 BOT_TOKEN 或 CHAT_ID，跳过推送")
        return
    
    import requests as req
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    text = f"*{title}*\n\n{content}"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        resp = req.post(url, json=payload, timeout=15)
        result = resp.json()
        if result.get("ok"):
            debug_print(f"[Telegram] 推送成功")
        else:
            debug_print(f"[Telegram] 推送失败: {result.get('description')}")
    except Exception as e:
        debug_print(f"[Telegram] 请求异常: {e}")

# ─────────────────────────────────────────────────────────────
# 登录核心流程
# ─────────────────────────────────────────────────────────────
def discover_oauth_params(session):
    """探测OAuth参数"""
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify email guilds",
    }
    debug_print("[A] 探测 OAuth 参数...")
    try:
        debug_print(f"    正在请求: https://optiklink.net/auth")
        start_time = time.time()
        r = session.get("https://optiklink.net/auth", timeout=REQUEST_TIMEOUT, headers=HEADERS_BROWSER, allow_redirects=True)
        elapsed = time.time() - start_time
        debug_print(f"    状态码: {r.status_code} 耗时: {elapsed:.2f}秒")
        debug_print(f"    最终URL: {mask_url(r.url)}")
    except Exception as e:
        debug_print(f"    ❌ 请求失败: {type(e).__name__}: {e}")
        raise RuntimeError(f"OAuth探测失败: {e}")
    
    # 从页面或URL中提取OAuth参数
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
        debug_print(f"    client_id 变更: {mask(DISCORD_CLIENT_ID)} → {mask(new_cid)}")
        github_output = os.environ.get("GITHUB_OUTPUT")
        if github_output:
            with open(github_output, "a") as f:
                f.write(f"new_client_id={new_cid}\n")
        tg_send("⚠️ client_id 已变更", f"旧: {mask(DISCORD_CLIENT_ID,6)}\n新: {mask(new_cid,6)}")
    
    return params

def discord_authorize(session, oauth_params):
    """Discord授权"""
    debug_print("[B] Discord 授权...")
    post_params = {k: oauth_params[k] for k in ("client_id", "redirect_uri", "response_type", "scope") if k in oauth_params}
    if "state" in oauth_params:
        post_params["state"] = oauth_params["state"]
    
    headers = {
        "Authorization": DISCORD_TOKEN,
        "Content-Type": "application/json",
        "User-Agent": HEADERS_BROWSER["User-Agent"],
        "Referer": "https://discord.com/oauth2/authorize?" + urlencode(post_params),
        "X-Super-Properties": "eyJvcyI6IldpbmRvd3MiLCJicm93c2VyIjoiQ2hyb21lIn0=",
    }
    
    try:
        debug_print(f"    正在请求 Discord API (超时: {REQUEST_TIMEOUT}秒)")
        start_time = time.time()
        r = session.post("https://discord.com/api/v10/oauth2/authorize",
                         params=post_params,
                         json={"authorize": True, "permissions": "0"},
                         headers=headers, 
                         timeout=REQUEST_TIMEOUT, 
                         allow_redirects=False)
        elapsed = time.time() - start_time
        debug_print(f"    Discord 状态: {r.status_code} 耗时: {elapsed:.2f}秒")
    except Exception as e:
        debug_print(f"    ❌ Discord 请求失败: {type(e).__name__}: {e}")
        raise RuntimeError(f"Discord授权失败: {e}")
    
    if r.status_code == 200 and "location" in r.json():
        return r.json()["location"]
    if r.status_code in (301,302,303,307,308) and "Location" in r.headers:
        return r.headers["Location"]
    raise RuntimeError(f"Discord 授权失败 HTTP {r.status_code}")

def optiklink_callback(session, callback_url):
    """处理回调 - 增加 /error/vpn 检测"""
    debug_print(f"[C] 回调: {mask_url(callback_url)}")
    current_url = callback_url
    
    # 检查初始URL
    if '/error/vpn' in current_url:
        debug_print(f"    ❌ 检测到VPN错误页，登录失败: {current_url}")
        raise RuntimeError("访问被拦截 (VPN error page)")
    
    for i in range(10):
        # 每次请求前检查当前URL
        if '/error/vpn' in current_url:
            debug_print(f"    ❌ 检测到VPN错误页，登录失败: {current_url}")
            raise RuntimeError("访问被拦截 (VPN error page)")
        
        try:
            debug_print(f"    跳转 #{i+1}: 请求中...")
            resp = session.get(current_url, timeout=REQUEST_TIMEOUT, headers=HEADERS_BROWSER, allow_redirects=False)
            debug_print(f"    跳转 #{i+1}: {resp.status_code} → {mask_url(resp.url)}")
        except Exception as e:
            debug_print(f"    ❌ 跳转 #{i+1} 失败: {type(e).__name__}: {e}")
            raise RuntimeError(f"回调请求失败: {e}")
        
        # 检查响应URL
        if '/error/vpn' in resp.url:
            debug_print(f"    ❌ 检测到VPN错误页，登录失败: {resp.url}")
            raise RuntimeError("访问被拦截 (VPN error page)")
        
        if resp.status_code in (301,302,303,307,308):
            location = resp.headers.get("Location")
            if not location:
                raise RuntimeError("无 Location")
            
            # 检查即将跳转的目标URL
            if '/error/vpn' in location:
                debug_print(f"    ❌ 检测到即将重定向到VPN错误页，登录失败: {location}")
                raise RuntimeError("访问被拦截 (VPN error page)")
            
            if location.startswith("/"):
                from urllib.parse import urljoin
                location = urljoin(current_url, location)
            current_url = location
            continue
        if resp.status_code >= 400:
            raise RuntimeError(f"回调失败 HTTP {resp.status_code}")
        return
    raise RuntimeError("重定向过多")

def check_dashboard(session):
    """检查Dashboard登录状态"""
    debug_print("[D] 检查 Dashboard...")
    try:
        r = session.get("https://optiklink.net", timeout=REQUEST_TIMEOUT, headers=HEADERS_BROWSER, allow_redirects=True)
        debug_print(f"    状态码: {r.status_code} 最终URL: {mask_url(r.url)}")
    except Exception as e:
        debug_print(f"    ❌ Dashboard 请求失败: {type(e).__name__}: {e}")
        raise RuntimeError(f"Dashboard检测失败: {e}")
    
    info = {"logged_in": False, "username": "N/A", "expire_date": EXPIRE_DATE_RAW, "running_servers": "N/A"}
    html = r.text
    
    # 安全检查：页面内容包含VPN错误信息
    if "vpn" in html.lower() and "error" in html.lower():
        debug_print(f"    ❌ 页面内容包含VPN错误信息")
        raise RuntimeError("访问被拦截 (VPN error page in content)")
    
    if "DASHBOARD" in html.upper() and "/error/" not in r.url:
        info["logged_in"] = True
        m = re.search(r'Welcome\s+<[^>]+>([^<]+)</[^>]+>\s+to your Dashboard', html, re.I)
        if m:
            info["username"] = m.group(1)
        m2 = re.search(r'(\d+)\s+servers?', html, re.I)
        if m2:
            info["running_servers"] = m2.group(1)
        m3 = re.search(r'(\d{2}\.\d{2}\.\d{4})', html)
        if m3:
            info["expire_date"] = m3.group(1)
    return info

# ─────────────────────────────────────────────────────────────
# 服务器保活（Pterodactyl）- 带错误处理避免卡死
# ─────────────────────────────────────────────────────────────
def panel_headers():
    return {"Authorization": f"Bearer {PANEL_API_KEY}", "Accept": "application/json"}

def get_server_identifier(session):
    if PANEL_SERVER_ID:
        return PANEL_SERVER_ID
    try:
        debug_print(f"    获取服务器列表: {PANEL_URL}/api/client")
        r = session.get(f"{PANEL_URL}/api/client", headers=panel_headers(), timeout=15)
        if r.status_code != 200:
            raise RuntimeError(f"获取服务器列表失败 HTTP {r.status_code}")
        servers = r.json().get("data", [])
        if not servers:
            raise RuntimeError("无服务器")
        return servers[0]["attributes"]["identifier"]
    except Exception as e:
        debug_print(f"    ❌ 获取服务器标识失败: {e}")
        raise

def get_server_status(session, identifier):
    try:
        r = session.get(f"{PANEL_URL}/api/client/servers/{identifier}/resources",
                        headers=panel_headers(), timeout=15)
        if r.status_code != 200:
            raise RuntimeError(f"状态查询失败 HTTP {r.status_code}")
        return r.json()["attributes"]["current_state"]
    except Exception as e:
        debug_print(f"    ❌ 状态查询失败: {e}")
        raise

def send_power_action(session, identifier, action):
    try:
        r = session.post(f"{PANEL_URL}/api/client/servers/{identifier}/power",
                         headers=panel_headers(), json={"signal": action}, timeout=15)
        if r.status_code not in (200,204):
            raise RuntimeError(f"电源指令失败 HTTP {r.status_code}")
    except Exception as e:
        debug_print(f"    ❌ 电源指令失败: {e}")
        raise

def check_and_start_server(session):
    result = {"skipped": True, "server_id": "", "status_before": "unknown", "status_after": "unknown", "action_taken": "none"}
    if not PANEL_API_KEY:
        debug_print("[保活] 未配置 PANEL_API_KEY，跳过服务器保活")
        return result
    
    # 整个保活逻辑用 try-except 包裹，任何错误都不影响登录结果
    try:
        result["skipped"] = False
        debug_print("[保活] 检查服务器状态...")
        
        identifier = get_server_identifier(session)
        result["server_id"] = identifier
        status = get_server_status(session, identifier)
        result["status_before"] = status
        debug_print(f"    当前状态: {status}")
        
        if status.lower() == "offline":
            debug_print(f"    服务器离线，正在启动...")
            send_power_action(session, identifier, "start")
            result["action_taken"] = "start"
            deadline = time.time() + SERVER_START_WAIT
            debug_print(f"    等待服务器启动（最多 {SERVER_START_WAIT} 秒）...")
            while time.time() < deadline:
                time.sleep(5)
                new_status = get_server_status(session, identifier)
                debug_print(f"    当前状态: {new_status}")
                if new_status.lower() in ("starting", "running"):
                    result["status_after"] = new_status
                    break
            else:
                result["status_after"] = get_server_status(session, identifier)
        else:
            result["status_after"] = status
            debug_print(f"    服务器运行正常，无需启动")
    except Exception as e:
        result["error"] = str(e)
        debug_print(f"    ❌ 服务器保活失败: {e}，跳过此步骤")
    
    return result

# ─────────────────────────────────────────────────────────────
# 构建简洁报告
# ─────────────────────────────────────────────────────────────
def build_report(info, server_result, attempt=1, is_intercepted=False):
    now = datetime.now(timezone.utc)
    status = "✅ 登录成功" if info["logged_in"] else "❌ 登录失败"
    days_left = "N/A"
    try:
        expire = datetime.strptime(info["expire_date"], "%d.%m.%Y").replace(tzinfo=timezone.utc)
        days_left = str((expire - now).days)
    except:
        pass

    lines = [
        f"## OptikLink 自动登录报告 (尝试 {attempt})",
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
    if is_intercepted:
        lines.append("\n⚠️ 本次尝试被拦截（VPN error），将自动重试")
    return "\n".join(lines)

# ─────────────────────────────────────────────────────────────
# 主函数（含重试逻辑）
# ─────────────────────────────────────────────────────────────
def main():
    debug_print("="*55)
    debug_print("OptikLink 自动登录 v4.3-plus (增强超时与错误处理)")
    debug_print(f"重试配置: 最多 {MAX_RETRIES} 次, 间隔 {RETRY_WAIT_SEC} 秒")
    debug_print(f"请求超时: {REQUEST_TIMEOUT} 秒")
    debug_print("="*55)

    # 检查必要的环境变量
    if not DISCORD_TOKEN:
        debug_print("❌ 错误: DISCORD_TOKEN 环境变量未设置")
        sys.exit(1)
    
    if EXPIRE_DATE_RAW:
        debug_print(f"[信息] 到期日期: {EXPIRE_DATE_RAW}")
    else:
        debug_print("[警告] EXPIRE_DATE 未设置，将尝试从页面自动获取")

    last_info = None
    last_server_result = None
    final_intercepted = False

    for attempt in range(1, MAX_RETRIES + 1):
        if attempt > 1:
            debug_print(f"\n⏳ 第 {attempt} 次尝试，等待 {RETRY_WAIT_SEC} 秒...")
            time.sleep(RETRY_WAIT_SEC)

        debug_print(f"\n========== 尝试 {attempt}/{MAX_RETRIES} ==========")
        session = create_session()
        intercepted = False
        info = {"logged_in": False, "username": "N/A", "expire_date": EXPIRE_DATE_RAW, "running_servers": "N/A"}
        server_result = {"skipped": True}

        try:
            oauth_params = discover_oauth_params(session)
            callback_url = discord_authorize(session, oauth_params)
            optiklink_callback(session, callback_url)
            info = check_dashboard(session)
            server_result = check_and_start_server(session)

            if not info["logged_in"]:
                raise RuntimeError("Dashboard 未识别为登录状态")
        except Exception as e:
            error_msg = str(e)
            debug_print(f"⚠️ 尝试 {attempt} 失败: {error_msg}")
            last_info = info
            last_server_result = server_result
            
            # 判断是否是VPN拦截错误
            if "VPN error" in error_msg or "vpn" in error_msg.lower():
                intercepted = True
            
            if intercepted and attempt < MAX_RETRIES:
                debug_print(f"检测到拦截，将在 {RETRY_WAIT_SEC} 秒后重试...")
                if attempt == 1:
                    report = build_report(info, server_result, attempt=attempt, is_intercepted=True)
                    tg_send("⚠️ OptikLink 被拦截，将自动重试", report)
                continue
            else:
                final_report = build_report(info, server_result, attempt=attempt, is_intercepted=intercepted)
                tg_send(f"❌ OptikLink 签到失败 (尝试 {attempt})", final_report)
                debug_print(f"\n❌ 最终失败，退出。")
                sys.exit(1)

        # 成功
        debug_print(f"✅ 尝试 {attempt} 成功！")
        report = build_report(info, server_result, attempt=attempt)
        tg_send("✅ OptikLink 签到成功", report)
        return

    # 理论上不会到达这里
    final_report = build_report(last_info or {}, last_server_result or {}, attempt=MAX_RETRIES, is_intercepted=final_intercepted)
    tg_send("❌ OptikLink 签到失败 (重试耗尽)", final_report)
    sys.exit(1)

if __name__ == "__main__":
    main()

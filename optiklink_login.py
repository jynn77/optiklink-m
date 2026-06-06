#!/usr/bin/env python3
"""
OptikLink 自动登录脚本 v4.4（修复Discord 400错误）
- 修复 Discord API 400 错误（改用正确的授权方式）
- 增加 /error/vpn 检测
- 增加超时和错误处理
"""

import os
import re
import sys
import time
import signal
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs, urlencode

# 强制刷新输出缓冲区的打印函数
def debug_print(msg):
    print(msg)
    sys.stdout.flush()

# 尝试导入 cloudscraper
USE_CLOUDSCRAPER = False
try:
    import cloudscraper
    USE_CLOUDSCRAPER = True
    debug_print("[信息] cloudscraper 模块加载成功")
except ImportError:
    import requests
    debug_print("[警告] cloudscraper 未安装，使用普通 requests")

# 设置全局超时
REQUEST_TIMEOUT = 30

# ─────────────────────────────────────────────────────────────
# 配置（环境变量）
# ─────────────────────────────────────────────────────────────
DISCORD_TOKEN       = os.environ.get("DISCORD_TOKEN", "")
TG_BOT_TOKEN        = os.environ.get("BOT_TOKEN", "")
TG_CHAT_ID          = os.environ.get("CHAT_ID", "")
EXPIRE_DATE_RAW     = os.environ.get("EXPIRE_DATE", "")
DISCORD_CLIENT_ID   = os.environ.get("DISCORD_CLIENT_ID", "933437142254887052")
DISCORD_REDIRECT_URI = os.environ.get("DISCORD_REDIRECT_URI", "https://optiklink.com/login")
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

# 自定义超时异常
class TimeoutError(Exception):
    pass

def timeout_handler(signum, frame):
    raise TimeoutError("操作超时")

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
    if not url:
        return "None"
    return re.sub(r'(code|token|access_token|refresh_token)=[^&]+', r'\1=***', url)

def create_session():
    """创建HTTP会话"""
    global USE_CLOUDSCRAPER
    
    debug_print("[信息] 开始创建HTTP会话...")
    
    if USE_CLOUDSCRAPER:
        debug_print("[信息] 初始化 cloudscraper...")
        signal.signal(signal.SIGALRM, timeout_handler)
        signal.alarm(30)
        try:
            sess = cloudscraper.create_scraper(
                delay=15,
                browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
            )
            signal.alarm(0)
            debug_print("[信息] cloudscraper 初始化成功")
        except Exception as e:
            debug_print(f"[错误] cloudscraper 初始化失败: {e}")
            signal.alarm(0)
            import requests
            sess = requests.Session()
            USE_CLOUDSCRAPER = False
    else:
        import requests
        sess = requests.Session()
        debug_print("[信息] 使用普通 requests")
    
    # 配置代理
    if PROXY_URL:
        debug_print(f"[信息] 配置代理: {PROXY_URL}")
        sess.proxies = {"http": PROXY_URL, "https": PROXY_URL}
        try:
            test_response = sess.get("https://1.1.1.1", timeout=10)
            debug_print(f"[信息] 代理测试成功")
        except Exception as e:
            debug_print(f"[警告] 代理测试失败: {e}")
            sess.proxies = {}
    else:
        debug_print("[信息] 直连")
    
    sess.headers.update(HEADERS_BROWSER)
    return sess

def tg_send(title: str, content: str):
    """发送 Telegram 消息"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    import requests as req
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    text = f"*{title}*\n\n{content}"
    try:
        req.post(url, json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=15)
    except:
        pass

# ─────────────────────────────────────────────────────────────
# 登录核心流程
# ─────────────────────────────────────────────────────────────
def discover_oauth_params(session):
    """探测OAuth参数"""
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify",
    }
    debug_print("[A] 探测 OAuth 参数...")
    
    try:
        r = session.get("https://optiklink.net/auth", timeout=REQUEST_TIMEOUT, allow_redirects=True)
        debug_print(f"    状态码: {r.status_code}")
    except Exception as e:
        debug_print(f"    ❌ 请求失败: {e}")
        raise
    
    # 从页面提取参数
    for pat in [r'https?://discord\.com/oauth2/authorize[^\s\'"<>\\]+']:
        m = re.search(pat, r.text)
        if m:
            qs = parse_qs(urlparse(m.group(0)).query)
            for k in ("client_id", "redirect_uri", "scope", "state"):
                if qs.get(k):
                    params[k] = qs[k][0]
            break
    
    debug_print(f"    client_id: {mask(params['client_id'])}")
    debug_print(f"    redirect_uri: {mask_url(params['redirect_uri'])}")
    return params

def discord_authorize(session, oauth_params):
    """Discord授权 - 修复版（解决400错误）"""
    debug_print("[B] Discord 授权...")
    
    # 检查必要的配置
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN 未设置")
    
    # 使用探测到的参数，如果没有则使用环境变量
    client_id = oauth_params.get("client_id") or DISCORD_CLIENT_ID
    redirect_uri = oauth_params.get("redirect_uri") or DISCORD_REDIRECT_URI
    scope = oauth_params.get("scope", "identify")
    
    if not client_id or not redirect_uri:
        raise RuntimeError(f"缺少必要参数: client_id={client_id}, redirect_uri={redirect_uri}")
    
    debug_print(f"    使用 client_id: {mask(client_id)}")
    debug_print(f"    使用 redirect_uri: {mask_url(redirect_uri)}")
    debug_print(f"    使用 scope: {scope}")
    
    # 构造授权URL
    auth_url = f"https://discord.com/api/v10/oauth2/authorize"
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
    }
    if "state" in oauth_params:
        params["state"] = oauth_params["state"]
    
    headers = {
        "Authorization": DISCORD_TOKEN,
        "User-Agent": HEADERS_BROWSER["User-Agent"],
    }
    
    try:
        debug_print(f"    请求 Discord API...")
        start_time = time.time()
        r = session.get(auth_url, params=params, headers=headers, allow_redirects=False, timeout=REQUEST_TIMEOUT)
        elapsed = time.time() - start_time
        debug_print(f"    Discord 状态: {r.status_code} 耗时: {elapsed:.2f}秒")
        
    except Exception as e:
        debug_print(f"    ❌ 请求失败: {e}")
        raise
    
    # 处理重定向（成功）
    if r.status_code == 302 or r.status_code == 301:
        location = r.headers.get("Location")
        if location:
            debug_print(f"    获取到重定向地址")
            return location
        raise RuntimeError("重定向无 Location")
    
    # 处理 200（可能需要额外步骤）
    elif r.status_code == 200:
        try:
            data = r.json()
            debug_print(f"    响应字段: {list(data.keys())}")
            # 检查是否已授权
            if data.get("authorized") == True and "location" in data:
                return data["location"]
            elif "code" in data:
                # 直接返回带code的回调URL
                return f"{redirect_uri}?code={data['code']}"
            else:
                debug_print(f"    响应内容: {str(data)[:200]}")
                raise RuntimeError("无法获取授权码")
        except Exception as e:
            debug_print(f"    ❌ 解析失败: {e}")
            raise RuntimeError(f"Discord 响应解析失败")
    
    # 处理 400
    elif r.status_code == 400:
        debug_print(f"    ❌ 400 错误")
        debug_print(f"    响应: {r.text[:300]}")
        raise RuntimeError("Discord 授权参数错误，请检查 client_id 和 redirect_uri")
    
    # 处理 401
    elif r.status_code == 401:
        raise RuntimeError("Discord Token 无效")
    
    else:
        raise RuntimeError(f"Discord 授权失败 HTTP {r.status_code}")

def optiklink_callback(session, callback_url):
    """处理回调 - 检测 /error/vpn"""
    debug_print(f"[C] 回调处理")
    current_url = callback_url
    
    for i in range(10):
        if '/error/vpn' in current_url:
            raise RuntimeError("访问被拦截 (VPN error page)")
        
        try:
            resp = session.get(current_url, timeout=REQUEST_TIMEOUT, allow_redirects=False)
            debug_print(f"    跳转 #{i+1}: {resp.status_code} -> {mask_url(resp.url)}")
        except Exception as e:
            raise RuntimeError(f"回调请求失败: {e}")
        
        if '/error/vpn' in resp.url:
            raise RuntimeError("访问被拦截 (VPN error page)")
        
        if resp.status_code in (301,302,303,307,308):
            location = resp.headers.get("Location")
            if not location:
                raise RuntimeError("无 Location")
            if '/error/vpn' in location:
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
    """检查Dashboard"""
    debug_print("[D] 检查 Dashboard...")
    try:
        r = session.get("https://optiklink.net", timeout=REQUEST_TIMEOUT, allow_redirects=True)
        debug_print(f"    状态码: {r.status_code} URL: {mask_url(r.url)}")
    except Exception as e:
        raise RuntimeError(f"Dashboard检测失败: {e}")
    
    html = r.text
    info = {"logged_in": False, "username": "N/A", "expire_date": EXPIRE_DATE_RAW, "running_servers": "N/A"}
    
    if "vpn" in html.lower() and "error" in html.lower():
        raise RuntimeError("访问被拦截")
    
    if "DASHBOARD" in html.upper() and "/error/" not in r.url:
        info["logged_in"] = True
        m = re.search(r'Welcome\s+<[^>]+>([^<]+)</[^>]+>', html, re.I)
        if m:
            info["username"] = m.group(1)
        m2 = re.search(r'(\d+)\s+servers?', html, re.I)
        if m2:
            info["running_servers"] = m2.group(1)
    
    return info

# ─────────────────────────────────────────────────────────────
# 主函数
# ─────────────────────────────────────────────────────────────
def main():
    debug_print("="*55)
    debug_print("OptikLink 自动登录 v4.4")
    debug_print("="*55)
    
    # 检查配置
    if not DISCORD_TOKEN:
        debug_print("❌ DISCORD_TOKEN 未设置")
        sys.exit(1)
    
    if not DISCORD_CLIENT_ID:
        debug_print("⚠️ DISCORD_CLIENT_ID 未设置，将从页面自动获取")
    
    for attempt in range(1, MAX_RETRIES + 1):
        if attempt > 1:
            debug_print(f"\n等待 {RETRY_WAIT_SEC} 秒后重试...")
            time.sleep(RETRY_WAIT_SEC)
        
        debug_print(f"\n========== 尝试 {attempt}/{MAX_RETRIES} ==========")
        session = create_session()
        
        try:
            oauth_params = discover_oauth_params(session)
            callback_url = discord_authorize(session, oauth_params)
            optiklink_callback(session, callback_url)
            info = check_dashboard(session)
            
            if not info["logged_in"]:
                raise RuntimeError("未登录")
            
            debug_print(f"✅ 登录成功！用户: {info['username']}")
            report = f"✅ OptikLink 签到成功\n用户: {info['username']}\n服务器: {info['running_servers']}个"
            tg_send("✅ OptikLink 签到成功", report)
            return
            
        except Exception as e:
            debug_print(f"⚠️ 尝试 {attempt} 失败: {e}")
            if attempt == MAX_RETRIES:
                tg_send("❌ OptikLink 签到失败", f"最终失败: {e}")
                sys.exit(1)
            continue

if __name__ == "__main__":
    main()

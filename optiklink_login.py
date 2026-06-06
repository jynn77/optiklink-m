#!/usr/bin/env python3
"""
OptikLink 自动登录脚本 v4.6（最终修复版）
- 使用 POST 请求方式授权 Discord
- 正确处理 authorized: true 响应
- 混合获取授权参数（自动探测 + 硬编码后备）
- 重试等待时间随机（300-500秒）
- 增加 /error/vpn 检测
"""

import os
import re
import sys
import time
import random
import json
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
DISCORD_CLIENT_ID   = os.environ.get("DISCORD_CLIENT_ID", "")
DISCORD_REDIRECT_URI = os.environ.get("DISCORD_REDIRECT_URI", "")
PANEL_URL           = os.environ.get("PANEL_URL", "https://control.optiklink.net")
PANEL_API_KEY       = os.environ.get("PANEL_API_KEY", "")
PANEL_SERVER_ID     = os.environ.get("PANEL_SERVER_ID", "")
SERVER_START_WAIT   = int(os.environ.get("SERVER_START_WAIT", "60"))
PROXY_URL           = os.environ.get("PROXY_URL", "")

# 重试配置 - 随机等待时间
MAX_RETRIES = 3
RETRY_WAIT_MIN = 300  # 最小等待秒数（5分钟）
RETRY_WAIT_MAX = 500  # 最大等待秒数（约8.3分钟）

# 硬编码后备参数（从您成功的登录链接中提取）
FALLBACK_CLIENT_ID = "933437142254887052"
FALLBACK_REDIRECT_URI = "https://optiklink.com/login"
FALLBACK_SCOPE = "guilds guilds.join identify email"

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
        debug_print("[Telegram] 推送成功")
    except Exception as e:
        debug_print(f"[Telegram] 推送失败: {e}")

# ─────────────────────────────────────────────────────────────
# 登录核心流程
# ─────────────────────────────────────────────────────────────
def discover_oauth_params(session):
    """探测OAuth参数 - 从页面提取"""
    params = {
        "client_id": "",
        "redirect_uri": "",
        "response_type": "code",
        "scope": "",
    }
    debug_print("[A] 探测 OAuth 参数...")
    
    try:
        r = session.get("https://optiklink.net/auth", timeout=REQUEST_TIMEOUT, allow_redirects=True)
        debug_print(f"    状态码: {r.status_code}")
    except Exception as e:
        debug_print(f"    ❌ 请求失败: {e}")
        return params
    
    # 从页面提取 Discord 授权链接
    patterns = [
        r'https?://discord\.com/oauth2/authorize[^\s\'"<>\\]+',
        r'https?://discord\.com/api/v[0-9]+/oauth2/authorize[^\s\'"<>\\]+'
    ]
    
    for pat in patterns:
        m = re.search(pat, r.text)
        if m:
            raw_url = m.group(0).replace("&amp;", "&")
            qs = parse_qs(urlparse(raw_url).query)
            if qs.get("client_id"):
                params["client_id"] = qs["client_id"][0]
            if qs.get("redirect_uri"):
                params["redirect_uri"] = qs["redirect_uri"][0]
            if qs.get("scope"):
                params["scope"] = qs["scope"][0]
            if qs.get("state"):
                params["state"] = qs["state"][0]
            debug_print(f"    [探测] client_id: {mask(params['client_id'])}")
            break
    
    if not params["client_id"]:
        debug_print("    [探测] 未找到授权参数，将使用硬编码后备值")
    
    return params

def discord_authorize(session, oauth_params):
    """Discord授权 - POST 请求方式（最终修复版）"""
    debug_print("[B] Discord 授权...")
    
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN 未设置")
    
    # 混合获取参数
    client_id = oauth_params.get("client_id") or FALLBACK_CLIENT_ID
    redirect_uri = oauth_params.get("redirect_uri") or FALLBACK_REDIRECT_URI
    scope = oauth_params.get("scope") or FALLBACK_SCOPE
    
    if not client_id or not redirect_uri:
        raise RuntimeError(f"缺少必要参数")
    
    if oauth_params.get("client_id"):
        debug_print(f"    [来源: 自动探测]")
    else:
        debug_print(f"    [来源: 硬编码后备]")
    
    debug_print(f"    client_id: {mask(client_id)}")
    debug_print(f"    redirect_uri: {redirect_uri}")
    debug_print(f"    scope: {scope}")
    
    # 构造请求参数
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
    }
    
    if "state" in oauth_params and oauth_params["state"]:
        params["state"] = oauth_params["state"]
    
    headers = {
        "Authorization": DISCORD_TOKEN,
        "Content-Type": "application/json",
        "User-Agent": HEADERS_BROWSER["User-Agent"],
        "Referer": "https://discord.com/oauth2/authorize?" + urlencode(params),
    }
    
    # 使用 POST 请求 + JSON body
    try:
        debug_print(f"    请求 Discord API (POST 方式)...")
        start_time = time.time()
        r = session.post(
            "https://discord.com/api/v10/oauth2/authorize",
            params=params,
            json={"authorize": True, "permissions": "0"},
            headers=headers,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=False
        )
        elapsed = time.time() - start_time
        debug_print(f"    Discord 状态: {r.status_code} 耗时: {elapsed:.2f}秒")
        
    except Exception as e:
        debug_print(f"    ❌ Discord 请求失败: {e}")
        raise RuntimeError(f"Discord授权失败: {e}")
    
    # 处理响应
    if r.status_code == 302 or r.status_code == 301:
        location = r.headers.get("Location")
        if location:
            debug_print(f"    重定向到: {mask_url(location)}")
            return location
        raise RuntimeError("重定向无 Location")
    
    elif r.status_code == 200:
        try:
            data = r.json()
            debug_print(f"    响应字段: {list(data.keys())}")
            
            # ========== 关键修复：优先检查 location ==========
            if "location" in data:
                debug_print(f"    获取到 location")
                return data["location"]
            
            # 检查 authorized 字段
            elif data.get("authorized") == True:
                debug_print(f"    账号已授权该应用")
                if "code" in data:
                    callback_url = f"{redirect_uri}?code={data['code']}"
                    debug_print(f"    从 code 构造回调URL")
                    return callback_url
                elif "location" in data:
                    return data["location"]
                else:
                    raise RuntimeError("已授权但未找到重定向地址")
            
            elif data.get("authorized") == False:
                debug_print(f"    ❌ Discord 账号未授权此应用")
                manual_url = f"https://discord.com/oauth2/authorize?client_id={client_id}&redirect_uri={redirect_uri}&response_type=code&scope={scope}"
                debug_print(f"    请手动授权: {manual_url}")
                raise RuntimeError("需要在 Discord 中手动授权应用（至少一次）")
            
            elif "code" in data:
                callback_url = f"{redirect_uri}?code={data['code']}"
                debug_print(f"    从 code 构造回调URL")
                return callback_url
            
            else:
                debug_print(f"    ❌ 未知响应格式")
                debug_print(f"    完整响应: {json.dumps(data, indent=2)[:500]}")
                raise RuntimeError("无法解析 Discord 响应")
                
        except Exception as e:
            debug_print(f"    ❌ 解析失败: {e}")
            raise RuntimeError(f"Discord 响应无效")
    
    elif r.status_code == 400:
        debug_print(f"    ❌ 400 错误")
        debug_print(f"    响应内容: {r.text[:300]}")
        raise RuntimeError("Discord 授权参数错误")
    
    elif r.status_code == 401:
        raise RuntimeError("Discord Token 无效或已过期")
    
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
 # 检查登录状态
    if "DASHBOARD" in html.upper() and "/error/" not in r.url:
        info["logged_in"] = True
        
        # 提取用户名
        m = re.search(r'Welcome\s+<[^>]+>([^<]+)</[^>]+>', html, re.I)
        if m:
            info["username"] = m.group(1)
        
        # 提取服务器数量
        m2 = re.search(r'(\d+)\s+servers?', html, re.I)
        if m2:
            info["running_servers"] = m2.group(1)
        
      # ========== 关键：提取到期日期 ==========
        # 匹配 "Your servers will be deleted on date: 14.06.2026"
        patterns = [
            r'deleted on date:\s*(\d{2}\.\d{2}\.\d{4})',
            r'expire on:\s*(\d{2}\.\d{2}\.\d{4})',
            r'expiry date:\s*(\d{2}\.\d{2}\.\d{4})',
            r'(\d{2}\.\d{2}\.\d{4})',  # 后备：任何 DD.MM.YYYY 格式
        ]
        
        for pattern in date_patterns:
            m3 = re.search(pattern, html, re.I)
            if m3:
                info["expire_date"] = m3.group(1)
                debug_print(f"    提取到期日期: {info['expire_date']}")
                break
        
        if info["expire_date"] == EXPIRE_DATE_RAW:
            debug_print(f"    [警告] 未能从页面提取到期日期，使用环境变量值")
    

    
    return info

def check_and_start_server(session):
    """服务器保活（可选）"""
    result = {"skipped": True}
    if not PANEL_API_KEY:
        debug_print("[保活] 未配置 PANEL_API_KEY，跳过")
        return result
    
    result["skipped"] = False
    debug_print("[保活] 检查服务器状态...")
    # 这里可以添加实际的服务器保活逻辑
    return result

# ─────────────────────────────────────────────────────────────
# 构建报告
# ─────────────────────────────────────────────────────────────
def build_report(info, server_result, attempt=1, is_intercepted=False):
    now = datetime.now(timezone.utc)
    status = "✅ 登录成功" if info.get("logged_in") else "❌ 登录失败"
    
    lines = [
        f"## OptikLink 自动登录报告 (尝试 {attempt})",
        f"**状态**: {status}",
        f"**用户名**: {info.get('username', 'N/A')}",
       
        f"**服务到期**: {info.get('expire_date', EXPIRE_DATE_RAW or '未设置')}",
        f"**执行时间**: {now.strftime('%Y-%m-%d %H:%M:%S')} UTC",
    ]
    # f"**运行服务器**: {info.get('running_servers', 'N/A')} 个",
    
    if is_intercepted:
        lines.append("\n⚠️ 本次尝试被拦截，将自动重试")
    
    return "\n".join(lines)

# ─────────────────────────────────────────────────────────────
# 主函数
# ─────────────────────────────────────────────────────────────
def main():
    debug_print("="*55)
    debug_print("OptikLink 自动登录 v4.6 (最终修复版)")
    debug_print(f"重试配置: 最多 {MAX_RETRIES} 次")
    debug_print(f"重试等待: {RETRY_WAIT_MIN}-{RETRY_WAIT_MAX} 秒随机")
    debug_print("="*55)
    
    # 检查配置
    if not DISCORD_TOKEN:
        debug_print("❌ DISCORD_TOKEN 未设置")
        sys.exit(1)
    
    for attempt in range(1, MAX_RETRIES + 1):
        if attempt > 1:
            wait_time = random.randint(RETRY_WAIT_MIN, RETRY_WAIT_MAX)
            debug_print(f"\n⏳ 等待 {wait_time} 秒后重试...")
            time.sleep(wait_time)
        
        debug_print(f"\n========== 尝试 {attempt}/{MAX_RETRIES} ==========")
        session = create_session()
        
        try:
            # 步骤1：探测OAuth参数
            oauth_params = discover_oauth_params(session)
            
            # 步骤2：Discord授权（POST 方式）
            callback_url = discord_authorize(session, oauth_params)
            debug_print(f"    获得回调URL: {mask_url(callback_url)}")
            
            # 步骤3：处理回调
            optiklink_callback(session, callback_url)
            
            # 步骤4：检查Dashboard
            info = check_dashboard(session)
            
            # 步骤5：服务器保活（可选）
            server_result = check_and_start_server(session)
            
            if not info.get("logged_in"):
                raise RuntimeError("未登录")
            
            debug_print(f"\n✅ 登录成功！用户: {info['username']}")
            report = build_report(info, server_result, attempt=attempt)
            tg_send("✅ OptikLink 签到成功", report)
            return
            
        except Exception as e:
            error_msg = str(e)
            debug_print(f"\n⚠️ 尝试 {attempt} 失败: {error_msg}")
            
            # 判断是否需要重试
            is_retryable = "VPN" in error_msg or "拦截" in error_msg or "未授权" in error_msg
            
            if is_retryable and attempt < MAX_RETRIES:
                debug_print(f"    将进行重试...")
                if attempt == 1:
                    report = build_report({}, {}, attempt=attempt, is_intercepted=True)
                    tg_send("⚠️ OptikLink 被拦截，将自动重试", report)
                continue
            else:
                report = build_report({}, {}, attempt=attempt, is_intercepted=False)
                tg_send(f"❌ OptikLink 签到失败", f"失败: {error_msg}")
                debug_print(f"\n❌ 最终失败，退出。")
                sys.exit(1)

if __name__ == "__main__":
    main()

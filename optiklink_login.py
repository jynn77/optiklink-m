"""
OptikLink 每日自动登录脚本 v3
原理：用 Discord Token 完成 OAuth2 授权，拿到 session 后访问 Dashboard

敏感信息处理规范：
  - Token / UID 等凭证全部从环境变量读取，从不硬编码
  - 日志输出中所有敏感值均经过 mask() 脱敏
"""

import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs, urlencode

# 优先使用 cloudscraper 绕过 Cloudflare 验证
try:
    import cloudscraper
    USE_CLOUDSCRAPER = True
    print("[信息] 使用 cloudscraper 绕过 Cloudflare 人机验证")
except ImportError:
    import requests
    USE_CLOUDSCRAPER = False
    print("[警告] cloudscraper 未安装，将使用普通 requests，可能无法绕过 Cloudflare 验证")

# ─────────────────────────────────────────────────────────────
# 配置区（全部从 GitHub Secrets / 环境变量读取，禁止明文硬编码）
# ─────────────────────────────────────────────────────────────
DISCORD_TOKEN  = os.environ["DISCORD_TOKEN"]    # Discord Token
WXPUSHER_TOKEN = os.environ["WXPUSHER_TOKEN"]   # WxPusher appToken
WXPUSHER_UID   = os.environ["WXPUSHER_UID"]     # WxPusher 接收者 UID

# 服务到期日：优先从环境变量 EXPIRE_DATE 读取（格式 DD.MM.YYYY），
# 否则使用下方兜底值（每次续期后更新此处 OR 在 Secrets 中维护）
EXPIRE_DATE = os.environ.get("EXPIRE_DATE", "22.05.2026")

# ── OptikLink Discord OAuth2 参数 ─────────────────────────────
# 优先从 Secrets 环境变量读取，便于 client_id 变更后无需改代码
# 若下面的值失效：按文末说明重新抓取后更新 GitHub Secrets
DISCORD_CLIENT_ID    = os.environ.get("DISCORD_CLIENT_ID",    "933437142254887052")
DISCORD_REDIRECT_URI = os.environ.get("DISCORD_REDIRECT_URI", "https://optiklink.com/login")

# ─────────────────────────────────────────────────────────────
HEADERS_BROWSER = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


# ─────────────────────────────────────────────────────────────
# 脱敏工具：保留前4位 + *** + 后4位，长度不足时全部遮盖
# ─────────────────────────────────────────────────────────────
def mask(value: str, keep: int = 4) -> str:
    if not value:
        return "***"
    if len(value) <= keep * 2:
        return "***"
    return value[:keep] + "***" + value[-keep:]


# ─────────────────────────────────────────────────────────────
# WxPusher 推送
# ─────────────────────────────────────────────────────────────
def wxpusher_send(title: str, content: str):
    import requests
    resp = requests.post(
        "https://wxpusher.zjiecode.com/api/send/message",
        json={
            "appToken": WXPUSHER_TOKEN,
            "content": content,
            "summary": title,
            "contentType": 3,
            "uids": [WXPUSHER_UID],
        },
        timeout=15,
    )
    result = resp.json()
    print(f"[WxPusher] 推送至 uid={mask(WXPUSHER_UID)} | "
          f"{result.get('msg')} | success={result.get('success')}")


# ─────────────────────────────────────────────────────────────
# Step A: 探测页面，动态发现 OAuth 参数；若发现新 client_id 则预警
# ─────────────────────────────────────────────────────────────
def discover_oauth_params(session) -> dict:
    params = {
        "client_id":     DISCORD_CLIENT_ID,
        "redirect_uri":  DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope":         "identify email guilds",
    }

    print("[A] 访问 /auth 探测页面结构 ...")
    r = session.get("https://optiklink.net/auth", timeout=15,
                    headers=HEADERS_BROWSER, allow_redirects=True)

    print(f"    状态码: {r.status_code}  最终URL: {r.url}")
    print(f"    响应体长度: {len(r.text)} 字节")
    print("─" * 40)

    found_from_page = False

    for pat in [
        r'https?://discord\.com(?:/api)?/oauth2/authorize[^\s\'"<>\\]+',
        r'https?://discord\.com/oauth2/authorize[^\s\'"<>\\]+',
    ]:
        m = re.search(pat, r.text)
        if m:
            raw_url = m.group(0).replace("&amp;", "&").rstrip("\\)\"'")
            print(f"    发现 OAuth URL（已截断）: {raw_url[:60]}...")
            parsed = urlparse(raw_url)
            qs = parse_qs(parsed.query)
            for key in ("client_id", "redirect_uri", "scope", "state"):
                if qs.get(key):
                    params[key] = qs[key][0]
            found_from_page = True
            break

    if "discord.com" in r.url:
        print(f"    页面直接跳转到 Discord: {r.url[:60]}...")
        qs = parse_qs(urlparse(r.url).query)
        for key in ("client_id", "redirect_uri", "scope", "state"):
            if qs.get(key):
                params[key] = qs[key][0]
        found_from_page = True

    if not found_from_page:
        print("    未从页面找到 OAuth URL，使用配置参数（环境变量/默认值）")

    if params.get("client_id") and params["client_id"] != DISCORD_CLIENT_ID:
        new_cid = params["client_id"]
        print(f"    ⚠️  页面 client_id 已变更！配置值={mask(DISCORD_CLIENT_ID, 6)}  "
              f"页面新值={mask(new_cid, 6)}")
        print(f"    ✅  已自动切换为新 client_id，本次直接使用新值继续执行")

        github_output = os.environ.get("GITHUB_OUTPUT", "")
        if github_output:
            with open(github_output, "a") as f:
                f.write(f"new_client_id={new_cid}\n")
            print(f"    📝  已写入 GITHUB_OUTPUT，workflow 将自动更新 Secret")

        try:
            wxpusher_send(
                "⚠️ OptikLink client_id 已变更（已自动处理）",
                f"## client_id 已变更\n\n"
                f"| | 值（已脱敏）|\n|---|---|\n"
                f"| 旧值 | `{mask(DISCORD_CLIENT_ID, 6)}` |\n"
                f"| 新值 | `{mask(new_cid, 6)}` |\n\n"
                f"✅ **本次已自动切换为新值执行，Secret 也将自动更新，无需手动操作。**\n\n"
                f"时间：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC",
            )
        except Exception as pe:
            print(f"    client_id 预警推送失败: {pe}")

    safe_params = {
        k: (mask(v, 6) if k in ("client_id", "redirect_uri") else v)
        for k, v in params.items()
    }
    print(f"    最终 OAuth 参数（已脱敏）: {safe_params}")
    return params


# ─────────────────────────────────────────────────────────────
# Step B: Discord Token 授权
# ─────────────────────────────────────────────────────────────
def discord_authorize(oauth_params: dict) -> str:
    print("[B] 向 Discord 提交 OAuth 授权 ...")
    post_params = {k: oauth_params[k]
                   for k in ("client_id", "redirect_uri", "response_type", "scope")
                   if k in oauth_params}
    if "state" in oauth_params:
        post_params["state"] = oauth_params["state"]

    import requests
    r = requests.post(
        "https://discord.com/api/v10/oauth2/authorize",
        params=post_params,
        json={"authorize": True, "permissions": "0"},
        headers={
            "Authorization": DISCORD_TOKEN,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://discord.com/oauth2/authorize?" + urlencode(post_params),
            "X-Super-Properties": "eyJvcyI6IldpbmRvd3MiLCJicm93c2VyIjoiQ2hyb21lIn0=",
            "X-Discord-Locale": "en-US",
        },
        timeout=15,
        allow_redirects=False,
    )

    print(f"    Discord 状态: {r.status_code}")
    try:
        data = r.json()
    except Exception:
        data = {}

    safe_data = dict(data)
    if "location" in safe_data:
        loc_masked = re.sub(r'(code|token|access_token)=[^&]+', r'\1=***', safe_data["location"])
        safe_data["location"] = loc_masked
    print(f"    Discord body（已脱敏）: {str(safe_data)[:300]}")

    if r.status_code == 200 and "location" in data:
        return data["location"]

    if r.status_code in (301, 302, 303, 307, 308):
        loc = r.headers.get("Location", "")
        if loc:
            loc_log = re.sub(r'(code|token|access_token)=[^&]+', r'\1=***', loc)
            print(f"    重定向 Location（已脱敏）: {loc_log[:100]}")
            return loc

    raise RuntimeError(
        f"Discord 授权失败 (HTTP {r.status_code})\n"
        "可能原因：①Token 失效或格式错误 ②账号被限制 ③client_id/redirect_uri 不匹配"
    )


# ─────────────────────────────────────────────────────────────
# Step C: 回调（使用 cloudscraper 会话自动绕过 Cloudflare）
# ─────────────────────────────────────────────────────────────
def optiklink_callback(session, callback_url: str):
    url_log = re.sub(r'(code|token)=[^&]+', r'\1=***', callback_url)
    print(f"[C] 访问回调 URL（已脱敏）: {url_log[:100]} ...")

    current_url = callback_url
    max_redirects = 10

    for i in range(max_redirects):
        resp = session.get(current_url, timeout=15,
                           headers=HEADERS_BROWSER, allow_redirects=False)
        url_masked = re.sub(r'(code|token|access_token)=[^&\s]+', r'\1=***', resp.url)
        print(f"    跳转 #{i+1}: 状态码 {resp.status_code}, URL={url_masked[:80]}")
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location")
            if not location:
                err_url = re.sub(r'(code|token|access_token)=[^&\s]+', r'\1=***', resp.url)
                raise RuntimeError(f"重定向无 Location 头: {err_url}")
            if location.startswith("/"):
                from urllib.parse import urljoin
                location = urljoin(current_url, location)
            current_url = location
            continue
        final_resp = resp
        break
    else:
        raise RuntimeError("重定向次数超过限制")

    final_url_masked = re.sub(r'(code|token|access_token)=[^&\s]+', r'\1=***', final_resp.url)
    print(f"    最终状态码: {final_resp.status_code}  最终URL: {final_url_masked[:100]}")
    if final_resp.status_code >= 400:
        body_preview = final_resp.text[:200].replace("\n", " ")
        print(f"    响应体预览（前200字符）: {body_preview}")
        raise RuntimeError(f"回调失败，HTTP {final_resp.status_code}")


# ─────────────────────────────────────────────────────────────
# Step D: Dashboard
# ─────────────────────────────────────────────────────────────
def check_dashboard(session) -> dict:
    print("[D] 访问 Dashboard ...")
    r = session.get("https://optiklink.net/home", timeout=15,
                    headers=HEADERS_BROWSER, allow_redirects=True)
    print(f"    状态码: {r.status_code}  最终URL: {r.url}")

    info = {"logged_in": False, "username": "N/A",
            "expire_date": EXPIRE_DATE, "running_servers": "N/A"}
    html = r.text

    if "DASHBOARD" in html.upper():
        info["logged_in"] = True
        for pat in [
            r'Welcome\s+<[^>]+>([^<]+)</[^>]+>\s+to your Dashboard',
            r'"username"\s*:\s*"([^"]+)"',
            r'simeter\w*',
        ]:
            m = re.search(pat, html, re.I)
            if m:
                info["username"] = m.group(1) if m.lastindex else m.group(0)
                break
        m2 = re.search(r'(\d+)\s+servers?', html, re.I)
        if m2:
            info["running_servers"] = m2.group(1)
        m3 = re.search(r'(\d{2}\.\d{2}\.\d{4})', html)
        if m3:
            info["expire_date"] = m3.group(1)

    print(f"    信息: {info}")
    return info


# ─────────────────────────────────────────────────────────────
# 推送消息（含分级到期提醒）
# ─────────────────────────────────────────────────────────────
def build_message(info: dict) -> tuple[str, str]:
    now_utc = datetime.now(timezone.utc)
    if info.get("expire_date"):
        expire_dt = datetime.strptime(info["expire_date"], "%d.%m.%Y").replace(tzinfo=timezone.utc)
        days_left = (expire_dt - now_utc).days
    else:
        expire_dt = None
        days_left = -1
    status = "✅ 登录成功" if info["logged_in"] else "❌ 登录失败"

    if days_left == -1:
        warning = "\n\n> ⚠️ 未能获取到期日期，请手动检查"
        title = f"OptikLink 签到 | {status} | 到期日期未知"
    elif days_left <= 3:
        warning = (
            f"\n\n---\n"
            f"## 🚨🚨🚨 紧急：服务即将到期！\n\n"
            f"> **距到期仅剩 {days_left} 天，请立即续期，否则服务将中断！**"
        )
        title = f"🚨 OptikLink 签到 | 紧急：{days_left}天后到期！"
    elif days_left <= 7:
        warning = (
            f"\n\n---\n"
            f"## ⚠️ 警告：服务即将到期\n\n"
            f"> 距到期还剩 **{days_left}** 天，请尽快安排续期。"
        )
        title = f"⚠️ OptikLink 签到 | 警告：{days_left}天后到期"
    else:
        warning = f"\n\n> 📅 服务到期还剩 **{days_left}** 天" if days_left <= 30 else ""
        title = f"OptikLink 签到 | {status}"

    content = f"""## OptikLink 每日自动登录报告

| 项目 | 内容 |
|------|------|
| 状态 | {status} |
| 用户名 | {info['username']} |
| 运行服务器 | {info['running_servers']} 个 |
| 服务到期 | {info['expire_date']} |
| 剩余天数 | {days_left} 天 |
| 执行时间 | {now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC |
{warning}
"""
    return title, content


# ─────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────
def main():
    print("=" * 55)
    print("  OptikLink 自动登录脚本  v3")
    print("=" * 55)
    print(f"  DISCORD_TOKEN      : ***")
    print(f"  WXPUSHER_TOKEN     : ***")
    print(f"  WXPUSHER_UID       : ***")
    print(f"  DISCORD_CLIENT_ID  : ***")
    # 完全隐藏 redirect_uri，不显示任何真实字符
    print(f"  DISCORD_REDIRECT_URI: ***")
    # 如果 EXPIRE_DATE 为空，显示“未设置”
    print(f"  EXPIRE_DATE        : ***")
    print("=" * 55)

    if USE_CLOUDSCRAPER:
        session = cloudscraper.create_scraper()
    else:
        import requests
        session = requests.Session()
        print("[警告] 使用普通 requests.Session，可能无法绕过 Cloudflare 验证")

    # 如果配置了 V2Ray SOCKS5 代理，让 session 走代理出去
    proxy_url = os.environ.get("PROXY_URL", "")
    if proxy_url:
        session.proxies = {"http": proxy_url, "https": proxy_url}
        print(f"[信息] 已启用代理: {proxy_url}")
    else:
        print("[信息] 未配置代理，直连出口")

    try:
        oauth_params   = discover_oauth_params(session)
        callback_url   = discord_authorize(oauth_params)
        optiklink_callback(session, callback_url)
        info           = check_dashboard(session)
        title, content = build_message(info)
        wxpusher_send(title, content)
        if not info["logged_in"]:
            raise RuntimeError("Dashboard 未出现，登录可能失败，请查看日志")
        print("\n✅ 全部完成！")
    except Exception as e:
        err_msg = str(e)
        print(f"\n❌ 出错: {err_msg}")
        try:
            wxpusher_send(
                "OptikLink 签到 ❌ 失败",
                f"## 执行失败\n\n**错误：**\n```\n{err_msg}\n```\n"
                f"时间：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC",
            )
        except Exception as pe:
            print(f"WxPusher 推送失败: {pe}")
        sys.exit(1)


if __name__ == "__main__":
    main()

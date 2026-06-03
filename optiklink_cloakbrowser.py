"""
OptikLink 每日自动登录脚本 v4.5 (CloakBrowser版)
原理：用 CloakBrowser 打开页面，注入 Discord Token 完成 OAuth2 授权

修复记录 v4.5:
  - 新增录屏功能：环境变量 ENABLE_SCREENRECORD=true 开启，默认 false
  - 录屏文件保存至 recordings/ 目录，格式为 webm
  - 录屏在 browser.new_page() 后立即开始，在 finally 块中停止并保存
  - 与截图功能互相独立，可单独或同时开启

修复记录 v4.4:
  - Discord 按钮实际为 <a href="login" class="hyperlink_abs w-inline-block">（无文字，图标为图片）
  - 将 a[href="login"].hyperlink_abs / a[href="login"] 加入选择器列表并置于首位

修复记录 v4.3:
  - 在点击 Discord 按钮前，自动关闭 Cookie/GDPR 同意弹窗（fc- 前缀）

修复记录 v4.2:
  - 修复 Discord 登录按钮选择器顺序（button 优先于 a 标签）
  - 增加按钮等待超时时间（3000ms）
  - 页面加载后增加 2s 等待让 JS 渲染完成
  - 找不到按钮时自动打印页面所有可交互元素（调试用）
  - 新增 button[class*="discord"] / [class*="discord"] 选择器兜底
"""

import os
import re
import sys
import json
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# 配置（全部从 GitHub Secrets / 环境变量读取）
# ─────────────────────────────────────────────────────────────
DISCORD_TOKEN        = os.environ["DISCORD_TOKEN"]
WXPUSHER_TOKEN       = os.environ["WXPUSHER_TOKEN"]
WXPUSHER_UID         = os.environ["WXPUSHER_UID"]
EXPIRE_DATE          = os.environ.get("EXPIRE_DATE", "")
PROXY_URL            = os.environ.get("PROXY_URL", "socks5://127.0.0.1:10808")
ENABLE_SCREENSHOT    = os.environ.get("ENABLE_SCREENSHOT",    "false").lower() == "true"
ENABLE_SCREENRECORD  = os.environ.get("ENABLE_SCREENRECORD",  "false").lower() == "true"

BASE_URL      = "https://optiklink.net"
AUTH_URL      = f"{BASE_URL}/auth"
DASHBOARD_URL = BASE_URL

VIEWPORT_W = 1280
VIEWPORT_H = 753

# ─────────────────────────────────────────────────────────────
# 截图
# ─────────────────────────────────────────────────────────────
SCREENSHOT_DIR = Path("./screenshots")
SCREENSHOT_DIR.mkdir(exist_ok=True)

def take_screenshot(page, name: str):
    if not ENABLE_SCREENSHOT:
        return
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = str(SCREENSHOT_DIR / f"{ts}_{name}.png")
        page.screenshot(path=path, full_page=False)
        log.info(f"📸 截图已保存: {path}")
    except Exception as e:
        log.warning(f"截图失败: {e}")

# ─────────────────────────────────────────────────────────────
# 录屏
# ─────────────────────────────────────────────────────────────
RECORDING_DIR = Path("./recordings")
RECORDING_DIR.mkdir(exist_ok=True)

def start_recording(context):
    """
    开始录屏。需要传入 browser context（不是 page）。
    Playwright 的 record_video 必须在 new_context() 时指定，
    但 CloakBrowser 通常直接暴露 browser.new_page()，
    所以这里改用 context.tracing 或手动逐帧截图方案作为兼容兜底。
    返回一个 recorder 对象（dict），供 stop_recording 使用。
    """
    if not ENABLE_SCREENRECORD:
        return None
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    rec = {"ts": ts, "running": True, "frames": [], "thread": None}
    return rec

def start_page_recording(page):
    """
    基于定时截图实现录屏：每 500ms 截一帧，最终合成为 GIF / 视频。
    在独立线程中运行，不阻塞主流程。
    返回 recorder dict，供 stop_page_recording 使用。
    """
    if not ENABLE_SCREENRECORD:
        return None

    import threading

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    frame_dir = RECORDING_DIR / f"frames_{ts}"
    frame_dir.mkdir(exist_ok=True)

    rec = {
        "ts":        ts,
        "frame_dir": frame_dir,
        "running":   True,
        "count":     0,
        "thread":    None,
    }

    def _capture():
        while rec["running"]:
            try:
                idx = rec["count"]
                path = str(frame_dir / f"frame_{idx:05d}.png")
                page.screenshot(path=path, full_page=False)
                rec["count"] += 1
            except Exception:
                pass
            time.sleep(0.5)

    t = threading.Thread(target=_capture, daemon=True)
    t.start()
    rec["thread"] = t
    log.info(f"🎬 录屏已开始，帧目录: {frame_dir}")
    return rec

def stop_page_recording(rec):
    """
    停止录屏，将帧合成为 GIF（无需额外依赖）或 MP4（需要 imageio / ffmpeg）。
    优先尝试 Pillow 合成 GIF，失败则保留帧目录供手动查看。
    """
    if rec is None:
        return

    rec["running"] = False
    if rec.get("thread"):
        rec["thread"].join(timeout=2)

    frame_dir = rec["frame_dir"]
    ts        = rec["ts"]
    count     = rec["count"]
    log.info(f"🎬 录屏停止，共采集 {count} 帧")

    if count == 0:
        log.warning("录屏无帧，跳过合成")
        return

    # ── 方案 A：用 Pillow 合成 GIF ──────────────────────────
    try:
        from PIL import Image

        frames_paths = sorted(frame_dir.glob("frame_*.png"))
        images = [Image.open(str(p)).convert("P", dither=Image.FLOYDSTEINBERG) for p in frames_paths]
        out_path = str(RECORDING_DIR / f"{ts}_recording.gif")
        images[0].save(
            out_path,
            save_all=True,
            append_images=images[1:],
            duration=500,      # 每帧 500ms，与采集间隔一致
            loop=0,
            optimize=True,
        )
        log.info(f"🎬 GIF 已保存: {out_path}")

        # 合成成功后清理帧目录节省空间
        for p in frames_paths:
            p.unlink()
        frame_dir.rmdir()
        return
    except ImportError:
        log.warning("Pillow 未安装，尝试 ffmpeg 合成 MP4...")
    except Exception as e:
        log.warning(f"GIF 合成失败: {e}，尝试 ffmpeg...")

    # ── 方案 B：用 ffmpeg 合成 MP4 ──────────────────────────
    try:
        import subprocess
        out_path = str(RECORDING_DIR / f"{ts}_recording.mp4")
        cmd = [
            "ffmpeg", "-y",
            "-framerate", "2",                          # 2fps（每帧 500ms）
            "-i", str(frame_dir / "frame_%05d.png"),
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",  # 保证偶数尺寸
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            out_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            log.info(f"🎬 MP4 已保存: {out_path}")
            for p in sorted(frame_dir.glob("frame_*.png")):
                p.unlink()
            frame_dir.rmdir()
        else:
            log.warning(f"ffmpeg 失败: {result.stderr[:200]}")
            log.info(f"🎬 帧目录保留供手动查看: {frame_dir}（共 {count} 帧）")
    except FileNotFoundError:
        log.warning("ffmpeg 未找到，帧目录保留供手动查看")
        log.info(f"🎬 帧目录: {frame_dir}（共 {count} 帧）")
    except Exception as e:
        log.warning(f"MP4 合成异常: {e}")
        log.info(f"🎬 帧目录保留供手动查看: {frame_dir}")

# ─────────────────────────────────────────────────────────────
# WxPusher 推送
# ─────────────────────────────────────────────────────────────
def wxpush(title: str, content: str):
    import urllib.request
    payload = json.dumps({
        "appToken":    WXPUSHER_TOKEN,
        "content":     content,
        "summary":     title,
        "contentType": 3,
        "uids":        [WXPUSHER_UID],
    }).encode()
    try:
        req = urllib.request.Request(
            "https://wxpusher.zjiecode.com/api/send/message",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            if result.get("success"):
                log.info("📨 WxPusher 推送成功")
            else:
                log.warning(f"📨 WxPusher 推送失败: {result}")
    except Exception as e:
        log.warning(f"📨 WxPusher 推送异常: {e}")

# ─────────────────────────────────────────────────────────────
# Discord Token 注入工具
# ─────────────────────────────────────────────────────────────
def inject_discord_token(page, token: str):
    """向 Discord 页面注入 Token（localStorage），然后刷新"""
    page.evaluate("""(token) => {
        const f = document.createElement('iframe');
        f.style.display = 'none';
        document.body.appendChild(f);
        f.contentWindow.localStorage.setItem('token', '"' + token + '"');
        try { localStorage.setItem('token', '"' + token + '"'); } catch(e) {}
        document.body.removeChild(f);
    }""", token)
    log.info("Token 已注入 localStorage")

# ─────────────────────────────────────────────────────────────
# Discord OAuth 授权页处理
# ─────────────────────────────────────────────────────────────
def handle_oauth_page(page):
    log.info("处理 Discord OAuth 授权页...")
    page.wait_for_timeout(2000)

    for _ in range(30):
        if "discord.com" not in page.url:
            log.info("已离开 Discord，OAuth 完成")
            return

        btn_text = ""
        try:
            for sel in ['button[type="submit"]', 'div[class*="footer"] button', 'button[class*="primary"]']:
                btn = page.locator(sel).last
                if btn.is_visible():
                    btn_text = btn.inner_text().strip().lower()
                    break
        except Exception:
            pass

        if "authorize" in btn_text or "授权" in btn_text:
            break

        page.evaluate("""() => {
            const sels = ['[class*="scroller"]','[class*="oauth2"]','[class*="permissionList"]',
                '[class*="content"] [class*="scroll"]','[class*="listScroller"]',
                'div[class*="modal"] div[style*="overflow"]','div[class*="root"] div[style*="overflow"]'];
            let scrolled = false;
            for (const sel of sels) {
                for (const el of document.querySelectorAll(sel)) {
                    const s = getComputedStyle(el);
                    if (el.scrollHeight > el.clientHeight &&
                        ['auto','scroll'].some(v => s.overflowY === v || s.overflow === v))
                        { el.scrollTop = el.scrollHeight; scrolled = true; }
                }
            }
            if (!scrolled) document.querySelectorAll('div').forEach(el => {
                if (el.scrollHeight > el.clientHeight + 10) {
                    const s = getComputedStyle(el);
                    if (['auto','scroll','hidden'].includes(s.overflowY)) el.scrollTop = el.scrollHeight;
                }
            });
            scrollTo(0, document.body.scrollHeight);
        }""")
        page.wait_for_timeout(800)

    for _ in range(10):
        if "discord.com" not in page.url:
            return
        for sel in [
            'button:has-text("Authorize")',
            'button:has-text("授权")',
            'button[type="submit"]',
            'div[class*="footer"] button',
            'button[class*="primary"]',
        ]:
            try:
                btn = page.locator(sel).last
                if not btn.is_visible():
                    continue
                text = btn.inner_text().strip()
                if any(k in text.lower() for k in ("取消", "cancel", "deny")):
                    continue
                if "scroll" in text.lower():
                    page.evaluate("""() => {
                        document.querySelectorAll('div').forEach(el => {
                            if (el.scrollHeight > el.clientHeight + 5) el.scrollTop = el.scrollHeight;
                        }); scrollTo(0, document.body.scrollHeight);
                    }""")
                    page.wait_for_timeout(1000)
                    break
                if btn.is_disabled():
                    page.wait_for_timeout(1000)
                    break
                log.info(f"点击授权按钮: {text}")
                btn.click()
                page.wait_for_timeout(2000)
                if "discord.com" not in page.url:
                    return
                break
            except Exception:
                continue
        page.wait_for_timeout(1500)

# ─────────────────────────────────────────────────────────────
# 主登录流程
# ─────────────────────────────────────────────────────────────
def do_login(page) -> bool:
    log.info(f"[A] 打开登录页: {AUTH_URL}")
    try:
        page.goto(AUTH_URL, timeout=30000, wait_until="domcontentloaded")
    except Exception as e:
        log.warning(f"goto 超时/异常: {e}")
    take_screenshot(page, "01_auth_page")

    page.wait_for_timeout(2000)

    # 服务条款确认按钮
    try:
        confirm_btn = page.locator("button#confirm-login, button:has-text('同意'), button:has-text('Agree'), button:has-text('Accept')")
        if confirm_btn.first.is_visible(timeout=3000):
            confirm_btn.first.click()
            log.info("已点击服务条款确认按钮")
            page.wait_for_timeout(1500)
    except Exception:
        pass

    # FIX v4.3: 关闭 Cookie/GDPR 同意弹窗（fc- 前缀）
    for consent_sel in [
        'button.fc-cta-consent',
        'button.fc-button.fc-cta-consent',
        'button.fc-vendor-preferences-accept-all',
        'button.fc-data-preferences-accept-all',
        'button:has-text("Consent")',
        'button:has-text("Accept all")',
        'button:has-text("同意")',
    ]:
        try:
            btn = page.locator(consent_sel).first
            if btn.is_visible(timeout=1500):
                btn.click()
                log.info(f"已关闭 Cookie 弹窗: {consent_sel}")
                page.wait_for_timeout(1000)
                break
        except Exception:
            continue

    # FIX v4.4: Discord 按钮实际是 <a href="login"> 无文字图标链接，置于首位
    log.info("[B] 点击 Discord 登录按钮...")
    clicked = False
    for sel in [
        'a[href="login"].hyperlink_abs',
        'a[href="login"]',
        'div.nav_login_block_extra a[href="login"]',
        'button:has-text("DISCORD")',
        'button:has-text("Discord")',
        'button:has-text("discord")',
        'a:has-text("DISCORD")',
        'a:has-text("Discord")',
        'button[class*="discord"]',
        '[class*="discord-btn"]',
        '[class*="discordBtn"]',
        'a[href*="discord.com/oauth2"]',
        'a[href*="oauth2/authorize"]',
        'a:has-text("Sign in with Discord")',
        'a:has-text("Login with Discord")',
        '.discord-btn',
    ]:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=3000):
                btn.click()
                log.info(f"已点击登录按钮: {sel}")
                clicked = True
                break
        except Exception:
            continue

    if not clicked:
        log.error("未找到 Discord 登录按钮，开始打印页面元素调试信息...")
        try:
            elements = page.locator("button, a").all()
            for el in elements:
                try:
                    tag  = el.evaluate("el => el.tagName")
                    text = el.inner_text(timeout=500).strip()[:80]
                    cls  = el.get_attribute("class") or ""
                    href = el.get_attribute("href") or ""
                    log.info(f"  [{tag}] text='{text}' class='{cls[:60]}' href='{href[:60]}'")
                except Exception:
                    pass
        except Exception as e:
            log.warning(f"调试元素打印失败: {e}")
        take_screenshot(page, "01b_click_fail")
        return False

    # 等待跳转到 discord.com
    log.info("[C] 等待跳转到 Discord...")
    try:
        page.wait_for_url(re.compile(r"discord\.com"), timeout=15000)
        log.info(f"已到达 Discord: {page.url}")
    except Exception as e:
        log.warning(f"等待 Discord 超时: {e}，当前URL: {page.url}")
        take_screenshot(page, "02_discord_timeout")
        return False

    take_screenshot(page, "02_discord_page")

    # 注入 Token
    log.info("[D] 注入 Discord Token...")
    inject_discord_token(page, DISCORD_TOKEN)
    page.reload(wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(3000)

    if re.search(r"discord\.com/login", page.url):
        log.error("Token 注入失败，仍在登录页")
        take_screenshot(page, "03_token_failed")
        return False
    log.info("Token 注入成功")
    take_screenshot(page, "03_token_injected")

    # 处理 OAuth 授权页
    try:
        page.wait_for_url(re.compile(r"discord\.com/oauth2/authorize"), timeout=6000)
        page.wait_for_timeout(2000)
        if "discord.com" in page.url:
            handle_oauth_page(page)
            if "discord.com" in page.url:
                try:
                    page.wait_for_url(re.compile(r"optiklink\.net"), timeout=20000)
                except Exception:
                    pass
    except Exception:
        if "discord.com" in page.url:
            handle_oauth_page(page)

    take_screenshot(page, "04_after_oauth")

    # 等待跳回 optiklink.net
    log.info("[E] 等待跳回 OptikLink...")
    try:
        page.wait_for_url(re.compile(r"optiklink\.net"), timeout=20000)
        log.info(f"已跳回: {page.url}")
    except Exception as e:
        log.warning(f"等待跳回超时: {e}，当前URL: {page.url}")
        if "optiklink.net" not in page.url:
            take_screenshot(page, "05_redirect_timeout")
            return False

    current = page.url
    if "optiklink.net" in current and "/auth" not in current:
        log.info(f"已在 OptikLink: {current}")
    else:
        log.info("手动导航到首页...")
        try:
            page.goto(DASHBOARD_URL, timeout=20000, wait_until="domcontentloaded")
        except Exception as e:
            log.warning(f"goto 首页超时: {e}")

    take_screenshot(page, "05_home_page")
    return True

# ─────────────────────────────────────────────────────────────
# 读取 Dashboard 信息
# ─────────────────────────────────────────────────────────────
def read_dashboard(page) -> dict:
    log.info("[F] 读取 Dashboard 信息...")
    info = {
        "logged_in":       False,
        "username":        "N/A",
        "expire_date":     EXPIRE_DATE,
        "running_servers": "N/A",
    }

    try:
        page.wait_for_timeout(3000)
        html = page.content()
        text = page.inner_text("body")
    except Exception as e:
        log.warning(f"读取页面失败: {e}")
        return info

    current_url = page.url.lower()

    is_logged_in = (
        "/auth" not in current_url
        and "optiklink.net" in current_url
        and any(kw in html.upper() for kw in ("DASHBOARD", "MY PLAN", "SERVER", "LOGOUT", "SIGN OUT"))
    )

    if is_logged_in:
        info["logged_in"] = True
        log.info(f"✅ 确认已登录，URL: {page.url}")
    else:
        log.warning(f"当前URL: {page.url}，未检测到登录态关键字")
        log.warning(f"页面片段: {text[:200]}")
        return info

    for pat in [
        r'Welcome\s+(?:<[^>]+>)?(\w+)(?:<[^>]+>)?\s+to',
        r'"username"\s*:\s*"([^"]+)"',
        r'Hello,?\s+(\w+)',
    ]:
        m = re.search(pat, html, re.I)
        if m:
            info["username"] = m.group(1) if m.lastindex else m.group(0)
            break

    for pat in [
        r'(\d{2}\.\d{2}\.\d{4})',
        r'date:\s*(\d{2}\.\d{2}\.\d{4})',
        r'expire[^:]*:\s*(\d{2}\.\d{2}\.\d{4})',
    ]:
        m = re.search(pat, text, re.I)
        if m:
            info["expire_date"] = m.group(1)
            break

    m2 = re.search(r'(\d+)\s*(?:running\s*)?servers?', text, re.I)
    if m2:
        info["running_servers"] = m2.group(1)

    log.info(f"Dashboard 信息: {info}")
    return info

# ─────────────────────────────────────────────────────────────
# 构建推送消息
# ─────────────────────────────────────────────────────────────
def build_message(info: dict) -> tuple[str, str]:
    now_utc = datetime.now(timezone.utc)
    status = "✅ 登录成功" if info["logged_in"] else "❌ 登录失败"

    days_left = -1
    if info.get("expire_date"):
        try:
            expire_dt = datetime.strptime(info["expire_date"], "%d.%m.%Y").replace(tzinfo=timezone.utc)
            days_left = (expire_dt - now_utc).days
        except Exception:
            pass

    if days_left == -1:
        warning = "\n\n> ⚠️ 未能获取到期日期，请手动检查"
        title = f"OptikLink 签到 | {status} | 到期日期未知"
    elif days_left <= 3:
        warning = f"\n\n---\n## 🚨 紧急：服务即将到期！\n\n> **距到期仅剩 {days_left} 天，请立即续期！**"
        title = f"🚨 OptikLink 签到 | 紧急：{days_left}天后到期！"
    elif days_left <= 7:
        warning = f"\n\n---\n## ⚠️ 服务即将到期\n\n> 距到期还剩 **{days_left}** 天，请尽快续期。"
        title = f"⚠️ OptikLink 签到 | 警告：{days_left}天后到期"
    else:
        warning = f"\n\n> 📅 服务到期还剩 **{days_left}** 天" if 0 < days_left <= 30 else ""
        title = f"OptikLink 签到 | {status}"

    content = f"""## OptikLink 每日自动登录报告

| 项目 | 内容 |
|------|------|
| 状态 | {status} |
| 用户名 | {info['username']} |
| 运行服务器 | {info['running_servers']} 个 |
| 服务到期 | {info['expire_date'] or '未知'} |
| 剩余天数 | {days_left if days_left >= 0 else '未知'} 天 |
| 执行时间 | {now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC |
{warning}
"""
    return title, content

# ─────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────
def main():
    log.info("=" * 55)
    log.info("  OptikLink 自动登录脚本 v4.5 (CloakBrowser)")
    log.info("=" * 55)
    log.info(f"  截图: {'开启' if ENABLE_SCREENSHOT else '关闭'}  |  录屏: {'开启' if ENABLE_SCREENRECORD else '关闭'}")

    from cloakbrowser import launch, ensure_binary
    ensure_binary()

    log.info("启动 CloakBrowser...")
    browser = launch(
        headless=True,
        humanize=True,
        proxy=PROXY_URL,
        geoip=True,
    )
    page = browser.new_page()
    try:
        page.set_viewport_size({"width": VIEWPORT_W, "height": VIEWPORT_H})
    except Exception:
        pass

    # 录屏：在页面创建后立即开始
    recorder = start_page_recording(page)

    try:
        success = do_login(page)

        if not success:
            wxpush(
                "OptikLink 签到 ❌ 失败",
                f"## 执行失败\n\n**错误：** 登录流程未完成，请查看日志\n\n"
                f"时间：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC",
            )
            sys.exit(1)

        info = read_dashboard(page)
        title, content = build_message(info)
        wxpush(title, content)

        if not info["logged_in"]:
            log.error("Dashboard 未出现登录状态，脚本标记为失败")
            sys.exit(1)

        log.info("✅ 全部完成！")

    except Exception as e:
        import traceback
        log.error(f"未预期异常: {e}")
        traceback.print_exc()
        take_screenshot(page, "99_error")
        wxpush(
            "OptikLink 签到 ❌ 异常",
            f"## 执行异常\n\n```\n{e}\n```\n\n"
            f"时间：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC",
        )
        sys.exit(1)
    finally:
        # 录屏：无论成功失败都停止并保存
        stop_page_recording(recorder)
        time.sleep(3)
        browser.close()
        log.info("浏览器已关闭")


if __name__ == "__main__":
    main()

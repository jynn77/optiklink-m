# OptikLink-M

> OptikLink 面板的自动签到 + 服务器保活，跑在 GitHub Actions 上，躺平不用管。

## 它做什么

每天早上（UTC 1:00，每 3 天一次），这个 workflow 自动：

1. **通过 Discord OAuth 登录 OptikLink 面板** — 用你的 Discord 令牌完成授权流程，绕过 Cloudflare 验证（`cloudscraper`）
2. **签到确认** — 检查 Dashboard 页面，提取登录状态、用户名、到期时间
3. **服务器保活** — 如果面板服务器（Pterodactyl）处于离线状态，自动发送启动指令
4. **推送报告** — 登录结果通过 Telegram Bot 推送给你
5. **自动更新 Client ID** — 如果 OptikLink 更换了 Discord OAuth 的 `client_id`，脚本会自动探测并更新 GitHub Secret（省得你手动改）

## 文件结构

| 文件 | 用途 |
|---|---|
| `optiklink_login.py` | 主脚本：登录、签到、服务器保活、TG 推送 |
| `generate_xray_config.py` | 从 VLESS 链接生成 Xray 客户端配置（用于代理出口） |
| `test_discord.py` | 调试工具：测试 Discord API 授权参数是否有效 |
| `time.txt` | 每次运行自动更新时间戳，保持仓库活跃 |
| `.github/workflows/optiklink.yml` | GitHub Actions 工作流定义 |

## 部署

### 1. Fork 这个仓库

点右上角 Fork。

### 2. 配置 Secrets

去 Settings → Secrets and variables → Actions → New repository secret，添加以下内容：

| Secret | 说明 |
|---|---|
| `DISCORD_TOKEN` | 你的 Discord 用户令牌（`Authorization` header 值，`mfa.xxx` 格式） |
| `BOT_TOKEN` | Telegram Bot Token（从 [@BotFather](https://t.me/BotFather) 获取） |
| `CHAT_ID` | 接收推送的 Telegram 用户/群组 ID |
| `VLESS_NODE` | VLESS 链接，用于 Xray 代理出口（绕过 OptikLink 的 IP/地区限制） |
| `PANEL_API_KEY` | （可选）Pterodactyl 面板 API Key，用于服务器保活 |
| `PANEL_SERVER_ID` | （可选）指定服务器 ID，不填则自动取第一个 |
| `EXPIRE_DATE` | （可选）手动指定到期日期作为后备 |
| `DISCORD_CLIENT_ID` | （可选）Discord OAuth client_id，脚本会尝试自动探测 |
| `DISCORD_REDIRECT_URI` | （可选）Discord OAuth 回调地址 |

### 3. 启动 Workflow

- 默认每 3 天运行一次（`cron: '0 1 */3 * *'`）
- 也可以手动触发：Actions → OptikLink 每日自动登录+服务器保活 → Run workflow

## 依赖

- Python 3.12+
- `requests`, `cloudscraper`（自动安装在 CI 中）

## 免责声明

这个项目仅供学习和个人自动化使用。使用 Discord 用户令牌进行 OAuth 授权可能违反 Discord 服务条款，请自行评估风险。

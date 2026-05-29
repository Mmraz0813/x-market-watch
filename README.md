# X-market-watch

X-market-watch 是一个 Python 小服务：定时读取你的 X List 推文，把新内容交给大模型筛选、总结或翻译成中文，然后通过 Telegram Bot 推送给你。

## 功能

- 使用 X 官方 API v2 的 List Posts endpoint 拉取 List timeline。
- 支持读取私密列表：`.env` 中的 `X_BEARER_TOKEN` 需要具备读取该私密列表的权限。
- 使用 OpenAI-compatible Chat Completions 接口做中文市场情报筛选和摘要。
- Telegram Bot 推送，默认关闭网页预览。
- 内置一个轻量网页控制台，可以查看配置状态、运行日志，并手动触发 dry-run/正式运行。
- 本地 `data/state.json` 记录 `last_seen_id`，避免重复推送。

## 准备

1. 在 X Developer Console 创建 Project/App，并生成 token。
2. 找到 List ID，例如 `https://x.com/i/lists/84839422` 中的 `84839422`。
3. 在 Telegram 中创建 bot，拿到 `TELEGRAM_BOT_TOKEN`。
4. 获取 `TELEGRAM_CHAT_ID`：

```bash
curl "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getUpdates"
```

先给你的 bot 发一条消息，再从返回结果里找 `chat.id`。

> 注意：如果你的 List 是私密列表，普通 app-only Bearer Token 可能无法读取。你可以使用用户上下文 OAuth2 access token 填到 `X_BEARER_TOKEN`，也可以使用 X 后台更容易生成的 OAuth 1.0a `Access Token + Access Token Secret`。

## 安装

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install ".[dev]"
```

启动网页控制台后，可以直接在“设置”页面填写密钥和运行参数。你也可以手动创建 `.env`：

```bash
cp .env.example .env
```

`.env` 示例：

```bash
X_AUTH_MODE=bearer
X_BEARER_TOKEN=...
X_LIST_ID=...
X_MAX_PAGES=1

LLM_API_KEY=...
LLM_API_BASE=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
LLM_MIN_IMPORTANCE=3

TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
POLL_INTERVAL_SECONDS=300
WEB_HOST=127.0.0.1
WEB_PORT=8787
```

如果你在 X App 里拿不到 OAuth2 access token，可以改用 OAuth 1.0a。进入 X Developer Portal 的 App：

```text
Keys and Tokens
→ Access Token and Secret
→ Generate
```

然后这样填 `.env`：

```bash
X_AUTH_MODE=oauth1
X_BEARER_TOKEN=unused
X_API_KEY=你的_API_Key
X_API_KEY_SECRET=你的_API_Key_Secret
X_ACCESS_TOKEN=你的_Access_Token
X_ACCESS_TOKEN_SECRET=你的_Access_Token_Secret
```

这种方式用的是你的 X 账号身份。如果这个账号能在网页里看到私密 List，API 通常也能读取。

## 运行

单次运行：

```bash
x-market-watch run-once
```

持续轮询：

```bash
x-market-watch daemon
```

只分析、不推送 Telegram，也不更新状态：

```bash
x-market-watch run-once --dry-run
```

启动网页控制台：

```bash
x-market-watch web
```

然后在浏览器打开：

```text
http://127.0.0.1:8787
```

网页控制台支持：

- 查看 X、LLM、Telegram 是否已配置。
- 查看当前 `last_seen_id` 和核心运行参数。
- 在左侧查看 AI 已整理和翻译的推文信号流。
- 点击信号后，用类似聊天消息的方式查看摘要、重要性说明、标签和原文链接。
- 手动触发 dry-run。
- 手动触发正式运行并推送 Telegram。
- 默认也会按 `POLL_INTERVAL_SECONDS` 自动轮询并正式推送；可用 `WEB_AUTO_POLL=false` 关闭。
- 停止当前网页触发的运行任务；如果正在等待 X/LLM 网络请求，会在下一个安全点停止。
- 查看网页触发任务的运行日志。
- 直接修改 `.env` 配置，包括密钥、模型、List ID、重要性阈值和轮询间隔。

密钥字段不会在网页里明文显示。已配置的密钥会显示为掩码；如果不想修改，保存时留空即可。如果输入新值，会覆盖 `.env` 里的旧值。

如果用 Docker 部署，控制台会默认把配置保存到 `data/.env`，所以你可以先启动容器，再在网页里填写配置。
Web 控制台默认会自动轮询；配置未填完整时会等待，填完后按 `POLL_INTERVAL_SECONDS` 周期运行。

```bash
docker compose up -d --build
```

然后访问：

```text
http://设备IP:8787
```

如果部署在云服务器或软路由，并希望从电脑浏览器访问这个控制台，需要确保设置里是：

```bash
WEB_HOST=0.0.0.0
WEB_PORT=8787
```

同时在云服务器安全组里只对你自己的 IP 开放 `8787` 端口。不要把这个控制台裸露给公网，因为它可以触发真实 Telegram 推送。

排查 X API 返回内容：

```bash
x-market-watch debug-x
```

这个命令会打印 `meta`、`errors`、返回条数和前几条推文摘要，不会打印 token。

## 部署建议

最简单的方式是在服务器上用 `systemd` 常驻运行。

`/etc/systemd/system/x-market-watch.service` 示例：

```ini
[Unit]
Description=X Market Watch
After=network-online.target

[Service]
WorkingDirectory=/opt/x-market-watch
EnvironmentFile=/opt/x-market-watch/.env
ExecStart=/opt/x-market-watch/.venv/bin/x-market-watch daemon
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

启用：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now x-market-watch
sudo journalctl -u x-market-watch -f
```

## 调整筛选逻辑

主要提示词在 [src/x_market_watch/llm.py](/Users/rxyan/Documents/x-market-watch/src/x_market_watch/llm.py)。默认会优先保留：

- 对某只股票、ETF、指数、行业或资产的明确看法
- 公司基本面、财报、估值、竞争格局、产品、订单、监管分析
- 对新闻、政策、宏观数据、行业事件的投资解读
- 交易逻辑、投资理由、催化剂、风险、反转信号或仓位思考
- 有数据、来源、图表、业绩指标或可验证事实支撑的观点

默认会过滤纯情绪喊单、无理由看涨/看跌、营销、重复转发、没有新增观点的新闻标题和与投资决策无关的泛泛资讯。

可以通过 `LLM_MIN_IMPORTANCE=1..5` 调整推送阈值，数字越高，推送越少。

如果你的 List 更新非常快，可以把 `X_MAX_PAGES` 调大到 `2..8`。服务会分页读取更多最近推文，并在本地用 `last_seen_id` 去重。

## 参考

- [X API List Posts 文档](https://docs.x.com/x-api/lists/list-tweets/introduction)
- [X API List Posts Quickstart](https://docs.x.com/x-api/lists/list-tweets/quickstart)

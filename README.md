# X-market-watch

X-market-watch 是一个 Python 小服务：定时读取你的 X List 推文，把新内容交给大模型筛选、总结或翻译成中文，然后通过 Telegram Bot 推送给你。

## 功能

- 使用 X 官方 API v2 的 List Posts endpoint 拉取 List timeline。
- 支持读取私密列表：`.env` 中的 `X_BEARER_TOKEN` 需要具备读取该私密列表的权限。
- 使用 OpenAI-compatible Chat Completions 接口做中文市场情报筛选和摘要。
- Telegram Bot 推送，默认关闭网页预览。
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
cp .env.example .env
```

编辑 `.env`：

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

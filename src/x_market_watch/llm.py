from __future__ import annotations

import json
import logging

import httpx
from pydantic import BaseModel, Field, ValidationError

from x_market_watch.models import Post, Signal

logger = logging.getLogger(__name__)


class SignalPayload(BaseModel):
    post_id: str
    keep: bool
    importance: int = Field(ge=1, le=5)
    title: str
    summary_zh: str
    why_it_matters_zh: str
    tickers: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class AnalysisPayload(BaseModel):
    signals: list[SignalPayload] = Field(default_factory=list)


class LLMAnalyzer:
    def __init__(self, api_key: str, api_base: str, model: str, timeout_seconds: int) -> None:
        self.model = model
        self._client = httpx.Client(
            base_url=api_base.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
        )

    def close(self) -> None:
        self._client.close()

    def analyze(self, posts: list[Post]) -> list[Signal]:
        if not posts:
            return []

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是一个服务个人投资者的投资信息筛选助手。请过滤 X 帖子中"
                        "对投资决策有实际帮助的信息，尤其关注股票、公司、行业、财报、"
                        "估值、交易逻辑、新闻解读、催化剂、风险和市场观点。"
                        "输出必须是严格 JSON，不要 Markdown。所有摘要和解释使用中文。"
                    ),
                },
                {
                    "role": "user",
                    "content": self._build_prompt(posts),
                },
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        response = self._client.post("/chat/completions", json=payload)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        try:
            parsed = AnalysisPayload.model_validate_json(content)
        except ValidationError:
            logger.warning("LLM returned invalid schema, attempting JSON extraction")
            parsed = AnalysisPayload.model_validate(self._extract_json(content))

        return [
            Signal(
                post_id=item.post_id,
                keep=item.keep,
                importance=item.importance,
                title=item.title.strip(),
                summary_zh=item.summary_zh.strip(),
                why_it_matters_zh=item.why_it_matters_zh.strip(),
                tickers=[_normalize_ticker(ticker) for ticker in item.tickers if ticker.strip()],
                tags=[tag.strip() for tag in item.tags if tag.strip()],
            )
            for item in parsed.signals
        ]

    @staticmethod
    def _build_prompt(posts: list[Post]) -> str:
        items = []
        for post in posts:
            author = post.author.display if post.author else post.author_id
            items.append(
                {
                    "post_id": post.id,
                    "author": author,
                    "created_at": post.created_at,
                    "text": post.text,
                    "metrics": post.metrics,
                    "url": post.url,
                }
            )
        return (
            "请分析下面的帖子列表。对每条帖子判断是否值得推送给一个关注投资决策的用户。"
            "优先保留这些内容："
            "1. 对某只股票、ETF、指数、行业或资产的明确看法；"
            "2. 对公司基本面、财报、估值、竞争格局、产品、订单、管理层、监管的分析；"
            "3. 对新闻、政策、宏观数据、行业事件的评论，并说明可能影响哪些资产；"
            "4. 提供交易逻辑、投资理由、催化剂、风险、反转信号或仓位思考；"
            "5. 有数据、来源、图表、业绩指标或可验证事实支撑的观点。"
            "过滤这些内容："
            "纯情绪喊单、无理由看涨/看跌、段子、营销、抽奖、重复转发、"
            "没有新增观点的新闻标题、无法验证的传言、与投资决策无关的泛泛资讯。"
            "importance 用 1-5 分："
            "1=无投资价值；2=弱相关；3=有参考价值；4=值得重点看；"
            "5=可能影响投资判断或需要立即关注。"
            "tickers 字段只填写推文正文中明确提及的股票代码、ETF 或资产代码，"
            "格式统一为 $TSLA、$NVDA；没有明确提及时返回空数组。不要凭公司名臆测代码。"
            "返回 JSON 格式："
            '{"signals":[{"post_id":"...","keep":true,"importance":4,"title":"...",'
            '"summary_zh":"...","why_it_matters_zh":"...","tickers":["$NVDA"],'
            '"tags":["个股","财报"]}]}'
            "\n\n帖子：\n"
            f"{json.dumps(items, ensure_ascii=False)}"
        )

    @staticmethod
    def _extract_json(content: str) -> dict[str, object]:
        start = content.find("{")
        end = content.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("No JSON object found in LLM response")
        return json.loads(content[start : end + 1])


def _normalize_ticker(ticker: str) -> str:
    normalized = ticker.strip().upper()
    if not normalized:
        return normalized
    return normalized if normalized.startswith("$") else f"${normalized}"

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from backend.market_research import MarketResearchResult


POSITIVE_KEYWORDS = {
    "业绩增长",
    "业绩预增",
    "扭亏",
    "回购",
    "增持",
    "中标",
    "订单增长",
    "景气",
    "涨价",
    "政策支持",
    "创新高",
}
NEGATIVE_KEYWORDS = {
    "业绩下滑",
    "亏损",
    "减持",
    "诉讼",
    "处罚",
    "问询函",
    "终止",
    "下调",
    "风险提示",
    "大股东质押",
}
SEVERE_RISK_KEYWORDS = {
    "立案调查",
    "退市风险",
    "重大违法",
    "财务造假",
    "债务违约",
    "暂停上市",
    "无法表示意见",
}
RISK_NEGATIONS = {"不存在", "不涉及", "未触发", "未收到", "否认", "澄清"}


@dataclass(frozen=True)
class NewsResearchConfig:
    enabled: bool = False
    freshness: str = "oneWeek"
    results_per_query: int = 5
    max_sectors: int = 3
    max_stocks: int = 8
    factor_weight: float = 0.05
    timeout_seconds: int = 20


class BochaNewsClient:
    endpoint = "https://api.bochaai.com/v1/web-search"

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: int = 20,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("Bocha 新闻研究已启用，但 BOCHA_API_KEY 为空")
        self._api_key = api_key
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_seconds)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def search(
        self,
        query: str,
        *,
        freshness: str,
        count: int,
    ) -> list[dict[str, Any]]:
        response = self._client.post(
            self.endpoint,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={
                "query": query,
                "freshness": freshness,
                "summary": True,
                "count": max(1, min(int(count), 10)),
            },
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") not in (None, 0, 200):
            raise RuntimeError(
                f"Bocha API 返回错误：{payload.get('msg') or payload.get('message')}"
            )
        return _extract_results(payload)


def _extract_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    web_pages = data.get("webPages") if isinstance(data, dict) else None
    if isinstance(web_pages, dict):
        values = web_pages.get("value") or []
    elif isinstance(data, dict):
        values = data.get("value") or data.get("results") or []
    else:
        values = []

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or item.get("link") or "").strip()
        title = str(item.get("name") or item.get("title") or "").strip()
        if not url or not title or url in seen:
            continue
        seen.add(url)
        site = item.get("siteName") or item.get("site") or item.get("source")
        if isinstance(site, dict):
            site = site.get("name")
        normalized.append(
            {
                "title": title[:180],
                "url": url,
                "summary": str(
                    item.get("summary")
                    or item.get("snippet")
                    or item.get("description")
                    or ""
                )[:360],
                "source": str(site or ""),
                "published_at": str(
                    item.get("datePublished")
                    or item.get("publishedAt")
                    or item.get("dateLastCrawled")
                    or ""
                ),
            }
        )
    return normalized


def _score_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    positive_hits: set[str] = set()
    negative_hits: set[str] = set()
    severe_hits: set[str] = set()
    for item in items:
        content = f"{item.get('title', '')} {item.get('summary', '')}"
        positive_hits.update(word for word in POSITIVE_KEYWORDS if word in content)
        negative_hits.update(word for word in NEGATIVE_KEYWORDS if word in content)
        severe_hits.update(
            word
            for word in SEVERE_RISK_KEYWORDS
            if word in content
            and not any(f"{negation}{word}" in content for negation in RISK_NEGATIONS)
        )

    if not items:
        sentiment = 0.5
    else:
        raw = len(positive_hits) - len(negative_hits) - len(severe_hits) * 2
        sentiment = max(0.0, min(1.0, 0.5 + raw * 0.08))
    return {
        "sentiment_score": round(sentiment, 4),
        "risk_level": "high" if severe_hits else ("medium" if negative_hits else "normal"),
        "positive_keywords": sorted(positive_hits),
        "negative_keywords": sorted(negative_hits),
        "severe_risk_keywords": sorted(severe_hits),
        "items": items,
    }


def enrich_with_bocha_news(
    result: MarketResearchResult,
    api_key: str,
    config: NewsResearchConfig,
    *,
    client: httpx.Client | None = None,
) -> MarketResearchResult:
    if not config.enabled:
        result.summary["news"] = {
            "enabled": False,
            "provider": "bocha",
            "message": "新闻研究未启用",
            "sectors": [],
            "stocks": [],
            "errors": [],
        }
        return result

    bocha = BochaNewsClient(
        api_key,
        timeout_seconds=config.timeout_seconds,
        client=client,
    )
    errors: list[str] = []
    sector_results: list[dict[str, Any]] = []
    stock_results: list[dict[str, Any]] = []
    try:
        for sector in result.summary.get("top_sectors", [])[: config.max_sectors]:
            name = str(sector["name"])
            try:
                items = bocha.search(
                    f"{name} A股 行业 政策 景气 风险 最新消息",
                    freshness=config.freshness,
                    count=config.results_per_query,
                )
                sector_results.append({"name": name, **_score_items(items)})
            except Exception as exc:
                errors.append(
                    f"板块 {name} 新闻检索失败：{type(exc).__name__}: {exc}"
                )

        for candidate in result.summary.get("candidates", [])[: config.max_stocks]:
            symbol = str(candidate["symbol"])
            name = str(candidate["name"])
            try:
                items = bocha.search(
                    f"{name} {symbol.split('.')[0]} 公告 业绩 风险 最新消息",
                    freshness=config.freshness,
                    count=config.results_per_query,
                )
                scored = _score_items(items)
                stock_results.append(
                    {
                        "symbol": symbol,
                        "name": name,
                        **scored,
                    }
                )
            except Exception as exc:
                errors.append(
                    f"个股 {name} 新闻检索失败：{type(exc).__name__}: {exc}"
                )
    finally:
        bocha.close()

    news_by_symbol = {item["symbol"]: item for item in stock_results}
    news_by_sector = {item["name"]: item for item in sector_results}
    weight = max(0.0, min(float(config.factor_weight), 0.10))
    candidates = []
    for candidate in result.summary.get("candidates", []):
        enriched = dict(candidate)
        stock_news = news_by_symbol.get(candidate["symbol"])
        sector_news = news_by_sector.get(candidate["sector"])
        stock_sentiment = (
            float(stock_news["sentiment_score"]) if stock_news else 0.5
        )
        sector_sentiment = (
            float(sector_news["sentiment_score"]) if sector_news else 0.5
        )
        sentiment = stock_sentiment * 0.70 + sector_sentiment * 0.30
        enriched["news_score"] = round(sentiment, 4)
        enriched["sector_news_score"] = round(sector_sentiment, 4)
        enriched["combined_score"] = round(
            float(candidate["factor_score"]) * (1 - weight) + sentiment * weight,
            4,
        )
        enriched["news_risk_level"] = (
            stock_news["risk_level"] if stock_news else "unknown"
        )
        enriched["excluded_by_news"] = bool(
            stock_news and stock_news["risk_level"] == "high"
        )
        candidates.append(enriched)
    candidates.sort(key=lambda item: item["combined_score"], reverse=True)
    result.summary["candidates"] = candidates
    result.summary["news"] = {
        "enabled": True,
        "provider": "bocha",
        "as_of_date": result.summary.get("trade_date"),
        "factor_weight": weight,
        "sectors": sector_results,
        "stocks": stock_results,
        "errors": errors,
        "method": "关键词情绪评分 + 重大事件风险否决；不使用新闻直接下单",
    }
    result.summary["warnings"].extend(errors)
    return result

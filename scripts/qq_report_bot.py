from __future__ import annotations

"""QQ 群日报机器人（被动回复模式）。

GitHub Actions 每天定时生成日报并提交到仓库 outputs/ 目录；
本脚本常驻本地（或云服务器），当群里 @机器人 并发送关键词时，
从 GitHub 拉取最新日报，分段回复到群（被动消息，带 msg_id）。

依赖：pip install qq-botpy httpx
运行：QQ_APP_ID=xxx QQ_APP_SECRET=xxx python scripts/qq_report_bot.py
"""

import argparse
import asyncio
import logging
import os
import re
from typing import Any

import httpx

LOGGER = logging.getLogger("qq-report-bot")

try:
    import botpy
    from botpy.message import GroupMessage
except ImportError:  # 便于单元测试在未安装 botpy 的环境下导入纯函数
    botpy = None  # type: ignore[assignment]
    GroupMessage = Any  # type: ignore[assignment]

# 关键词 -> GitHub 上的日报文件（公开仓库 raw 地址）
REPORT_SOURCES = {
    "日报": "https://raw.githubusercontent.com/ToolmanInside/quant/main/outputs/daily-paper-report.md",
    "午盘": "https://raw.githubusercontent.com/ToolmanInside/quant/main/outputs/midday-position-report.md",
}
HELP_TEXT = "可用指令：日报（日终报告）、午盘（午间盘位）"

# QQ 文本消息单条长度上限约 3000 字符，保守分段。
CHUNK_LIMIT = 2500


def _markdown_to_plain_text(content: str) -> str:
    """把日报 markdown 降级为 QQ 可展示的纯文本。"""
    lines: list[str] = []
    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            lines.append("")
            continue
        stripped = line.lstrip()
        if stripped.startswith(("###", "##", "#")):
            line = stripped.lstrip("#").strip()
        elif stripped.startswith(">"):
            line = stripped.lstrip(">").strip()
        line = line.replace("**", "").replace("`", "")
        line = re.sub(r"\[([^\]]*)\]\(([^)]+)\)", r"\1 (\2)", line)
        if line.startswith(("- ", "* ")):
            line = line[2:]
        lines.append(line)
    return "\n".join(lines)


def _split_messages(content: str, limit: int = CHUNK_LIMIT) -> list[str]:
    """按字符数分段，避免 QQ 文本消息超限。"""
    plain = _markdown_to_plain_text(content)
    blocks = re.split(r"\n\s*\n", plain)
    chunks: list[str] = []
    current = ""
    for block in blocks:
        candidate = f"{current}\n\n{block}".strip() if current else block.strip()
        if len(candidate) > limit and current:
            chunks.append(current)
            current = block.strip()
        elif len(candidate) > limit:
            while len(block) > limit:
                chunks.append(block[:limit])
                block = block[limit:]
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [""]


def _match_keyword(content: str) -> str | None:
    """从 @消息文本中识别指令关键词。"""
    text = str(content or "").strip()
    if not text:
        return None
    for keyword in REPORT_SOURCES:
        if text == keyword or text.startswith(keyword):
            return keyword
    if text in ("帮助", "help", "指令", "菜单"):
        return "__help__"
    return None


def _fetch_report(url: str, client: httpx.Client) -> str:
    response = client.get(url)
    response.raise_for_status()
    return response.text


if botpy is not None:

    class ReportClient(botpy.Client):  # type: ignore[misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._http = httpx.Client(timeout=20, follow_redirects=True)

        async def on_group_at_message_create(
            self, message: GroupMessage
        ) -> None:
            keyword = _match_keyword(message.content)
            if keyword is None:
                return
            LOGGER.info(
                "群 %s 收到指令：%s", message.group_openid, keyword
            )
            try:
                if keyword == "__help__":
                    await self._reply(
                        message, HELP_TEXT
                    )
                    return
                url = REPORT_SOURCES[keyword]
                report = await asyncio.to_thread(
                    _fetch_report, url, self._http
                )
                chunks = _split_messages(report)
                LOGGER.info(
                    "拉取 %s 成功，分段 %s 条", keyword, len(chunks)
                )
                for chunk in chunks:
                    await self._reply(message, chunk)
            except Exception as exc:
                LOGGER.exception("回复 %s 失败", keyword)
                await self._reply(
                    message,
                    f"获取{keyword}失败：{type(exc).__name__}，"
                    "请确认 GitHub Actions 已完成当日任务。",
                )

        async def _reply(
            self, message: GroupMessage, content: str
        ) -> None:
            """被动回复：带 msg_id；同一消息多次回复时递增 msg_seq。"""
            chunks = _split_messages(content)
            for index, chunk in enumerate(chunks, start=1):
                await self.api.post_group_message(
                    group_openid=message.group_openid,
                    msg_type=0,
                    content=chunk,
                    msg_id=message.id,
                    msg_seq=index,
                )
                LOGGER.info("已回复第 %s/%s 段", index, len(chunks))

else:
    ReportClient = None  # type: ignore[assignment]


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    parser = argparse.ArgumentParser(description="QQ 群日报机器人（被动回复）")
    parser.add_argument("--app-id", default=os.getenv("QQ_APP_ID", ""))
    parser.add_argument("--app-secret", default=os.getenv("QQ_APP_SECRET", ""))
    args = parser.parse_args()
    if not args.app_id or not args.app_secret:
        print("缺少 QQ_APP_ID / QQ_APP_SECRET（参数或环境变量）")
        return 1
    if botpy is None:
        print("缺少依赖：pip install qq-botpy httpx")
        return 1

    # Python 3.12+ 需要先手动创建事件循环，否则 botpy Client 初始化报错。
    asyncio.set_event_loop(asyncio.new_event_loop())

    client = ReportClient(
        intents=botpy.Intents(public_messages=True),
        is_sandbox=False,
    )
    LOGGER.info("日报机器人已启动，等待群内 @指令…")
    client.run(appid=args.app_id, secret=args.app_secret)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

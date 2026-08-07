import pytest

from scripts import qq_report_bot as bot


def test_match_keyword_recognizes_commands() -> None:
    assert bot._match_keyword("日报") == "日报"
    assert bot._match_keyword("午盘") == "午盘"
    assert bot._match_keyword("帮助") == "__help__"
    assert bot._match_keyword("help") == "__help__"
    assert bot._match_keyword("随便聊聊") is None
    assert bot._match_keyword("") is None
    assert bot._match_keyword(None) is None


def test_markdown_is_downgraded_to_plain_text() -> None:
    markdown = (
        "### 账户概览\n"
        "> 信号日：**2026-08-06**\n"
        "- 当前权益：**¥99,904.96**\n"
    )
    plain = bot._markdown_to_plain_text(markdown)
    assert "账户概览" in plain
    assert "###" not in plain
    assert "**" not in plain
    assert "信号日：2026-08-06" in plain


def test_long_report_is_split_without_omitting_tail() -> None:
    content = "\n\n".join(
        f"#### 段落 {index}\n" + "内容" * 300 for index in range(10)
    )
    chunks = bot._split_messages(content)
    assert len(chunks) > 1
    assert any("段落 9" in chunk for chunk in chunks)
    assert all(len(chunk) <= bot.CHUNK_LIMIT for chunk in chunks)


def test_reply_splits_and_increments_msg_seq() -> None:
    """被动回复必须带 msg_id，多段递增 msg_seq（文档：相同msg_id+msg_seq重复发送会失败）。"""
    if bot.ReportClient is None:
        pytest.skip("qq-botpy 未安装，跳过依赖 botpy 的用例")
    sent: list[dict] = []

    class FakeAPI:
        async def post_group_message(self, **kwargs) -> None:
            sent.append(kwargs)

    class FakeMessage:
        id = "MSG_123"
        group_openid = "GROUP_OPENID"

    async def run() -> None:
        client = object.__new__(bot.ReportClient)
        client.api = FakeAPI()
        await bot.ReportClient._reply(client, FakeMessage(), "第一段内容" * 800)

    import asyncio

    asyncio.run(run())

    assert len(sent) > 1
    for index, payload in enumerate(sent, start=1):
        assert payload["msg_id"] == "MSG_123"
        assert payload["msg_seq"] == index
        assert payload["group_openid"] == "GROUP_OPENID"
        assert payload["msg_type"] == 0

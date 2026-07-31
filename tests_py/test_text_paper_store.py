import json

from backend.paper_store import PaperStore


def test_one_account_is_persisted_as_readable_text(tmp_path) -> None:
    path = tmp_path / "account-a.txt"
    store = PaperStore(path)
    store.reset_account(
        "account-a",
        100_000,
        ["000001.SZ"],
        "v1.0-balanced",
        {"strategy_id": "moving_average"},
    )
    account = store.account("account-a")
    assert account is not None
    account["last_date"] = "2026-07-31"
    account["pending_plan"] = [{"symbol": "000001.SZ", "action": "BUY"}]
    store.save_account(account)
    store.close()

    raw = path.read_text(encoding="utf-8")
    document = json.loads(raw)
    assert document["schema_version"] == 1
    assert document["account"]["account_id"] == "account-a"
    assert document["account"]["pending_plan"][0]["action"] == "BUY"
    assert "\n  \"account\"" in raw

    restored = PaperStore(path)
    assert restored.account("account-a")["last_date"] == "2026-07-31"
    restored.close()

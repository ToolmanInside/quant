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
    assert document["schema_version"] == 2
    assert document["account"]["account_id"] == "account-a"
    assert document["account"]["pending_plan"][0]["action"] == "BUY"
    assert "\n  \"account\"" in raw

    restored = PaperStore(path)
    assert restored.account("account-a")["last_date"] == "2026-07-31"
    restored.close()


def test_v1_account_requests_replay_after_automatic_migration(tmp_path) -> None:
    path = tmp_path / "legacy.txt"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "account": {
                    "account_id": "legacy",
                    "initial_cash": 100_000,
                    "cash": 100_000,
                },
                "positions": [],
                "executions": [],
                "snapshots": [],
                "reviews": [],
                "daily_journals": [],
                "strategy_versions": [],
                "upgrade_events": [],
                "counters": {},
            }
        ),
        encoding="utf-8",
    )

    store = PaperStore(path)
    account = store.account("legacy")
    assert account is not None
    assert account["requires_reinitialize_reason"] == "corporate_action_ledger_v2"
    store.close()
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 2

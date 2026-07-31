from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any


ACCOUNT_STATE_DIR = (
    Path(__file__).resolve().parent / "data" / "simulation" / "accounts"
)
DATABASE_PATH = ACCOUNT_STATE_DIR / "default.txt"
SCHEMA_VERSION = 2
ACCOUNT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")


def account_state_path(
    account_id: str,
    directory: Path = ACCOUNT_STATE_DIR,
) -> Path:
    if not ACCOUNT_ID_PATTERN.fullmatch(account_id):
        raise ValueError("模拟账户标识只能包含字母、数字、下划线和连字符")
    return directory / f"{account_id}.txt"


def _empty_document() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "account": None,
        "positions": [],
        "executions": [],
        "corporate_actions": [],
        "snapshots": [],
        "reviews": [],
        "daily_journals": [],
        "strategy_versions": [],
        "upgrade_events": [],
        "counters": {
            "execution": 0,
            "corporate_action": 0,
            "review": 0,
            "upgrade_event": 0,
        },
    }


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime,)):
        return value.isoformat()
    item = getattr(value, "item", None)
    if callable(item):
        return item()
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    raise TypeError(f"文本账本无法序列化 {type(value).__name__}")


class PaperStore:
    """A small, atomic JSON document store backed by one UTF-8 .txt file.

    One instance is scoped to one paper account. A run is processed in memory
    and atomically persisted when the store closes.
    """

    def __init__(self, path: Path = DATABASE_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._dirty = False
        self._document = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return _empty_document()
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"模拟账户文本账本无法读取：{self.path}") from exc
        version = document.get("schema_version", 1)
        if version == 1:
            document["schema_version"] = SCHEMA_VERSION
            document.setdefault("corporate_actions", [])
            document.setdefault("counters", {}).setdefault("corporate_action", 0)
            for position in document.get("positions", []):
                position.setdefault(
                    "cost_basis_total",
                    float(position.get("avg_price", 0)) * int(position.get("shares", 0)),
                )
            if document.get("account") is not None:
                document["account"]["requires_reinitialize_reason"] = (
                    "corporate_action_ledger_v2"
                )
            self._dirty = True
        elif version != SCHEMA_VERSION:
            raise ValueError(
                f"不支持的模拟账户账本版本：{document.get('schema_version')}"
            )
        template = _empty_document()
        for key, default in template.items():
            document.setdefault(key, deepcopy(default))
        return document

    def _persist(self) -> None:
        if not self._dirty:
            return
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(
            json.dumps(
                self._document,
                ensure_ascii=False,
                indent=2,
                default=_json_default,
            ),
            encoding="utf-8",
        )
        temporary.replace(self.path)
        self._dirty = False

    def _touch(self) -> None:
        self._dirty = True

    def _assert_account(self, account_id: str) -> None:
        account = self._document.get("account")
        if account is not None and account["account_id"] != account_id:
            raise ValueError(
                f"文本账本属于账户 {account['account_id']}，不能用于 {account_id}"
            )

    def _next_id(self, name: str) -> int:
        value = int(self._document["counters"].get(name, 0)) + 1
        self._document["counters"][name] = value
        return value

    def close(self) -> None:
        self._persist()

    def reset_account(
        self,
        account_id: str,
        initial_cash: float,
        universe: list[str],
        version: str,
        configuration: dict[str, Any] | None = None,
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        self._document = _empty_document()
        self._document["account"] = {
            "account_id": account_id,
            "initial_cash": initial_cash,
            "cash": initial_cash,
            "peak_equity": initial_cash,
            "current_version": version,
            "last_date": None,
            "pending_plan": [],
            "universe": list(universe),
            "configuration": deepcopy(configuration or {}),
            "created_at": now,
            "updated_at": now,
        }
        self._touch()

    def account(self, account_id: str) -> dict[str, Any] | None:
        self._assert_account(account_id)
        account = self._document.get("account")
        return deepcopy(account) if account else None

    def save_account(self, account: dict[str, Any]) -> None:
        self._assert_account(account["account_id"])
        stored = self._document.get("account")
        if stored is None:
            raise ValueError("模拟账户尚未初始化")
        for key in (
            "cash",
            "peak_equity",
            "current_version",
            "last_date",
            "pending_plan",
            "universe",
            "configuration",
            "market_research",
        ):
            if key in account:
                stored[key] = deepcopy(account.get(key))
        stored["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self._touch()

    def positions(self, account_id: str) -> list[dict[str, Any]]:
        self._assert_account(account_id)
        return sorted(
            deepcopy(self._document["positions"]),
            key=lambda item: item["symbol"],
        )

    def save_positions(
        self,
        account_id: str,
        positions: dict[str, dict[str, Any]],
    ) -> None:
        self._assert_account(account_id)
        self._document["positions"] = [
            {
                "account_id": account_id,
                "symbol": symbol,
                "name": position["name"],
                "sector": position["sector"],
                "shares": int(position["shares"]),
                "avg_price": float(position["avg_price"]),
                "cost_basis_total": float(
                    position.get(
                        "cost_basis_total",
                        float(position["avg_price"]) * int(position["shares"]),
                    )
                ),
                "entry_date": position["entry_date"],
            }
            for symbol, position in sorted(positions.items())
        ]
        self._touch()

    def add_execution(self, account_id: str, execution: dict[str, Any]) -> None:
        self._assert_account(account_id)
        self._document["executions"].append(
            {
                "id": self._next_id("execution"),
                "account_id": account_id,
                **deepcopy(execution),
            }
        )
        self._touch()

    def add_corporate_action(
        self,
        account_id: str,
        action: dict[str, Any],
    ) -> None:
        self._assert_account(account_id)
        duplicate = any(
            item.get("trade_date") == action.get("trade_date")
            and item.get("symbol") == action.get("symbol")
            and item.get("end_date") == action.get("end_date")
            for item in self._document["corporate_actions"]
        )
        if duplicate:
            return
        self._document["corporate_actions"].append(
            {
                "id": self._next_id("corporate_action"),
                "account_id": account_id,
                **deepcopy(action),
            }
        )
        self._touch()

    def add_snapshot(self, account_id: str, snapshot: dict[str, Any]) -> None:
        self._assert_account(account_id)
        item = {"account_id": account_id, **deepcopy(snapshot)}
        snapshots = self._document["snapshots"]
        snapshots[:] = [
            existing
            for existing in snapshots
            if existing["trade_date"] != snapshot["trade_date"]
        ]
        snapshots.append(item)
        self._touch()

    def add_review(self, account_id: str, review: dict[str, Any]) -> None:
        self._assert_account(account_id)
        duplicate = any(
            item["trade_date"] == review["trade_date"]
            and item["category"] == review["category"]
            for item in self._document["reviews"]
        )
        if duplicate:
            return
        self._document["reviews"].append(
            {
                "id": self._next_id("review"),
                "account_id": account_id,
                **deepcopy(review),
            }
        )
        self._touch()

    def add_daily_journal(
        self,
        account_id: str,
        journal: dict[str, Any],
    ) -> None:
        self._assert_account(account_id)
        item = {
            "account_id": account_id,
            **deepcopy(journal),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        journals = self._document["daily_journals"]
        journals[:] = [
            existing
            for existing in journals
            if existing["trade_date"] != journal["trade_date"]
        ]
        journals.append(item)
        self._touch()

    def attach_market_research(
        self,
        account_id: str,
        trade_date: str,
        research: dict[str, Any],
    ) -> None:
        """Attach the cross-sectional evidence to that day's audit journal."""
        self._assert_account(account_id)
        for journal in reversed(self._document["daily_journals"]):
            if journal.get("trade_date") == trade_date:
                journal["market_research"] = deepcopy(research)
                self._touch()
                return

    def market_contexts(self, account_id: str) -> dict[str, dict[str, Any]]:
        """Return only research saved against its original trading date."""
        self._assert_account(account_id)
        return {
            str(journal["trade_date"]): deepcopy(journal["market_research"])
            for journal in self._document["daily_journals"]
            if journal.get("trade_date") and journal.get("market_research")
        }

    def save_version(
        self,
        account_id: str,
        version: str,
        status: str,
        params: dict[str, Any],
        metrics: dict[str, Any],
        reason: str,
    ) -> None:
        self._assert_account(account_id)
        item = {
            "account_id": account_id,
            "version": version,
            "status": status,
            "params": deepcopy(params),
            "metrics": deepcopy(metrics),
            "reason": reason,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        versions = self._document["strategy_versions"]
        versions[:] = [
            existing for existing in versions if existing["version"] != version
        ]
        versions.append(item)
        self._touch()

    def promote_version(self, account_id: str, version: str) -> None:
        self._assert_account(account_id)
        found = False
        for item in self._document["strategy_versions"]:
            item["status"] = "champion" if item["version"] == version else "challenger"
            found = found or item["version"] == version
        if not found:
            raise ValueError(f"策略版本不存在：{version}")
        account = self._document["account"]
        account["current_version"] = version
        self._touch()

    def add_upgrade_event(self, account_id: str, event: dict[str, Any]) -> None:
        self._assert_account(account_id)
        self._document["upgrade_events"].append(
            {
                "id": self._next_id("upgrade_event"),
                "account_id": account_id,
                **deepcopy(event),
            }
        )
        self._touch()

    def positions_as_of(
        self,
        account_id: str,
        trade_date: str,
    ) -> list[dict[str, Any]]:
        self._assert_account(account_id)
        events = [
            (item["trade_date"], 1, item["id"], "execution", item)
            for item in self._document["executions"]
            if item["trade_date"] <= trade_date
        ]
        events.extend(
            (item["trade_date"], 0, item["id"], "corporate_action", item)
            for item in self._document["corporate_actions"]
            if item["trade_date"] <= trade_date
        )
        events.sort(key=lambda item: item[:3])
        positions: dict[str, dict[str, Any]] = {}
        for _, _, _, event_type, event in events:
            if event_type == "corporate_action":
                current = positions.get(event["symbol"])
                if current:
                    current["shares"] += int(event.get("shares_added", 0))
                    current["cost_basis_total"] = max(
                        float(current["cost_basis_total"])
                        - float(event.get("cash_accrued", 0)),
                        0.0,
                    )
                    if current["shares"] > 0:
                        current["avg_price"] = (
                            current["cost_basis_total"] / current["shares"]
                        )
                continue
            execution = event
            symbol = execution["symbol"]
            current = positions.get(symbol)
            if execution["action"] == "BUY":
                quantity = int(execution["quantity"])
                added_basis = float(execution["price"]) * quantity
                if current:
                    previous_shares = int(current["shares"])
                    total_shares = previous_shares + quantity
                    current["cost_basis_total"] += added_basis
                    current["avg_price"] = current["cost_basis_total"] / total_shares
                    current["shares"] = total_shares
                else:
                    positions[symbol] = {
                        "symbol": symbol,
                        "name": execution["name"],
                        "sector": execution["sector"],
                        "shares": quantity,
                        "avg_price": float(execution["price"]),
                        "cost_basis_total": added_basis,
                        "entry_date": execution["trade_date"],
                    }
                continue
            if current:
                previous_shares = int(current["shares"])
                sold = min(int(execution["quantity"]), previous_shares)
                current["cost_basis_total"] *= (
                    (previous_shares - sold) / previous_shares
                )
                current["shares"] -= sold
                if current["shares"] <= 0:
                    positions.pop(symbol, None)
                else:
                    current["avg_price"] = (
                        current["cost_basis_total"] / current["shares"]
                    )
        return sorted(positions.values(), key=lambda item: item["symbol"])

    def dashboard(self, account_id: str) -> dict[str, Any]:
        account = self.account(account_id)
        if account is None:
            raise ValueError("模拟账户尚未初始化，请先运行历史回放")

        snapshots = sorted(
            deepcopy(self._document["snapshots"]),
            key=lambda item: item["trade_date"],
        )
        latest = snapshots[-1] if snapshots else None
        executions = sorted(
            deepcopy(self._document["executions"]),
            key=lambda item: (item["trade_date"], item["id"]),
            reverse=True,
        )[:30]
        corporate_actions = sorted(
            deepcopy(self._document["corporate_actions"]),
            key=lambda item: (item["trade_date"], item["id"]),
            reverse=True,
        )[:30]
        reviews = sorted(
            deepcopy(self._document["reviews"]),
            key=lambda item: (item["trade_date"], item["id"]),
            reverse=True,
        )[:12]
        versions = sorted(
            deepcopy(self._document["strategy_versions"]),
            key=lambda item: item["created_at"],
            reverse=True,
        )
        upgrades = sorted(
            deepcopy(self._document["upgrade_events"]),
            key=lambda item: item["id"],
            reverse=True,
        )[:5]
        all_journals = sorted(
            deepcopy(self._document["daily_journals"]),
            key=lambda item: item["trade_date"],
            reverse=True,
        )

        invested = [
            item for item in snapshots if float(item.get("market_value", 0)) > 0
        ]
        last_exit_candidates = [
            item
            for item in self._document["executions"]
            if item["action"] == "CLOSE"
        ]
        last_exit_date = (
            max(item["trade_date"] for item in last_exit_candidates)
            if last_exit_candidates
            else None
        )
        last_exits = sorted(
            (
                deepcopy(item)
                for item in last_exit_candidates
                if item["trade_date"] == last_exit_date
            ),
            key=lambda item: item["id"],
            reverse=True,
        )
        last_holding_date = (
            max(item["trade_date"] for item in invested) if invested else None
        )
        holding_summary = {
            "invested_days": len(invested),
            "first_holding_date": (
                min(item["trade_date"] for item in invested) if invested else None
            ),
            "last_holding_date": last_holding_date,
            "last_holding_positions": (
                self.positions_as_of(account_id, last_holding_date)
                if last_holding_date
                else []
            ),
            "last_exit_date": last_exit_date,
            "last_exits": last_exits,
        }
        return {
            "account": account,
            "latest": latest,
            "positions": self.positions(account_id),
            "executions": executions,
            "corporate_actions": corporate_actions,
            "reviews": reviews,
            "daily_journals": all_journals[:30],
            "journal_count": len(all_journals),
            "versions": versions,
            "upgrade_events": upgrades,
            "equity_curve": snapshots,
            "holding_summary": holding_summary,
        }

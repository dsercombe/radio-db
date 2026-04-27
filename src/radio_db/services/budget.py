from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class DailyBudgetState:
    date: str
    llm_calls: int = 0
    usd_spent_estimate: float = 0.0


@dataclass
class MonthlyApiUsageState:
    month: str
    date: str
    brave_calls: int = 0
    google_cse_calls_month: int = 0
    google_cse_calls_day: int = 0
    tavily_calls_month: int = 0
    tavily_calls_day: int = 0
    grok_calls_month: int = 0
    grok_calls_day: int = 0
    grok_usd_month: float = 0.0
    grok_usd_day: float = 0.0
    linkup_calls_month: int = 0
    linkup_calls_day: int = 0
    duckduckgo_calls_month: int = 0
    duckduckgo_calls_day: int = 0


class CostGuard:
    def __init__(
        self,
        state_path: str | Path,
        max_daily_usd: float,
        max_llm_calls_per_day: int,
        input_price_per_1m: float,
        output_price_per_1m: float,
    ) -> None:
        self.state_path = Path(state_path)
        self.max_daily_usd = max_daily_usd
        self.max_llm_calls_per_day = max_llm_calls_per_day
        self.input_price_per_1m = input_price_per_1m
        self.output_price_per_1m = output_price_per_1m
        self.state = self._load_state()

    def _today(self) -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def _load_state(self) -> DailyBudgetState:
        if not self.state_path.exists():
            return DailyBudgetState(date=self._today())

        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            state = DailyBudgetState(
                date=str(payload.get("date", self._today())),
                llm_calls=int(payload.get("llm_calls", 0)),
                usd_spent_estimate=float(payload.get("usd_spent_estimate", 0.0)),
            )
        except Exception:
            state = DailyBudgetState(date=self._today())

        if state.date != self._today():
            return DailyBudgetState(date=self._today())
        return state

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(
                {
                    "date": self.state.date,
                    "llm_calls": self.state.llm_calls,
                    "usd_spent_estimate": round(self.state.usd_spent_estimate, 6),
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    @staticmethod
    def estimate_input_tokens(text: str) -> int:
        # Conservative approximation for mixed web text.
        return max(1, int(len(text) / 3.5))

    def estimate_call_cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens / 1_000_000) * self.input_price_per_1m + (output_tokens / 1_000_000) * self.output_price_per_1m

    def can_spend(self, estimated_usd: float) -> bool:
        if self.max_daily_usd <= 0:
            return False
        return (self.state.usd_spent_estimate + estimated_usd) <= self.max_daily_usd

    def can_call_llm_today(self) -> bool:
        return self.state.llm_calls < self.max_llm_calls_per_day

    def register_call(self, estimated_usd: float) -> None:
        self.state.llm_calls += 1
        self.state.usd_spent_estimate += max(0.0, estimated_usd)
        self._save_state()


class ApiUsageGuard:
    def __init__(self, state_path: str | Path, max_brave_calls_per_month: int) -> None:
        self.state_path = Path(state_path)
        self.max_brave_calls_per_month = max_brave_calls_per_month
        self.state = self._load_state()

    def _current_month(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def _today(self) -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def _load_state(self) -> MonthlyApiUsageState:
        if not self.state_path.exists():
            return MonthlyApiUsageState(month=self._current_month(), date=self._today())

        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            state = MonthlyApiUsageState(
                month=str(payload.get("month", self._current_month())),
                date=str(payload.get("date", self._today())),
                brave_calls=int(payload.get("brave_calls", 0)),
                google_cse_calls_month=int(payload.get("google_cse_calls_month", 0)),
                google_cse_calls_day=int(payload.get("google_cse_calls_day", 0)),
                tavily_calls_month=int(payload.get("tavily_calls_month", 0)),
                tavily_calls_day=int(payload.get("tavily_calls_day", 0)),
                grok_calls_month=int(payload.get("grok_calls_month", 0)),
                grok_calls_day=int(payload.get("grok_calls_day", 0)),
                grok_usd_month=float(payload.get("grok_usd_month", 0.0)),
                grok_usd_day=float(payload.get("grok_usd_day", 0.0)),
                linkup_calls_month=int(payload.get("linkup_calls_month", 0)),
                linkup_calls_day=int(payload.get("linkup_calls_day", 0)),
                duckduckgo_calls_month=int(payload.get("duckduckgo_calls_month", 0)),
                duckduckgo_calls_day=int(payload.get("duckduckgo_calls_day", 0)),
            )
        except Exception:
            state = MonthlyApiUsageState(month=self._current_month(), date=self._today())

        if state.month != self._current_month():
            state = MonthlyApiUsageState(
                month=self._current_month(),
                date=self._today(),
                brave_calls=0,
                google_cse_calls_month=0,
                google_cse_calls_day=0,
                tavily_calls_month=0,
                tavily_calls_day=0,
                grok_calls_month=0,
                grok_calls_day=0,
                grok_usd_month=0.0,
                grok_usd_day=0.0,
                linkup_calls_month=0,
                linkup_calls_day=0,
                duckduckgo_calls_month=0,
                duckduckgo_calls_day=0,
            )
        elif state.date != self._today():
            state.date = self._today()
            state.google_cse_calls_day = 0
            state.tavily_calls_day = 0
            state.grok_calls_day = 0
            state.grok_usd_day = 0.0
            state.linkup_calls_day = 0
            state.duckduckgo_calls_day = 0
        return state

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(
                {
                    "month": self.state.month,
                    "date": self.state.date,
                    "brave_calls": self.state.brave_calls,
                    "google_cse_calls_month": self.state.google_cse_calls_month,
                    "google_cse_calls_day": self.state.google_cse_calls_day,
                    "tavily_calls_month": self.state.tavily_calls_month,
                    "tavily_calls_day": self.state.tavily_calls_day,
                    "grok_calls_month": self.state.grok_calls_month,
                    "grok_calls_day": self.state.grok_calls_day,
                    "grok_usd_month": round(self.state.grok_usd_month, 6),
                    "grok_usd_day": round(self.state.grok_usd_day, 6),
                    "linkup_calls_month": self.state.linkup_calls_month,
                    "linkup_calls_day": self.state.linkup_calls_day,
                    "duckduckgo_calls_month": self.state.duckduckgo_calls_month,
                    "duckduckgo_calls_day": self.state.duckduckgo_calls_day,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def can_call_brave(self) -> bool:
        if self.max_brave_calls_per_month <= 0:
            return False
        return self.state.brave_calls < self.max_brave_calls_per_month

    def register_brave_call(self) -> None:
        self.state.brave_calls += 1
        self._save_state()

    def can_call_google_cse(self, max_daily_calls: int, max_monthly_calls: int) -> bool:
        if max_daily_calls <= 0 or max_monthly_calls <= 0:
            return False
        return self.state.google_cse_calls_day < max_daily_calls and self.state.google_cse_calls_month < max_monthly_calls

    def register_google_cse_call(self) -> None:
        self.state.google_cse_calls_day += 1
        self.state.google_cse_calls_month += 1
        self._save_state()

    def can_call_tavily(self, max_daily_calls: int, max_monthly_calls: int) -> bool:
        if max_daily_calls <= 0 or max_monthly_calls <= 0:
            return False
        return self.state.tavily_calls_day < max_daily_calls and self.state.tavily_calls_month < max_monthly_calls

    def register_tavily_call(self) -> None:
        self.state.tavily_calls_day += 1
        self.state.tavily_calls_month += 1
        self._save_state()

    def can_call_grok(
        self,
        max_daily_calls: int,
        max_monthly_calls: int,
        max_monthly_usd: float,
        estimated_call_usd: float,
    ) -> bool:
        if max_daily_calls <= 0 or max_monthly_calls <= 0 or max_monthly_usd <= 0:
            return False
        if self.state.grok_calls_day >= max_daily_calls or self.state.grok_calls_month >= max_monthly_calls:
            return False
        projected = self.state.grok_usd_month + max(0.0, estimated_call_usd)
        return projected <= max_monthly_usd

    def register_grok_call(self, usd_spent: float = 0.0) -> None:
        self.state.grok_calls_day += 1
        self.state.grok_calls_month += 1
        self.state.grok_usd_day += max(0.0, usd_spent)
        self.state.grok_usd_month += max(0.0, usd_spent)
        self._save_state()

    def can_call_linkup(self, max_daily_calls: int, max_monthly_calls: int) -> bool:
        if max_daily_calls <= 0 or max_monthly_calls <= 0:
            return False
        return self.state.linkup_calls_day < max_daily_calls and self.state.linkup_calls_month < max_monthly_calls

    def register_linkup_call(self) -> None:
        self.state.linkup_calls_day += 1
        self.state.linkup_calls_month += 1
        self._save_state()

    def can_call_duckduckgo(self, max_daily_calls: int, max_monthly_calls: int) -> bool:
        if max_daily_calls <= 0 or max_monthly_calls <= 0:
            return False
        return (
            self.state.duckduckgo_calls_day < max_daily_calls
            and self.state.duckduckgo_calls_month < max_monthly_calls
        )

    def register_duckduckgo_call(self) -> None:
        self.state.duckduckgo_calls_day += 1
        self.state.duckduckgo_calls_month += 1
        self._save_state()

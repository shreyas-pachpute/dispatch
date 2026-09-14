"""The approval matrix, as data. Decides auto / notify / approve for every proposed action. No model involved."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class Policy:
    def __init__(self, path: Path):
        self.raw = yaml.safe_load(path.read_text(encoding="utf8"))
        self.actions = self.raw["actions"]
        self.time_estimates = self.raw.get("time_estimates_minutes", {})

    def decide(self, action_type: str, amount: float | None, flags: dict[str, Any]) -> tuple[str, str]:
        """Returns (decision, rule) where rule is a one-line explanation for the control room."""
        rule = self.actions.get(action_type)
        if rule is None:
            return "approve", f"no rule for {action_type}: default approve"
        if "sensitive" in rule and flags.get("sensitive"):
            return rule["sensitive"], "sensitive counterparty"
        if flags.get("intent") and flags["intent"] in rule:
            return rule[flags["intent"]], f"{action_type}.{flags['intent']}"
        if "auto_below" in rule and amount is not None:
            if amount < rule["auto_below"]:
                return "auto", f"{action_type} under {rule['auto_below']:,.0f}"
            return rule.get("else", "approve"), f"{action_type} at or over {rule['auto_below']:,.0f}"
        if "default" in rule:
            return rule["default"], f"{action_type} default"
        return "approve", f"{action_type}: no matching rule, approve"

    def excerpt(self, action_types: list[str]) -> str:
        """Only the policy sections that apply to this case, for the context pack."""
        parts = []
        for t in action_types:
            if t in self.actions:
                parts.append(f"{t}: {self.actions[t]}")
        return "\n".join(parts) if parts else "(no action policy applies)"

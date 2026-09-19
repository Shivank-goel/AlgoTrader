"""Broker-independent position comparison; never adopts broker state implicitly."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, StrictInt


class PositionViews(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intended: dict[str, StrictInt]
    local: dict[str, StrictInt]
    broker: dict[str, StrictInt]

    def reconcile(self) -> dict:
        symbols = set(self.local) | set(self.broker)
        differences = {s: {"local": self.local.get(s, 0), "broker": self.broker.get(s, 0)}
                       for s in sorted(symbols) if self.local.get(s, 0) != self.broker.get(s, 0)}
        # Desired holdings can legitimately differ while an order is pending;
        # expose that separately from unexplained local-vs-broker divergence.
        target_delta = {s: self.intended.get(s, 0) - self.local.get(s, 0)
                        for s in sorted(set(self.intended) | set(self.local))
                        if self.intended.get(s, 0) != self.local.get(s, 0)}
        return {"entry_blocked": bool(differences), "differences": differences,
                "target_delta": target_delta,
                "reconciled": None if differences else {s: q for s, q in self.local.items() if q}}

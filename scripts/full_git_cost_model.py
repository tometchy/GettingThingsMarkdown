#!/usr/bin/env python3
"""Four-tier full-Git economics; scenario inputs, not a provider quotation."""
from dataclasses import dataclass
import argparse
import json
import math

TIERS = {"basic": (10, 1.99, 17.88), "plus": (75, 5.99, 59.88),
         "pro": (500, 24.99, 239.88), "max": (2048, 99.0, None)}

@dataclass(frozen=True)
class FullGitCosts:
    # Illustrative inputs from docs/pricing.md; local mirroring assumed.
    a_node_eur: float = 292.30
    a_usable_tib: float = 40.0
    b_node_eur: float = 384.99
    b_usable_tib: float = 21.85
    pool_utilization: float = .75
    usd_to_eur: float = .91
    vat: float = .23
    payment_fraction: float = .015
    payment_fixed_eur: float = .25
    # Explicit scenario assumptions, not measured costs.
    extra_storage_fraction: float = .25
    rebuild_capacity_fraction: float = .25
    support_eur: float = .80
    transfer_compute_eur: float = 1.0
    shared_services_eur: float = 60.0
    customers: int = 500
    max_month_usd: float = 99.0
    max_year_usd: float | None = None
    target_margin: float = .40
    required_margin_gap: float = .05

    def validate(self):
        values = vars(self)
        if any(not math.isfinite(v) or v < 0 for v in values.values() if v is not None):
            raise ValueError("Inputs must be finite and nonnegative")
        if min(self.a_node_eur, self.b_node_eur, self.a_usable_tib,
               self.b_usable_tib, self.usd_to_eur, self.customers,
               self.max_month_usd, self.pool_utilization) <= 0:
            raise ValueError("Positive resource, price and capacity inputs required")
        if not 0 < self.pool_utilization <= 1 or self.payment_fraction >= 1 or self.target_margin >= 1:
            raise ValueError("Invalid fraction")
        if self.max_year_usd is not None and self.max_year_usd <= 0:
            raise ValueError("Annual price must be positive")
        if self.payment_fraction * (1 + self.vat) + self.target_margin >= 1:
            raise ValueError("No price can achieve this margin with these fees and tax")

    def estimate(self, plan, fill=1.0, annual=False):
        self.validate()
        if plan not in TIERS or not math.isfinite(fill) or not 0 <= fill <= 1:
            raise ValueError("Invalid plan or utilization")
        quota, monthly, yearly = TIERS[plan]
        if plan == "max":
            monthly, yearly = self.max_month_usd, self.max_year_usd
        if annual and yearly is None:
            return {"plan": plan, "fill": fill, "annual": True, "status": "price_not_set"}
        gross = (yearly / 12 if annual else monthly) * self.usd_to_eur
        net = gross / (1 + self.vat)
        q = quota * fill / 1024
        a, b = 2*q, q
        overhead = (1 + self.extra_storage_fraction) * (1 + self.rebuild_capacity_fraction)
        a_reserved = a * overhead / self.pool_utilization
        b_reserved = b * overhead / self.pool_utilization
        storage = a_reserved * self.a_node_eur / self.a_usable_tib + b_reserved * self.b_node_eur / self.b_usable_tib
        fixed = storage + self.support_eur + self.transfer_compute_eur + self.shared_services_eur/self.customers
        fixed += self.payment_fixed_eur / (12 if annual else 1)
        cost = fixed + gross * self.payment_fraction
        floor = fixed / (1 - self.target_margin - self.payment_fraction * (1+self.vat)) * (1+self.vat) / self.usd_to_eur
        return dict(plan=plan, fill=fill, annual=annual, status="scenario",
                    logical_gib=quota*fill, a_data_tib=a, b_data_tib=b,
                    a_reserved_tib=a_reserved, b_reserved_tib=b_reserved,
                    storage_eur=storage, net_revenue_eur=net, monthly_cost_eur=cost,
                    profit_eur=net-cost, net_margin=(net-cost)/net,
                    gross_usd_month_for_target_margin=floor)

    def full_capacity_gate(self, annual=False):
        rows = {p: self.estimate(p, 1, annual) for p in TIERS}
        if any(x["status"] != "scenario" for x in rows.values()):
            return {"passed": False, "reason": "missing price", "plans": rows}
        margins = {p: x["net_margin"] for p,x in rows.items()}
        return {"passed": (all(m > 0 for m in margins.values()) and
                           margins["max"] >= max(margins["basic"], margins["plus"]) + self.required_margin_gap),
                "plans": rows}

if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    defaults = FullGitCosts()
    for name, value in vars(defaults).items():
        p.add_argument("--"+name.replace("_","-"), type=int if name == "customers" else float, default=value)
    args = p.parse_args()
    model = FullGitCosts(**vars(args))
    print(json.dumps({"assumptions": vars(model),
                      "scenarios": [model.estimate(t, f, a) for t in TIERS
                                    for f in (.25,.5,.75,1) for a in (False,True)],
                      "full_monthly": model.full_capacity_gate(),
                      "full_annual": model.full_capacity_gate(True)}, indent=2))

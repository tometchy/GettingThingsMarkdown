#!/usr/bin/env python3
"""Transparent pre-VAT and VAT-inclusive pricing scenarios, NOT a provider quote.

Run: python3 scripts/cost_model.py --utilization 0.75 --history-multiplier 1.5
Only provider list prices are parameters; demand, RAID efficiency, churn, card
fees and utilization are sensitivity assumptions that must be verified.
"""
from __future__ import annotations
import argparse
from dataclasses import dataclass

GIB_IN_GB = 1024**3 / 10**9


@dataclass(frozen=True)
class StorageEconomics:
    # Price assumptions from Nov 2026. Prices excl VAT, subject to change.
    archive_eur_per_tb: float = 6.49
    backup_box_eur_per_10tb: float = 20.80
    hdd_node_eur: float = 132.00
    hdd_usable_tb_before_reserve: float = 32.0
    utilization: float = 0.75
    history_multiplier: float = 1.5
    compute_eur_per_tenant: float = 0.50
    monitoring_support_eur_per_tenant: float = 0.80
    payment_variable: float = 0.015
    payment_fixed_eur: float = 0.25
    vat: float = 0.23
    minimum_margin: float = 0.25

    def model(self, storage_gib: int, gross_eur: float) -> dict[str, float]:
        if storage_gib <= 0 or gross_eur <= 0:
            raise ValueError("Storage and gross price must be positive.")
        if not 0 < self.utilization <= 1 or self.history_multiplier < 1:
            raise ValueError("Utilization must be (0,1] and archive >= live data.")
        gb = storage_gib * GIB_IN_GB
        tb = gb / 1000
        hot_eur = self.hdd_node_eur * tb / (
            self.hdd_usable_tb_before_reserve * self.utilization)
        archive_eur = self.archive_eur_per_tb * tb * self.history_multiplier
        backup_eur = self.backup_box_eur_per_10tb * (tb * self.history_multiplier / 10)
        fee = gross_eur * self.payment_variable + self.payment_fixed_eur
        revenue = gross_eur / (1 + self.vat)
        cost = hot_eur + archive_eur + backup_eur + fee
        cost += self.compute_eur_per_tenant + self.monitoring_support_eur_per_tenant
        profit = revenue - cost
        return {
            "gross_eur": round(gross_eur, 2),
            "revenue_ex_vat_eur": round(revenue, 2),
            "live_hdd_eur": round(hot_eur, 2),
            "encrypted_lfs_object_eur": round(archive_eur, 2),
            "storage_box_backup_eur": round(backup_eur, 2),
            "compute_and_ops_eur": round(
                self.compute_eur_per_tenant + self.monitoring_support_eur_per_tenant, 2),
            "payment_fee_eur": round(fee, 2),
            "profit_eur": round(profit, 2),
            "margin_on_net_revenue": round(profit / revenue, 3),
        }

    def minimum_gross_price(self, storage_gib: int) -> float:
        # Brute-force €0.01 increments to avoid algebra mistakes in the VAT,
        # fee and margin definition. This is a quoting floor, not a billing action.
        for cents in range(1, 50000):
            gross = cents / 100
            projection = self.model(storage_gib, gross)
            if projection["margin_on_net_revenue"] >= self.minimum_margin:
                return gross
        raise ValueError("No profitable price below €500 found.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--utilization", type=float, default=0.75)
    parser.add_argument("--history-multiplier", type=float, default=1.5)
    parser.add_argument("--compute-eur", type=float, default=0.5)
    parser.add_argument("--support-eur", type=float, default=0.8)
    args = parser.parse_args()
    economy = StorageEconomics(
        utilization=args.utilization,
        history_multiplier=args.history_multiplier,
        compute_eur_per_tenant=args.compute_eur,
        monitoring_support_eur_per_tenant=args.support_eur,
    )
    print("Assumptions: excluding provider VAT, 23% customer VAT,")
    print("dedicated HDD host at €132/32TB usable, 75% occupancy,")
    print("history/backup multiplier; actual price and occupancy may vary.")
    print("Plan | GB quota | example gross €/mo | profit €/mo | net margin | floor €/mo")
    for name, quota, price in [
        ("basic", 15, 3.99), ("plus", 200, 9.99), ("pro", 1024, 39.99)
    ]:
        v = economy.model(quota, price)
        print(f"{name:5} | {quota:>8} | {price:>18.2f} | "
              f"{v['profit_eur']:>11.2f} | "
              f"{100*v['margin_on_net_revenue']:>9.1f}% | "
              f"{economy.minimum_gross_price(quota):>10.2f}")
    print("IMPORTANT: not validated against real provider invoices, subscriptions,")
    print("backup versions retained >30 days, refunds, heavy churn or load tests.")


if __name__ == "__main__":
    main()

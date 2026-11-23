#!/usr/bin/env python3
"""Transparent pre-VAT and VAT-inclusive pricing scenarios, NOT a provider quote.

Run: python3 scripts/cost_model.py --utilization 0.75 --history-multiplier 1.5
Only provider list prices are parameters; demand, RAID efficiency, churn, card
fees and utilization are sensitivity assumptions that must be verified.
"""
from __future__ import annotations
import argparse
import math
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
    hdd_node_cpu_cores: float = 8.0
    hdd_node_ram_gib: float = 64.0
    schedulable_cpu_fraction: float = 0.70
    schedulable_ram_fraction: float = 0.70
    cpu_request_per_pod_cores: float = 0.10
    ram_request_per_pod_gib: float = 0.25
    controlplane_cost_eur: float = 30.0
    usd_to_eur: float = 0.9093  # Nov 23 indicative rate; adjustable in stress tests

    def fleet(self, customers: int, storage_gib: int, gross_eur: float) -> dict[str, float]:
        """Finite fleet, *two* pods per tenant and a minimum of two physical hosts.
        Assumes all customers use their full disk allowance and archive grows
        with history_multiplier. This is a stress scenario, not provider SLA.
        """
        if customers < 1 or storage_gib < 1 or gross_eur <= 0:
            raise ValueError("Positive customer count, quota, gross price required")
        if not 0 < self.utilization <= 1 or self.history_multiplier < 1:
            raise ValueError("Invalid utilization/history parameters")
        if not 0 < self.schedulable_cpu_fraction <= 1 or not 0 < self.schedulable_ram_fraction <= 1:
            raise ValueError("Invalid worker scheduling fractions")
        gigabytes = storage_gib * GIB_IN_GB
        live_tb = gigabytes * customers / 1000
        stored_history_tb = live_tb * self.history_multiplier
        workers_cpu = math.ceil(
            customers * 2 * self.cpu_request_per_pod_cores /
            (self.hdd_node_cpu_cores * self.schedulable_cpu_fraction))
        workers_ram = math.ceil(
            customers * 2 * self.ram_request_per_pod_gib /
            (self.hdd_node_ram_gib * self.schedulable_ram_fraction))
        workers_disk = math.ceil(
            live_tb / (self.hdd_usable_tb_before_reserve * self.utilization))
        worker_count = max(2, workers_cpu, workers_ram, workers_disk)
        backup_boxes = max(1, math.ceil(stored_history_tb / 10))
        # One shared S3 billing minimum; over-1TB usage is modeled at a
        # constant marginal rate in this scenario, not a provider quote.
        s3_eur = max(1.0, stored_history_tb) * self.archive_eur_per_tb
        backup_eur = backup_boxes * self.backup_box_eur_per_10tb
        workers_eur = worker_count * self.hdd_node_eur
        support_eur = customers * self.monitoring_support_eur_per_tenant
        payment_eur = customers * (gross_eur * self.payment_variable + self.payment_fixed_eur)
        monthly_cost = workers_eur + s3_eur + backup_eur + support_eur + payment_eur + self.controlplane_cost_eur
        net_revenue = customers * gross_eur / (1 + self.vat)
        return {
            "customers": customers,
            "quota_gib": storage_gib,
            "workers": worker_count,
            "history_tb": round(stored_history_tb, 2),
            "backup_boxes": backup_boxes,
            "net_revenue_eur": round(net_revenue, 2),
            "total_cost_eur": round(monthly_cost, 2),
            "monthly_profit_eur": round(net_revenue - monthly_cost, 2),
            "margin": round((net_revenue - monthly_cost) / net_revenue, 3),
        }

    def mixed_fleet(self, mini: int, plus: int, pro: int,
                    annual_share: float = 0.0) -> dict[str, float]:
        """Worst case: every account uses all advertised GiB quota.
        Costs in EUR, gross prices in USD, with FX and buyer VAT modeled.
        Two resident pods per account on at least two independent HDD nodes.
        """
        if min(mini, plus, pro) < 0 or mini + plus + pro < 1:
            raise ValueError("Provide at least one nonnegative tenant count.")
        if not 0 <= annual_share <= 1:
            raise ValueError("Annual share must be 0..1.")
        if self.usd_to_eur <= 0:
            raise ValueError("USD/EUR rate must be positive.")
        tiers = {
            "basic": (mini, 10, 1.99, 1.49, .025, .125),
            "plus": (plus, 75, 5.99, 4.99, .025, .1875),
            "pro": (pro, 500, 24.99, 19.99, .100, .250)
        }
        customers = mini + plus + pro
        gib = sum(n * quota for n, quota, _, _, _, _ in tiers.values())
        history_tb = gib * GIB_IN_GB / 1000 * self.history_multiplier
        cpu = sum(n * 2 * cores for n, _, _, _, cores, _ in tiers.values())
        ram = sum(n * 2 * memory for n, _, _, _, _, memory in tiers.values())
        nodes = max(
            2,
            math.ceil(cpu / (self.hdd_node_cpu_cores * self.schedulable_cpu_fraction)),
            math.ceil(ram / (self.hdd_node_ram_gib * self.schedulable_ram_fraction)),
            math.ceil(gib * GIB_IN_GB / 1000 /
                (self.hdd_usable_tb_before_reserve * self.utilization))
        )
        net_revenue = 0.0
        payment = 0.0
        for n, _, monthly, annually_equivalent, _, _ in tiers.values():
            monthly_share = 1 - annual_share
            gross_usd = n * (monthly * monthly_share +
                             annually_equivalent * annual_share)
            net_revenue += gross_usd * self.usd_to_eur / (1 + self.vat)
            # The fixed card fee for annual contracts is amortized over 12
            # months; annual money is received upfront, this is accrual.
            payment += n * (self.payment_fixed_eur *
                            (monthly_share + annual_share / 12))
            payment += gross_usd * self.usd_to_eur * self.payment_variable

        # StorageBox capacities are billed per whole 10TB box. These are not
        # WORM buckets themselves; provider-compatible WORM cost needs pricing.
        boxes = max(1, math.ceil(history_tb / 10))
        cost = nodes * self.hdd_node_eur +                max(1, history_tb) * self.archive_eur_per_tb +                boxes * self.backup_box_eur_per_10tb +                customers * self.monitoring_support_eur_per_tenant +                payment + self.controlplane_cost_eur
        return {
            "users": customers,
            "mini": mini,
            "plus": plus,
            "pro": pro,
            "nodes": nodes,
            "history_tb": round(history_tb, 2),
            "gross_sales_usd_equivalent": round(
                sum(n * (monthly * (1 - annual_share) +
                         annually_equivalent * annual_share)
                    for n, _, monthly, annually_equivalent, _, _ in tiers.values()), 2),
            "net_revenue_eur": round(net_revenue, 2),
            "cost_eur": round(cost, 2),
            "profit_eur": round(net_revenue - cost, 2),
            "margin": round((net_revenue - cost) / net_revenue, 3)
        }

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
    print("\nMore conservative fleet-level scenario for ONLY 200GiB Plus users:")
    print("Two pods/user each request 100m CPU + 256MiB RAM; 8-core/64GiB")
    print("HDD servers with 70% allocatable CPU+RAM, at least two workers,")
    print("one 30 EUR/month shared control-plane. No real CPU benchmarks.")
    for count in (10, 50, 100, 150):
        for price in (9.99, 15.99, 17.99):
            result = economy.fleet(count, 200, price)
            print(f"{count:3} accounts at gross EUR {price:5.2f}: "
                  f"{result['workers']} workers, "
                  f"EUR {result['monthly_profit_eur']:8.2f} profit/mo, "
                  f"{100*result['margin']:5.1f}% margin")
    print("\nSuggested public offers, gross USD/month (yearly effective in parentheses):")
    for name, gb, monthly, yearly in [
        ("mini", 10, 1.99, 1.49), ("plus", 75, 5.99, 4.99),
        ("pro", 500, 24.99, 19.99)
    ]:
        print(f"{name:5}: {gb:>3} GiB for ${monthly:5.2f}/month "
              f"(${yearly:5.2f}/month paid annually)")
    print("\nBlended tenant stress scenario: 20% Mini, 60% Plus, 20% Pro")
    for count in (20, 50, 100, 150, 300, 500, 1000):
        n_mini, n_plus = count // 5, count * 3 // 5
        n_pro = count - n_mini - n_plus
        r = economy.mixed_fleet(n_mini, n_plus, n_pro)
        print(f"{count:4} accounts: {r['nodes']:2} workers, "
              f"EUR {r['profit_eur']:8.2f}/month, {100*r['margin']:5.1f}% margin")
    print("IMPORTANT: not validated against real provider invoices, subscriptions,")
    print("backup versions retained >30 days, refunds, heavy churn or load tests.")


if __name__ == "__main__":
    main()

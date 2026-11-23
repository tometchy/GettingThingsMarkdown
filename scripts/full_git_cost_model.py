#!/usr/bin/env python3
"""Honest two-cloud full Git price sensitivity: NOT a live provider quotation.

A stores current encrypted working files PLUS full Git history; B stores full
Git history. No LFS, S3, compression assumption, or unlimited historical
bytes are magically free. Change node costs to actual offers/invoices.
"""
from __future__ import annotations
import argparse
import math
from dataclasses import dataclass

GIB_PER_TIB = 1024.0

@dataclass(frozen=True)
class FullGitCosts:
    # Assumptions, not verified EU November 2026 offers.
    hetzner_node_eur: float = 132.0
    hetzner_usable_tib: float = 32.0
    ovh_node_eur: float = 190.0
    ovh_usable_tib: float = 28.0
    disk_utilization: float = 0.75
    # Measured history ratio needed: 1.0 ~= one complete copy of current data.
    history_multiplier: float = 2.0
    usd_to_eur: float = 0.91
    vat: float = 0.23
    stripe_variable: float = 0.015
    stripe_fixed_eur: float = 0.25
    support_eur_per_user: float = 0.80
    control_plane_and_witness_eur: float = 60.0

    def estimate(self, mini: int, plus: int, pro: int,
                 annual_share: float = 0.0, warm_failover: bool = False) -> dict:
        if min(mini, plus, pro) < 0 or mini+plus+pro <= 0:
            raise ValueError("Invalid customer counts")
        if not 0 <= annual_share <= 1 or not 0 < self.disk_utilization <= 1:
            raise ValueError("Invalid utilization")
        if self.history_multiplier < 1:
            raise ValueError("Archive history ratio must be >=1")
        if min(self.hetzner_usable_tib,self.ovh_usable_tib,
               self.hetzner_node_eur,self.ovh_node_eur) <= 0:
            raise ValueError("Provider resources/costs must be positive")
        # Actual user plan limits reflect nominal current bytes, NOT a bounded
        # historical quota. A working tree + full Git; B full Git.
        tiers = ((mini,10,1.99,1.49),(plus,75,5.99,4.99),(pro,500,24.99,19.99))
        count=mini+plus+pro
        logical_gib=sum(n*quota for n,quota,_,_ in tiers)
        history_gib=logical_gib*self.history_multiplier
        a_gib=logical_gib+history_gib
        b_gib=history_gib
        # Warm-failover: either site must fit BOTH roles, not just its own.
        # A placement split across nodes also needs enough CPU/RAM, ignored
        # here; the result is a conservative *underestimate* of total cost.
        a_reserved=(a_gib+b_gib) if warm_failover else a_gib
        b_reserved=(a_gib+b_gib) if warm_failover else b_gib
        def nodes(gib,cap):
            return max(1, math.ceil(gib/(cap*GIB_PER_TIB*self.disk_utilization)))
        a_nodes=nodes(a_reserved,self.hetzner_usable_tib)
        b_nodes=nodes(b_reserved,self.ovh_usable_tib)
        gross_usd=sum(n*(month*(1-annual_share)+annual*annual_share)
                      for n,_,month,annual in tiers)
        gross_eur=gross_usd*self.usd_to_eur
        net_revenue=gross_eur/(1+self.vat)
        stripe=gross_eur*self.stripe_variable+count*self.stripe_fixed_eur*(1-annual_share+annual_share/12)
        cost=(a_nodes*self.hetzner_node_eur+b_nodes*self.ovh_node_eur+
              count*self.support_eur_per_user+self.control_plane_and_witness_eur+stripe)
        return dict(users=count,source_gib=logical_gib,live_plus_history_a_tib=round(a_gib/1024,2),
                    full_git_b_tib=round(b_gib/1024,2),reserved_a_tib=round(a_reserved/1024,2),
                    reserved_b_tib=round(b_reserved/1024,2),
                    hetzner_nodes=a_nodes,ovh_nodes=b_nodes,
                    net_revenue_eur=round(net_revenue,2),
                    monthly_cost_eur=round(cost,2),
                    profit_eur=round(net_revenue-cost,2),
                    net_margin=round((net_revenue-cost)/net_revenue,3),
                    warm_failover=warm_failover)

if __name__ == "__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--mini",type=int,default=100)
    p.add_argument("--plus",type=int,default=300)
    p.add_argument("--pro",type=int,default=100)
    p.add_argument("--warm-failover",action="store_true")
    p.add_argument("--annual-share",type=float,default=0.0)
    p.add_argument("--history-multiplier",type=float,default=2.0)
    p.add_argument("--hetzner-node-eur",type=float,default=132)
    p.add_argument("--ovh-node-eur",type=float,default=190)
    a=p.parse_args()
    r=FullGitCosts(history_multiplier=a.history_multiplier,
                   hetzner_node_eur=a.hetzner_node_eur,
                   ovh_node_eur=a.ovh_node_eur).estimate(
        a.mini,a.plus,a.pro,annual_share=a.annual_share,warm_failover=a.warm_failover)
    print("ILLUSTRATIVE COSTS ONLY: EU provider HDD capacity/prices and failover not verified")
    for key,value in r.items():print(f"{key}: {value}")

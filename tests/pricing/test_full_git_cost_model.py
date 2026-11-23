import importlib.util
from pathlib import Path
import unittest,sys
source=Path(__file__).resolve().parents[2]/"scripts"/"full_git_cost_model.py"
spec=importlib.util.spec_from_file_location("full_git_cost_model",source)
model=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=model
spec.loader.exec_module(model)

class FullGitEconomicsTests(unittest.TestCase):
    def test_both_full_git_copies_are_counted(self):
        r=model.FullGitCosts().estimate(100,300,100)
        self.assertGreater(r["live_plus_history_a_tib"],r["full_git_b_tib"])
        self.assertGreater(r["hetzner_nodes"],1)
        self.assertGreater(r["ovh_nodes"],1)
        self.assertGreater(r["source_gib"],50000)

    def test_warm_failover_costs_more_than_cold(self):
        e=model.FullGitCosts()
        cold=e.estimate(100,300,100)
        warm=e.estimate(100,300,100,warm_failover=True)
        self.assertGreaterEqual(warm["hetzner_nodes"],cold["hetzner_nodes"])
        self.assertGreater(warm["ovh_nodes"],cold["ovh_nodes"])
        self.assertLess(warm["net_margin"],cold["net_margin"])

    def test_more_histories_and_annual_discounts_reduce_margin(self):
        regular=model.FullGitCosts().estimate(100,300,100)
        higher=model.FullGitCosts(history_multiplier=3).estimate(100,300,100)
        annual=model.FullGitCosts().estimate(100,300,100,annual_share=1)
        self.assertLess(higher["profit_eur"],regular["profit_eur"])
        self.assertLess(annual["net_revenue_eur"],regular["net_revenue_eur"])

    def test_invalid_ratio_rejected(self):
        with self.assertRaises(ValueError):
            model.FullGitCosts(history_multiplier=0).estimate(10,10,10)

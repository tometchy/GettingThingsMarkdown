import importlib.util
from pathlib import Path
import unittest, sys
source = Path(__file__).resolve().parents[2] / "scripts" / "full_git_cost_model.py"
spec = importlib.util.spec_from_file_location("full_git_cost_model", source)
model = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = model
spec.loader.exec_module(model)

class FullGitEconomicsTests(unittest.TestCase):
    def test_max_counts_history_twice_and_working_tree_once(self):
        r = model.FullGitCosts(extra_storage_fraction=0, rebuild_capacity_fraction=0).estimate("max")
        self.assertEqual(r["a_data_tib"], 4)
        self.assertEqual(r["b_data_tib"], 2)
        self.assertAlmostEqual(r["a_reserved_tib"]+r["b_reserved_tib"], 8)
    def test_all_plans_and_missing_annual_price_fail_closed(self):
        m = model.FullGitCosts()
        self.assertEqual(set(m.full_capacity_gate()["plans"]), {"basic","plus","pro","max"})
        self.assertFalse(m.full_capacity_gate(True)["passed"])
        self.assertEqual(m.estimate("max", annual=True)["status"], "price_not_set")
    def test_retention_and_store_fee_reduce_margin(self):
        base = model.FullGitCosts().estimate("max")
        expensive = model.FullGitCosts(extra_storage_fraction=.5, payment_fraction=.3).estimate("max")
        self.assertLess(expensive["net_margin"], base["net_margin"])
        self.assertLess(model.FullGitCosts().estimate("plus", annual=True)["net_revenue_eur"],
                        model.FullGitCosts().estimate("plus")["net_revenue_eur"])
    def test_price_floor(self):
        e = model.FullGitCosts()
        floor = e.estimate("max")["gross_usd_month_for_target_margin"]
        self.assertAlmostEqual(model.FullGitCosts(max_month_usd=floor).estimate("max")["net_margin"], e.target_margin)
    def test_invalid_inputs(self):
        with self.assertRaises(ValueError):
            model.FullGitCosts(pool_utilization=0).estimate("max")
        with self.assertRaises(ValueError):
            model.FullGitCosts().estimate("max", fill=1.1)

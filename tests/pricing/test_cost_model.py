import importlib.util
from pathlib import Path
import unittest

source = Path(__file__).resolve().parents[2] / "scripts" / "cost_model.py"
spec = importlib.util.spec_from_file_location("cost_model", source)
model = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = model
spec.loader.exec_module(model)


class TestUnitEconomics(unittest.TestCase):
    def test_four_euro_plan_fails_for_full_200gib(self):
        economics = model.StorageEconomics()
        self.assertLess(economics.model(200, 4.00)["profit_eur"], 0)

    def test_recommended_prices_have_positive_margin_in_baseline(self):
        economics = model.StorageEconomics()
        for gb, price in [(15, 3.99), (200, 9.99), (1024, 39.99)]:
            self.assertGreaterEqual(
                economics.model(gb, price)["margin_on_net_revenue"], .25)

    def test_lower_disk_utilization_and_history_churn_reduce_margin(self):
        good = model.StorageEconomics().model(200, 9.99)
        bad = model.StorageEconomics(utilization=.4,
                    history_multiplier=3.0).model(200, 9.99)
        self.assertLess(bad["profit_eur"], good["profit_eur"])

    def test_floor_rejects_unprofitable_prices(self):
        e = model.StorageEconomics()
        price = e.minimum_gross_price(200)
        self.assertLessEqual(price, 9.99)
        self.assertLess(e.model(200, 4)["profit_eur"], 0)

    def test_cpu_capacity_makes_low_price_unprofitable(self):
        e = model.StorageEconomics()
        fleet = e.fleet(150, 200, 9.99)
        self.assertGreaterEqual(fleet["workers"], 6)
        self.assertLess(fleet["margin"], 0)

    def test_candidate_price_requires_sufficient_customer_scale(self):
        e = model.StorageEconomics()
        self.assertGreaterEqual(e.fleet(150, 200, 15.99)["margin"], .25)
        self.assertLess(e.fleet(10, 200, 15.99)["margin"], 0)

    def test_more_cpu_reduces_density_and_margin(self):
        baseline = model.StorageEconomics()
        heavy = model.StorageEconomics(cpu_request_per_pod_cores=.20)
        self.assertGreater(heavy.fleet(150, 200, 15.99)["workers"],
                           baseline.fleet(150, 200, 15.99)["workers"])
        self.assertLess(heavy.fleet(150, 200, 15.99)["margin"],
                        baseline.fleet(150, 200, 15.99)["margin"])

    def test_three_tier_blend_not_profitable_at_tiny_scale(self):
        e = model.StorageEconomics()
        tiny = e.mixed_fleet(10, 30, 10)
        self.assertLess(tiny["profit_eur"], 0)

    def test_three_tier_blend_contribution_positive_but_not_guaranteed(self):
        e = model.StorageEconomics()
        spread = e.mixed_fleet(100, 300, 100)
        self.assertGreater(spread["profit_eur"], 0)
        self.assertLess(spread["margin"], .30)
        self.assertEqual(spread["nodes"], 8)

    def test_annual_discount_does_not_improve_revenue(self):
        e = model.StorageEconomics()
        monthly = e.mixed_fleet(100, 300, 100, annual_share=0)
        yearly = e.mixed_fleet(100, 300, 100, annual_share=1)
        self.assertLess(yearly["net_revenue_eur"], monthly["net_revenue_eur"])

    def test_expensive_storage_churn_worsens_blend(self):
        e1 = model.StorageEconomics()
        e2 = model.StorageEconomics(history_multiplier=3, utilization=.4)
        self.assertLess(e2.mixed_fleet(100, 300, 100)["margin"],
                        e1.mixed_fleet(100, 300, 100)["margin"])

    def test_invalid_assumptions_fail(self):
        with self.assertRaises(ValueError):
            model.StorageEconomics(utilization=0).model(200, 9.99)


if __name__ == "__main__":
    unittest.main()

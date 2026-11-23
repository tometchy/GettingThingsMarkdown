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

    def test_invalid_assumptions_fail(self):
        with self.assertRaises(ValueError):
            model.StorageEconomics(utilization=0).model(200, 9.99)


if __name__ == "__main__":
    unittest.main()

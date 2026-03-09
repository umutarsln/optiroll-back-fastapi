"""
Optimizasyon sonuçlarının sayısal tutarlılık testleri.

solve_optimization çıktısının sunuma uygun olduğunu doğrular:
- Rulo bazında: used + stock + fire = totalTonnage
- Özet: totalFire, totalStock, openedRolls roll_status ile uyumlu
- 0,5 ton kuralı: kalan > 0,5 ton ise stok, aksi halde fire
- Kesim planı: sipariş bazlı tonaj toplamları talep ile tutarlı
"""
import unittest
import sys
import os

# backend modüllerini import edebilmek için
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from optimizer import (
    solve_optimization,
    calculate_demand,
    MIN_STOCK_THRESHOLD_TON,
)


def _make_orders(m2_list, panel_width=1.0, panel_length=1.0):
    """m² listesinden orders listesi üretir."""
    return [
        {"m2": m2, "panelWidth": panel_width, "panelLength": panel_length}
        for m2 in m2_list
    ]


class TestOptimizationConsistency(unittest.TestCase):
    """Optimizasyon çıktı tutarlılığı: sayılar sunumla uyumlu olmalı."""

    def test_solve_returns_optimal_and_results(self):
        """Çözüm Optimal ise results dolu dönmeli."""
        orders = _make_orders([100, 50], panel_width=1.0)
        rolls = [15, 10, 8]
        status, results = solve_optimization(
            thickness=0.75,
            density=7.85,
            orders=orders,
            panel_widths=[1.0, 1.0],
            rolls=rolls,
            max_orders_per_roll=5,
            max_rolls_per_order=3,
            fire_cost=450,
            setup_cost=120,
            stock_cost=2.5,
            time_limit_seconds=30,
        )
        self.assertEqual(status, "Optimal", "Durum Optimal olmalı")
        self.assertIsNotNone(results, "Sonuç dolu olmalı")
        self.assertIn("rollStatus", results)
        self.assertIn("summary", results)
        self.assertIn("cuttingPlan", results)

    def test_roll_status_used_plus_stock_plus_fire_equals_total_tonnage(self):
        """Her ruloda: used + stock + fire = totalTonnage (sunum tutarlılığı)."""
        orders = _make_orders([80, 60], panel_width=1.0)
        rolls = [20, 15, 10]
        status, results = solve_optimization(
            thickness=0.75,
            density=7.85,
            orders=orders,
            panel_widths=[1.0, 1.0],
            rolls=rolls,
            max_orders_per_roll=5,
            max_rolls_per_order=3,
            fire_cost=450,
            setup_cost=120,
            stock_cost=2.5,
            time_limit_seconds=30,
        )
        self.assertEqual(status, "Optimal")
        roll_status = results["rollStatus"]
        for item in roll_status:
            total = float(item["totalTonnage"])
            used = float(item["used"])
            stock = float(item["stock"])
            fire = float(item["fire"])
            toplam_kalan = stock + fire
            self.assertAlmostEqual(
                used + toplam_kalan,
                total,
                places=4,
                msg=f"Rulo {item['rollId']}: used+stock+fire={used}+{stock}+{fire}={used + toplam_kalan} != totalTonnage={total}",
            )

    def test_summary_totals_match_roll_status(self):
        """summary.totalFire ve totalStock, roll_status toplamlarına eşit olmalı."""
        orders = _make_orders([50, 40], panel_width=1.0)
        rolls = [15, 12, 10]
        status, results = solve_optimization(
            thickness=0.75,
            density=7.85,
            orders=orders,
            panel_widths=[1.0, 1.0],
            rolls=rolls,
            max_orders_per_roll=5,
            max_rolls_per_order=3,
            fire_cost=450,
            setup_cost=120,
            stock_cost=2.5,
            time_limit_seconds=30,
        )
        self.assertEqual(status, "Optimal")
        roll_status = results["rollStatus"]
        summary = results["summary"]
        sum_fire = sum(float(r["fire"]) for r in roll_status)
        sum_stock = sum(float(r["stock"]) for r in roll_status)
        self.assertAlmostEqual(
            summary["totalFire"],
            sum_fire,
            places=4,
            msg="summary.totalFire roll_status fire toplamına eşit olmalı",
        )
        self.assertAlmostEqual(
            summary["totalStock"],
            sum_stock,
            places=4,
            msg="summary.totalStock roll_status stock toplamına eşit olmalı",
        )

    def test_half_ton_rule_above_is_stock_below_is_fire(self):
        """0,5 ton üstü kalan stoğa, 0,5 ve altı fire sayılmalı."""
        orders = _make_orders([30, 25], panel_width=1.0)
        rolls = [10, 10, 10]
        status, results = solve_optimization(
            thickness=0.75,
            density=7.85,
            orders=orders,
            panel_widths=[1.0, 1.0],
            rolls=rolls,
            max_orders_per_roll=5,
            max_rolls_per_order=3,
            fire_cost=450,
            setup_cost=120,
            stock_cost=2.5,
            time_limit_seconds=30,
        )
        self.assertEqual(status, "Optimal")
        for item in results["rollStatus"]:
            stock = float(item["stock"])
            fire = float(item["fire"])
            kalan = stock + fire
            if kalan > MIN_STOCK_THRESHOLD_TON:
                self.assertGreater(stock, 0, f"Rulo {item['rollId']}: kalan > 0.5 ise stock > 0 olmalı")
                self.assertAlmostEqual(fire, 0.0, places=4, msg=f"Rulo {item['rollId']}: kalan > 0.5 ise fire=0 olmalı")
            elif kalan > 0:
                self.assertAlmostEqual(stock, 0.0, places=4, msg=f"Rulo {item['rollId']}: kalan <= 0.5 ise stock=0 olmalı")
                self.assertGreater(fire, 0, f"Rulo {item['rollId']}: kalan <= 0.5 ise fire > 0 olmalı")
                self.assertLessEqual(fire, MIN_STOCK_THRESHOLD_TON + 0.01, msg="Fire en fazla ~0.5 ton olmalı")

    def test_cutting_plan_tonnage_per_order_near_demand(self):
        """Kesim planında sipariş bazlı tonaj toplamı, talep (D) ile tutarlı olmalı."""
        orders = _make_orders([60, 40], panel_width=1.0)
        panel_widths = [1.0, 1.0]
        panel_lengths = [1.0, 1.0]
        D, _ = calculate_demand(
            orders,
            thickness=0.75,
            density=7.85,
            panel_widths=panel_widths,
            panel_lengths=panel_lengths,
        )
        rolls = [15, 12, 10]
        status, results = solve_optimization(
            thickness=0.75,
            density=7.85,
            orders=orders,
            panel_widths=panel_widths,
            rolls=rolls,
            max_orders_per_roll=5,
            max_rolls_per_order=3,
            fire_cost=450,
            setup_cost=120,
            stock_cost=2.5,
            time_limit_seconds=30,
            panel_lengths=panel_lengths,
        )
        self.assertEqual(status, "Optimal")
        cutting_plan = results["cuttingPlan"]
        # orderId 1-based; sipariş j için tonaj toplamı
        ton_per_order = {}
        for c in cutting_plan:
            oid = int(c.get("orderId", 0))
            ton_per_order[oid] = ton_per_order.get(oid, 0) + float(c.get("tonnage", 0))
        for j, demand_j in D.items():
            oid = j + 1
            plan_ton = ton_per_order.get(oid, 0)
            self.assertAlmostEqual(
                plan_ton,
                demand_j,
                places=2,
                msg=f"Sipariş {oid}: kesim planı tonajı {plan_ton} talep {demand_j} ile uyumlu olmalı",
            )

    def test_opened_rolls_consistent_with_roll_status(self):
        """summary.openedRolls, kullanılan (used > 0) rulo sayısı ile tutarlı olmalı."""
        orders = _make_orders([100, 80], panel_width=1.0)
        rolls = [25, 20, 15, 10]
        status, results = solve_optimization(
            thickness=0.75,
            density=7.85,
            orders=orders,
            panel_widths=[1.0, 1.0],
            rolls=rolls,
            max_orders_per_roll=5,
            max_rolls_per_order=3,
            fire_cost=450,
            setup_cost=120,
            stock_cost=2.5,
            time_limit_seconds=30,
        )
        self.assertEqual(status, "Optimal")
        roll_status = results["rollStatus"]
        opened = results["summary"]["openedRolls"]
        used_count = sum(1 for r in roll_status if float(r.get("used", 0) or 0) > 0.0001)
        self.assertEqual(opened, used_count, "openedRolls kullanılan rulo sayısına eşit olmalı")


if __name__ == "__main__":
    unittest.main()

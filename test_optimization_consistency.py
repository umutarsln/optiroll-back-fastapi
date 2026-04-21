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
    calculate_return_gap_penalty,
    build_roll_order_sequence,
    apply_sequence_local_improvement,
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

    def test_roll_status_tonnage_fields_on_kilogram_grid(self):
        """Rulo ton alanları 1 kg (0,001 t) ızgarasında olmalı (raporlama kg defteri)."""
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
        for item in results["rollStatus"]:
            for key in (
                "totalTonnage",
                "used",
                "fire",
                "stock",
                "unusedRollTonnage",
                "remaining",
            ):
                v = float(item.get(key, 0) or 0)
                kg = round(v * 1000.0)
                self.assertAlmostEqual(
                    v,
                    kg / 1000.0,
                    places=9,
                    msg=f"Rulo {item['rollId']} {key}={v} 1 kg ızgarasında değil",
                )

    def test_roll_status_used_plus_stock_plus_fire_equals_total_tonnage(self):
        """Her ruloda: used + stock + fire + unusedRollTonnage = totalTonnage (sunum tutarlılığı)."""
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
            unused = float(item.get("unusedRollTonnage", 0) or 0)
            self.assertAlmostEqual(
                used + stock + fire + unused,
                total,
                places=4,
                msg=f"Rulo {item['rollId']}: used+stock+fire+eldeki={used}+{stock}+{fire}+{unused} != totalTonnage={total}",
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
        sum_unused = sum(float(r.get("unusedRollTonnage", 0) or 0) for r in roll_status)
        self.assertAlmostEqual(
            float(summary.get("totalUnusedInventoryTon", 0) or 0),
            sum_unused,
            places=4,
            msg="summary.totalUnusedInventoryTon roll_status eldeki toplamına eşit olmalı",
        )

    def test_summary_cost_components_sum_to_total_cost(self):
        """costFireLira + costStockLira + costSetupLira + costSequencePenaltyLira ≈ totalCost."""
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
            fire_cost=100,
            setup_cost=100,
            stock_cost=100,
            time_limit_seconds=30,
        )
        self.assertEqual(status, "Optimal")
        s = results["summary"]
        parts = (
            float(s.get("costFireLira", 0) or 0)
            + float(s.get("costStockLira", 0) or 0)
            + float(s.get("costSetupLira", 0) or 0)
            + float(s.get("costSequencePenaltyLira", 0) or 0)
        )
        self.assertAlmostEqual(
            parts,
            float(s.get("totalCost", 0) or 0),
            places=1,
            msg="Özet maliyet kırılımı totalCost ile uyumlu olmalı",
        )

    def test_dual_surface_prefers_tighter_roll_pair_when_stock_cost_tie(self):
        """
        Dar (5,89 t) ve geniş (6 t) çiftleri varken stok maliyeti toplamı (kullanılmayan R=S) beraberlik
        verebilir; indeks beraberlik kırıcı ile önce listelenen dar çift seçilmeli ve rapor firesi düşük kalmalı.
        """
        orders = _make_orders([1000], panel_width=1.0, panel_length=1.0)
        rolls = [5.89, 5.89, 6.0, 6.0]
        status, results = solve_optimization(
            thickness=0.75,
            density=7.85,
            orders=orders,
            panel_widths=[1.0],
            rolls=rolls,
            max_orders_per_roll=5,
            max_rolls_per_order=8,
            fire_cost=10000,
            setup_cost=120,
            stock_cost=1,
            time_limit_seconds=120,
            surface_factor=2,
        )
        self.assertEqual(status, "Optimal")
        used_roll_ids = sorted({int(c["rollId"]) for c in results["cuttingPlan"]})
        self.assertEqual(
            used_roll_ids,
            [1, 2],
            msg="Stok maliyeti beraberliğinde dar bobin çifti (Rulo #1 ve #2) seçilmeli",
        )
        self.assertLess(
            float(results["summary"]["totalFire"]),
            0.02,
            msg="Dar bobinlerde kesim sonrası rapor firesi 6 t çiftine göre çok daha düşük olmalı",
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

    def test_calculate_demand_surface_factor_doubles_tonnage(self):
        """surface_factor=2 oldugunda talep tonaji, surface_factor=1'e gore iki kat olmali."""
        orders = _make_orders([120, 80], panel_width=1.0, panel_length=2.0)
        panel_widths = [1.0, 1.0]
        panel_lengths = [2.0, 2.0]
        demand_single, total_single = calculate_demand(
            orders=orders,
            thickness=0.75,
            density=7.85,
            panel_widths=panel_widths,
            panel_lengths=panel_lengths,
            surface_factor=1.0,
        )
        demand_double, total_double = calculate_demand(
            orders=orders,
            thickness=0.75,
            density=7.85,
            panel_widths=panel_widths,
            panel_lengths=panel_lengths,
            surface_factor=2.0,
        )
        self.assertAlmostEqual(total_double, total_single * 2, places=3)
        for j in demand_single:
            self.assertAlmostEqual(demand_double[j], demand_single[j] * 2, places=3)

    def test_return_gap_penalty_excess_over_max_interleaving(self):
        """Araya fazla siparis girdiginde ceza ve ihlal listesi uretilmeli."""
        seq = {1: [1, 2, 3, 4, 1]}
        penalty, violations = calculate_return_gap_penalty(seq, max_interleaving=2, penalty_per_excess=10.0)
        self.assertGreater(penalty, 0)
        self.assertTrue(any(v.get("excess", 0) > 0 for v in violations))

    def test_return_gap_penalty_zero_cost_still_records_violations(self):
        """Ceza birimi 0 iken parasal ceza 0, ihlal kaydi tutulabilir."""
        seq = {1: [1, 2, 3, 4, 1]}
        penalty, violations = calculate_return_gap_penalty(seq, max_interleaving=2, penalty_per_excess=0.0)
        self.assertAlmostEqual(penalty, 0.0, places=4)
        self.assertGreater(len(violations), 0)

    def test_solve_includes_sequence_metadata(self):
        """solve_optimization ciktisinda sıra cezasi alanlari bulunmali."""
        orders = _make_orders([50, 40], panel_width=1.0)
        status, results = solve_optimization(
            thickness=0.75,
            density=7.85,
            orders=orders,
            panel_widths=[1.0, 1.0],
            rolls=[15, 12, 10],
            max_orders_per_roll=5,
            max_rolls_per_order=3,
            fire_cost=450,
            setup_cost=120,
            stock_cost=2.5,
            time_limit_seconds=30,
            max_interleaving_orders=2,
            interleaving_penalty_cost=0.0,
        )
        self.assertEqual(status, "Optimal")
        self.assertIn("sequencePenalty", results)
        self.assertIn("sequenceViolations", results)
        self.assertIn("rollOrderSequences", results)
        self.assertIn("sequencePenalty", results["summary"])
        self.assertIn("interleavingViolationCount", results["summary"])

    def test_build_roll_order_sequence_from_plan(self):
        """Kesim plani satir sirasindan rulo siparis dizisi cikarilmali."""
        plan = [
            {"rollId": 1, "orderId": 2, "tonnage": 1},
            {"rollId": 1, "orderId": 1, "tonnage": 1},
            {"rollId": 2, "orderId": 1, "tonnage": 1},
        ]
        seq = build_roll_order_sequence(plan)
        self.assertEqual(seq[1], [2, 1])
        self.assertEqual(seq[2], [1])

    def test_return_gap_penalty_exactly_two_distinct_interleaved_no_penalty(self):
        """Tam 2 farklı araya sipariş (max=2) iken parasal ceza ve ihlal olmamalı."""
        seq = {1: [1, 2, 3, 1]}
        penalty, violations = calculate_return_gap_penalty(
            seq, max_interleaving=2, penalty_per_excess=10.0
        )
        self.assertAlmostEqual(penalty, 0.0, places=4)
        self.assertEqual(len(violations), 0)

    def test_return_gap_penalty_three_distinct_interleaved_triggers(self):
        """3 farklı araya sipariş (max=2) iken en az bir ihlal ve pozitif ceza."""
        seq = {1: [1, 2, 3, 4, 1]}
        penalty, violations = calculate_return_gap_penalty(
            seq, max_interleaving=2, penalty_per_excess=10.0
        )
        self.assertGreater(penalty, 0)
        self.assertGreaterEqual(len(violations), 1)
        self.assertEqual(penalty, 10.0)

    def test_build_roll_order_sequence_duplicate_order_same_roll(self):
        """Aynı ruloda aynı sipariş birden fazla satırda tekrarlanırsa sıra korunur (çoklu segment)."""
        plan = [
            {"rollId": 1, "orderId": 1, "tonnage": 0.5},
            {"rollId": 1, "orderId": 2, "tonnage": 0.5},
            {"rollId": 1, "orderId": 3, "tonnage": 0.5},
            {"rollId": 1, "orderId": 4, "tonnage": 0.5},
            {"rollId": 1, "orderId": 1, "tonnage": 0.5},
        ]
        seq = build_roll_order_sequence(plan)
        self.assertEqual(seq[1], [1, 2, 3, 4, 1])

    def test_apply_sequence_local_improvement_never_increases_penalty(self):
        """Yerel sıra iyileştirmesi, rulolar üzerinde toplam sıra cezasını artırmaz."""
        plan = [
            {"rollId": 1, "orderId": 1, "tonnage": 1},
            {"rollId": 1, "orderId": 2, "tonnage": 1},
            {"rollId": 1, "orderId": 3, "tonnage": 1},
            {"rollId": 1, "orderId": 4, "tonnage": 1},
            {"rollId": 1, "orderId": 1, "tonnage": 1},
            {"rollId": 2, "orderId": 5, "tonnage": 1},
            {"rollId": 2, "orderId": 1, "tonnage": 1},
            {"rollId": 2, "orderId": 2, "tonnage": 1},
            {"rollId": 2, "orderId": 3, "tonnage": 1},
            {"rollId": 2, "orderId": 4, "tonnage": 1},
            {"rollId": 2, "orderId": 1, "tonnage": 1},
        ]
        max_int, unit = 2, 25.0
        seq_before = build_roll_order_sequence(plan)
        pen_before, _ = calculate_return_gap_penalty(seq_before, max_int, unit)
        improved, _ = apply_sequence_local_improvement(plan, max_int, unit)
        seq_after = build_roll_order_sequence(improved)
        pen_after, _ = calculate_return_gap_penalty(seq_after, max_int, unit)
        self.assertLessEqual(pen_after, pen_before)

    def test_dual_roll_with_surface_factor_two_demand_and_min_two_rolls(self):
        """surface_factor=2: talep 2x, kesim toplamı D, üst/alt yüzey tonajı modelde tam D/2, en az 2 rulo/sipariş."""
        orders = _make_orders([80, 70], panel_width=1.0, panel_length=1.0)
        panel_widths = [1.0, 1.0]
        panel_lengths = [1.0, 1.0]
        demand, _ = calculate_demand(
            orders=orders,
            thickness=0.75,
            density=7.85,
            panel_widths=panel_widths,
            panel_lengths=panel_lengths,
            surface_factor=2.0,
        )
        status, results = solve_optimization(
            thickness=0.75,
            density=7.85,
            orders=orders,
            panel_widths=panel_widths,
            panel_lengths=panel_lengths,
            rolls=[10, 10, 10, 10, 10],
            max_orders_per_roll=5,
            max_rolls_per_order=5,
            fire_cost=450,
            setup_cost=120,
            stock_cost=2.5,
            time_limit_seconds=30,
            surface_factor=2.0,
        )
        self.assertEqual(status, "Optimal")
        cutting_plan = results["cuttingPlan"]
        per_order_rolls = {}
        for item in cutting_plan:
            j = int(item["orderId"]) - 1
            per_order_rolls.setdefault(j, set()).add(int(item["rollId"]))
        ton_per_order = {}
        for c in cutting_plan:
            oid = int(c["orderId"])
            ton_per_order[oid] = ton_per_order.get(oid, 0) + float(c["tonnage"])
        for j, demand_j in demand.items():
            self.assertAlmostEqual(
                ton_per_order[j + 1],
                demand_j,
                places=2,
                msg="Çift yüzey talebi kesim tonajı ile örtüşmeli",
            )
        for j, demand_j in demand.items():
            oid = j + 1
            half = demand_j / 2.0
            upper_sum = 0.0
            lower_sum = 0.0
            for c in cutting_plan:
                if int(c["orderId"]) != oid:
                    continue
                upper_sum += float(c.get("upperTonnage", 0))
                lower_sum += float(c.get("lowerTonnage", 0))
            self.assertAlmostEqual(
                upper_sum,
                half,
                places=2,
                msg=f"Sipariş {oid}: üst yüzey tonajı toplamı D/2 olmalı",
            )
            self.assertAlmostEqual(
                lower_sum,
                half,
                places=2,
                msg=f"Sipariş {oid}: alt yüzey tonajı toplamı D/2 olmalı",
            )
        for j in demand:
            self.assertGreaterEqual(
                len(per_order_rolls.get(j, set())),
                2,
                msg=f"Sipariş {j+1} en az 2 ruloda görünmeli",
            )

    def test_dual_surface_same_physical_roll_not_upper_and_lower_same_order(self):
        """
        Çift yüzeyde aynı fiziksel rulo, aynı sipariş için üst ve alt yüzeye aynı anda atanamaz;
        kesim satırında üst ve alt tonaj birlikte pozitif olmamalı.
        """
        orders = _make_orders([80, 70], panel_width=1.0, panel_length=1.0)
        panel_widths = [1.0, 1.0]
        panel_lengths = [1.0, 1.0]
        status, results = solve_optimization(
            thickness=0.75,
            density=7.85,
            orders=orders,
            panel_widths=panel_widths,
            panel_lengths=panel_lengths,
            rolls=[10, 10, 10, 10, 10],
            max_orders_per_roll=5,
            max_rolls_per_order=5,
            fire_cost=450,
            setup_cost=120,
            stock_cost=2.5,
            time_limit_seconds=30,
            surface_factor=2.0,
        )
        self.assertEqual(status, "Optimal")
        eps = 1e-3
        for item in results["cuttingPlan"]:
            u = float(item.get("upperTonnage", 0) or 0)
            l = float(item.get("lowerTonnage", 0) or 0)
            self.assertFalse(
                u > eps and l > eps,
                msg=(
                    f"Rulo {item['rollId']} · Sipariş {item['orderId']}: "
                    f"aynı rulo aynı siparişte hem üst ({u}) hem alt ({l}) olamaz"
                ),
            )

    def test_surface_factor_two_small_orders_still_feasible(self):
        """Düşük m² siparişlerde surface_factor=2 ve min iki rulo ile çözüm bulunabilmeli."""
        orders = _make_orders([30, 25], panel_width=1.0, panel_length=1.0)
        panel_widths = [1.0, 1.0]
        panel_lengths = [1.0, 1.0]
        status, results = solve_optimization(
            thickness=0.75,
            density=7.85,
            orders=orders,
            panel_widths=panel_widths,
            panel_lengths=panel_lengths,
            rolls=[12, 12, 12, 12],
            max_orders_per_roll=4,
            max_rolls_per_order=4,
            fire_cost=450,
            setup_cost=120,
            stock_cost=2.5,
            time_limit_seconds=30,
            surface_factor=2.0,
        )
        self.assertEqual(status, "Optimal")
        self.assertIsNotNone(results)

    def test_roll_orders_used_respects_max_orders_per_roll(self):
        """LP kısıtı: kullanılan her ruloda distinct sipariş sayısı max_orders_per_roll altında."""
        orders = _make_orders([90, 85, 80, 75], panel_width=1.0)
        rolls = [12, 12, 12, 12, 12, 12]
        max_opr = 3
        status, results = solve_optimization(
            thickness=0.75,
            density=7.85,
            orders=orders,
            panel_widths=[1.0, 1.0, 1.0, 1.0],
            rolls=rolls,
            max_orders_per_roll=max_opr,
            max_rolls_per_order=6,
            fire_cost=450,
            setup_cost=120,
            stock_cost=2.5,
            time_limit_seconds=45,
        )
        self.assertEqual(status, "Optimal")
        cutting_plan = results["cuttingPlan"]
        per_roll_orders = {}
        for c in cutting_plan:
            rid = int(c["rollId"])
            oid = int(c["orderId"])
            per_roll_orders.setdefault(rid, set()).add(oid)
        for rid, oset in per_roll_orders.items():
            self.assertLessEqual(
                len(oset),
                max_opr,
                msg=f"Rulo {rid}: en fazla {max_opr} sipariş olmalı",
            )
        for item in results["rollStatus"]:
            if float(item.get("used", 0) or 0) <= 0.0001:
                continue
            self.assertLessEqual(
                int(item.get("ordersUsed", 0)),
                max_opr,
                msg=f"roll_status.ordersUsed rulo {item['rollId']}",
            )

    def test_summary_total_cost_matches_fire_stock_setup_sequence(self):
        """summary.totalCost, TL kırılım satırlarının toplamına eşit (fire + stok tutma + kurulum + sıra cezası)."""
        orders = _make_orders([55, 45], panel_width=1.0)
        status, results = solve_optimization(
            thickness=0.75,
            density=7.85,
            orders=orders,
            panel_widths=[1.0, 1.0],
            rolls=[14, 12, 10],
            max_orders_per_roll=5,
            max_rolls_per_order=3,
            fire_cost=450.0,
            setup_cost=120.0,
            stock_cost=2.5,
            time_limit_seconds=30,
            max_interleaving_orders=2,
            interleaving_penalty_cost=50.0,
        )
        self.assertEqual(status, "Optimal")
        s = results["summary"]
        expected = (
            float(s["costFireLira"])
            + float(s["costStockLira"])
            + float(s["costSetupLira"])
            + float(s["costSequencePenaltyLira"])
        )
        self.assertAlmostEqual(float(s["totalCost"]), expected, places=1)

    def test_stock_holding_cost_includes_shelf_unused_tonnage(self):
        """costStockLira, üretim stoğu + rafta elde ton için h×(totalStock + totalUnused) ile uyumlu olmalı."""
        orders = _make_orders([55, 45], panel_width=1.0)
        h = 2.5
        status, results = solve_optimization(
            thickness=0.75,
            density=7.85,
            orders=orders,
            panel_widths=[1.0, 1.0],
            rolls=[14, 12, 10],
            max_orders_per_roll=5,
            max_rolls_per_order=3,
            fire_cost=450.0,
            setup_cost=120.0,
            stock_cost=h,
            time_limit_seconds=30,
        )
        self.assertEqual(status, "Optimal")
        s = results["summary"]
        ton_holding = float(s["totalStock"]) + float(s.get("totalUnusedInventoryTon", 0) or 0)
        self.assertAlmostEqual(
            float(s.get("totalStockHoldingTon", 0) or 0),
            ton_holding,
            places=4,
            msg="totalStockHoldingTon = totalStock + totalUnusedInventoryTon",
        )
        self.assertAlmostEqual(
            float(s["costStockLira"]),
            round(ton_holding * h, 2),
            places=1,
            msg="Stok tutma TL, h × (üretim stoku + elde) olmalı",
        )

    def test_standard_lp_cutting_plan_no_repeat_order_per_roll_zero_interleaving(self):
        """Tek (rulo,sipariş) satırı modelinde aynı ruloda sipariş tekrarı yok; sıra ihlali beklenmez."""
        orders = _make_orders([70, 65, 60], panel_width=1.0)
        status, results = solve_optimization(
            thickness=0.75,
            density=7.85,
            orders=orders,
            panel_widths=[1.0, 1.0, 1.0],
            rolls=[15, 14, 13, 12],
            max_orders_per_roll=5,
            max_rolls_per_order=4,
            fire_cost=450,
            setup_cost=120,
            stock_cost=2.5,
            time_limit_seconds=45,
            max_interleaving_orders=2,
            interleaving_penalty_cost=100.0,
        )
        self.assertEqual(status, "Optimal")
        self.assertAlmostEqual(float(results["sequencePenalty"]), 0.0, places=4)
        self.assertEqual(len(results["sequenceViolations"]), 0)
        self.assertEqual(int(results["summary"]["interleavingViolationCount"]), 0)
        by_roll = {}
        for c in results["cuttingPlan"]:
            rid = int(c["rollId"])
            oid = int(c["orderId"])
            by_roll.setdefault(rid, []).append(oid)
        for rid, oids in by_roll.items():
            self.assertEqual(len(oids), len(set(oids)), f"Rulo {rid} tekrarsız sipariş")

    def test_synthetic_multisegment_same_roll_interleaving_penalty_formula(self):
        """Çoklu segment: 1→2→3→4→1 dizisinde max=2 için 1 birim fazlalık cezası (birim fiyat 7)."""
        plan = [
            {"rollId": 1, "orderId": 1, "tonnage": 0.1},
            {"rollId": 1, "orderId": 2, "tonnage": 0.1},
            {"rollId": 1, "orderId": 3, "tonnage": 0.1},
            {"rollId": 1, "orderId": 4, "tonnage": 0.1},
            {"rollId": 1, "orderId": 1, "tonnage": 0.1},
        ]
        seq = build_roll_order_sequence(plan)
        penalty, violations = calculate_return_gap_penalty(
            seq, max_interleaving=2, penalty_per_excess=7.0
        )
        self.assertAlmostEqual(penalty, 7.0, places=4)
        self.assertTrue(any(v["orderId"] == 1 and v["excess"] == 1 for v in violations))


if __name__ == "__main__":
    unittest.main()

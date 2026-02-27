"""
Supabase kayıt akışının analitik tablolar için yazım testleri.
"""
import unittest
from unittest.mock import patch

import supabase_client


class _FakeResponse:
    """Sahte Supabase execute cevabı."""

    def __init__(self, data):
        """Response veri taşıyıcısını başlatır."""
        self.data = data


class _FakeTable:
    """Sahte Supabase tablo işlemleri (insert/execute)."""

    def __init__(self, db, name):
        """Tablo bağlamını başlatır."""
        self._db = db
        self._name = name
        self._payload = None

    def insert(self, payload):
        """Insert payload'ını saklar ve zincirleme çağrı için self döner."""
        self._payload = payload
        return self

    def execute(self):
        """Payload'ı sahte DB'ye yazar ve Supabase benzeri yanıt döner."""
        if self._name == "optimization_runs":
            row = dict(self._payload or {})
            row["id"] = "run-123"
            self._db[self._name].append(row)
            return _FakeResponse([{"id": "run-123"}])

        if isinstance(self._payload, list):
            self._db[self._name].extend(self._payload)
            return _FakeResponse(self._payload)

        self._db[self._name].append(self._payload)
        return _FakeResponse([self._payload])


class _FakeClient:
    """Sahte Supabase client."""

    def __init__(self):
        """Test boyunca kullanılacak bellek içi tabloları başlatır."""
        self.db = {
            "optimization_runs": [],
            "optimization_run_metrics": [],
            "optimization_run_roll_status": [],
            "optimization_run_cutting_plan": [],
        }

    def table(self, name):
        """Tablo adı için sahte tablo nesnesi döner."""
        return _FakeTable(self.db, name)


class SupabaseAnalyticsPersistenceTest(unittest.TestCase):
    """Analitik tablo yazımlarını doğrulayan testler."""

    def test_save_optimization_result_writes_analytics_tables(self):
        """Ana kayıt, metrik, rulo ve kesim kırılımı tablolarına yazım yapılmalıdır."""
        fake_client = _FakeClient()
        input_data = {
            "material": {"thickness": 0.75, "density": 7.85},
            "orders": [{"m2": 100, "panelWidth": 1.0}, {"m2": 50, "panelWidth": 0.5}],
            "rollSettings": {"rolls": [10, 8], "maxOrdersPerRoll": 8, "maxRollsPerOrder": 5},
            "costs": {"fireCost": 450, "setupCost": 120, "stockCost": 2.5},
        }
        summary = {
            "totalCost": 12500.0,
            "totalFire": 1.4,
            "totalStock": 0.6,
            "openedRolls": 2,
        }
        cutting_plan = [
            {"rollId": 1, "orderId": 1, "panelCount": 100, "panelWidth": 1.0, "tonnage": 5.0, "m2": 100.0},
            {"rollId": 2, "orderId": 2, "panelCount": 100, "panelWidth": 0.5, "tonnage": 4.0, "m2": 50.0},
        ]
        roll_status = [
            {"rollId": 1, "totalTonnage": 10.0, "used": 5.0, "remaining": 5.0, "fire": 1.0, "stock": 4.0, "ordersUsed": 1},
            {"rollId": 2, "totalTonnage": 8.0, "used": 4.0, "remaining": 4.0, "fire": 0.4, "stock": 3.6, "ordersUsed": 1},
        ]

        with patch("supabase_client.get_supabase_client", return_value=fake_client):
            run_id = supabase_client.save_optimization_result(
                file_id="abc123def456",
                input_data=input_data,
                summary=summary,
                cutting_plan=cutting_plan,
                roll_status=roll_status,
            )

        self.assertEqual(run_id, "run-123")
        self.assertEqual(len(fake_client.db["optimization_runs"]), 1)
        self.assertEqual(len(fake_client.db["optimization_run_metrics"]), 1)
        self.assertEqual(len(fake_client.db["optimization_run_roll_status"]), 2)
        self.assertEqual(len(fake_client.db["optimization_run_cutting_plan"]), 2)

        metrics = fake_client.db["optimization_run_metrics"][0]
        self.assertEqual(metrics["status"], "Optimal")
        self.assertEqual(metrics["total_cost"], 12500.0)
        self.assertEqual(metrics["total_tonnage"], 18.0)
        self.assertEqual(metrics["total_used_ton"], 9.0)
        self.assertEqual(metrics["material_usage_pct"], 50.0)
        self.assertAlmostEqual(metrics["fire_pct"], 7.7778, places=4)
        self.assertEqual(metrics["total_panels"], 200)
        self.assertEqual(metrics["total_m2"], 150.0)
        self.assertEqual(metrics["unique_rolls"], 2)


if __name__ == "__main__":
    """Dosya doğrudan çalıştırılırsa unittest giriş noktası."""
    unittest.main()

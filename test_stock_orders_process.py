"""
Stok, sipariş ve optimizasyon sonucu işleme (process_optimization_result) testleri.

Sayısal tutarlılık: işleme alındığında stoktan düşülen rulo sayısı, eklenen kalan rulo sayısı
ve güncellenen sipariş sayısı beklenen verilerle uyumlu olmalı.
"""
import unittest
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# supabase_client import edilmeden önce mock'lanacak
import supabase_client


class _FakeTable:
    """Sahte Supabase tablo: update/delete/select/insert zinciri."""

    def __init__(self, db, name):
        self._db = db
        self._name = name
        self._op = None
        self._payload = None
        self._filter_key = None
        self._filter_val = None

    def insert(self, payload):
        self._op = "insert"
        self._payload = payload
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    def select(self, *args):
        self._op = "select"
        return self

    def eq(self, key, val):
        self._filter_key = key
        self._filter_val = val
        return self

    def execute(self):
        if self._name == "optimization_runs" and self._op == "select":
            return MagicMock(data=self._db.get("_run_row") or [])
        if self._name == "stock_rolls" and self._op == "delete":
            self._db.setdefault("_deleted_stock_rolls", []).append(self._filter_val)
            return MagicMock()
        if self._name == "stock_rolls" and self._op == "insert":
            self._db.setdefault("_inserted_stock_rolls", []).append(self._payload)
            return MagicMock(data=[{"id": "new-id"}])
        if self._name == "orders" and self._op == "update":
            self._db.setdefault("_updated_orders", []).append(self._filter_val)
            return MagicMock()
        if self._name == "optimization_runs" and self._op == "update":
            return MagicMock()
        return MagicMock()


class _FakeClient:
    """Sahte Supabase client; silinen/eklenen/güncellenen kayıtları toplar."""

    def __init__(self):
        self._run_row = None
        self._deleted_stock_rolls = []
        self._inserted_stock_rolls = []
        self._updated_orders = []

    def table(self, name):
        db = {
            "_run_row": self._run_row,
            "_deleted_stock_rolls": self._deleted_stock_rolls,
            "_inserted_stock_rolls": self._inserted_stock_rolls,
            "_updated_orders": self._updated_orders,
        }
        return _FakeTable(db, name)

    def _get_captured(self):
        return {
            "deleted_stock_rolls": list(self._deleted_stock_rolls),
            "inserted_stock_rolls": list(self._inserted_stock_rolls),
            "updated_orders": list(self._updated_orders),
        }


class TestStockOrdersProcessConsistency(unittest.TestCase):
    """process_optimization_result: stok düşme, kalan ekleme, sipariş güncelleme sayısal tutarlılığı."""

    def setUp(self):
        self.fake_client = _FakeClient()

    @patch.object(supabase_client, "get_supabase_client")
    @patch.object(supabase_client, "get_run_by_file_id")
    @patch.object(supabase_client, "delete_stock_roll")
    @patch.object(supabase_client, "add_stock_roll")
    def test_process_result_deletes_used_stock_rolls_and_adds_leftover(
        self, mock_add, mock_delete, mock_get_run, mock_get_client
    ):
        """İşleme alında: stockRollIds kadar rulo silinmeli, stock > 0 olan her rulo için stoka ekleme yapılmalı."""
        file_id = "test-file-16"
        run_id = "run-uuid-123"
        stock_roll_ids = ["roll-a", "roll-b", "roll-c"]
        roll_status = [
            {"rollId": 1, "totalTonnage": 10, "used": 8, "stock": 1.5, "fire": 0.5},
            {"rollId": 2, "totalTonnage": 10, "used": 10, "stock": 0, "fire": 0},
            {"rollId": 3, "totalTonnage": 8, "used": 5, "stock": 3.0, "fire": 0},
        ]
        mock_get_client.return_value = self.fake_client
        mock_get_run.return_value = {
            "id": run_id,
            "file_id": file_id,
            "input_data": {
                "orders": [{"orderId": "ord-1"}, {"orderId": "ord-2"}],
                "stockRollIds": stock_roll_ids,
            },
            "cutting_plan": [{"orderId": 1}, {"orderId": 2}],
            "roll_status": roll_status,
        }
        # delete_stock_roll ve add_stock_roll gerçekten çağrılıyor; capture için side_effect ile listeye ekleyelim
        deleted = []
        inserted = []

        def capture_delete(roll_id):
            deleted.append(roll_id)
            self.fake_client._deleted_stock_rolls.append(roll_id)

        def capture_add(tonnage, source, run_id):
            inserted.append({"tonnage": tonnage, "source": source, "run_id": run_id})
            self.fake_client._inserted_stock_rolls.append({"tonnage": tonnage, "source": source})

        mock_delete.side_effect = capture_delete
        mock_add.return_value = {"id": "new-id"}

        ok = supabase_client.process_optimization_result(file_id)

        self.assertTrue(ok, "İşlem başarılı dönmeli")
        self.assertEqual(len(deleted), 3, "3 stok rulosu silinmeli (stockRollIds)")
        self.assertIn("roll-a", deleted)
        self.assertIn("roll-b", deleted)
        self.assertIn("roll-c", deleted)
        # stock > 0 olan 2 rulo: 1.5 ton ve 3.0 ton
        self.assertEqual(mock_add.call_count, 2, "stock > 0 olan 2 rulo için stoka ekleme yapılmalı")
        tonnages = [c[1]["tonnage"] for c in mock_add.call_args_list]
        self.assertIn(1.5, tonnages)
        self.assertIn(3.0, tonnages)

    @patch.object(supabase_client, "get_supabase_client")
    @patch.object(supabase_client, "get_run_by_file_id")
    @patch.object(supabase_client, "delete_stock_roll")
    @patch.object(supabase_client, "add_stock_roll")
    def test_process_result_updates_orders_in_cutting_plan(
        self, mock_add, mock_delete, mock_get_run, mock_get_client
    ):
        """cutting_plan'da geçen siparişler In Production olarak güncellenmeli (sayı tutarlı)."""
        file_id = "test-file-2"
        run_id = "run-uuid-456"
        # Backend: used_order_indices = {orderId from cutting_plan}; orders_input[idx] ile orderId alınıyor.
        # orderId 1,2 için en az 3 eleman gerekir (idx 1 ve 2 kullanılır).
        orders_input = [
            {"orderId": "ord-0"},
            {"orderId": "ord-1"},
            {"orderId": "ord-2"},
        ]
        cutting_plan = [{"orderId": 1}, {"orderId": 2}]
        mock_table = MagicMock()
        mock_table.update.return_value.eq.return_value.execute.return_value = None
        mock_get_client.return_value = MagicMock(table=lambda name: mock_table)
        mock_get_run.return_value = {
            "id": run_id,
            "file_id": file_id,
            "input_data": {"orders": orders_input, "stockRollIds": []},
            "cutting_plan": cutting_plan,
            "roll_status": [{"rollId": 1, "stock": 0, "fire": 0}],
        }
        mock_delete.return_value = True
        mock_add.return_value = {}

        ok = supabase_client.process_optimization_result(file_id)

        self.assertTrue(ok)
        eq_calls = mock_table.update.return_value.eq.call_args_list
        # orders.update(...).eq("id", order_id) 2 kez; optimization_runs.update(...).eq("file_id", file_id) 1 kez
        order_eq_calls = [c for c in eq_calls if c[0][0] == "id"]
        self.assertEqual(len(order_eq_calls), 2, "İki sipariş güncellenmeli (cutting_plan'da orderId 1 ve 2)")
        updated_ids = [c[0][1] for c in order_eq_calls]
        self.assertIn("ord-1", updated_ids)
        self.assertIn("ord-2", updated_ids)

    @patch.object(supabase_client, "get_supabase_client")
    @patch.object(supabase_client, "get_run_by_file_id")
    def test_process_result_returns_false_when_run_not_found(self, mock_get_run, mock_get_client):
        """Run bulunamazsa False dönmeli."""
        mock_get_client.return_value = MagicMock()
        mock_get_run.return_value = None
        ok = supabase_client.process_optimization_result("nonexistent-file")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""
Frontend gibi stok/sipariş ekleyip farklı optimizasyon ayarlarıyla test eder.
Yüksek m² değerleri kullanır; stok–sipariş eşleşmelerinde fire ve stok oluşacak şekilde senaryolar tasarlanır.
Sonuçlar dashboard > Sonuçlar sayfasında test-1, test-2, ... olarak görünür.
Backend çalışıyor olmalı (örn. http://localhost:8000).

Tonaj yaklaşımı: 0.5 mm, 2.7 yoğunluk → ~1.35 ton / 1000 m²
"""

import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000")

# Malzeme: 0.5 mm, 2.7 g/cm³ → m² * 0.00135 = ton (yaklaşık)
DEFAULT_MATERIAL = {"thickness": 0.5, "density": 2.7}


def _request(method: str, path: str, body: dict | None = None) -> dict:
    """Backend API'ye HTTP isteği gönderir."""
    url = f"{BASE_URL.rstrip('/')}{path}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            err = json.loads(raw)
            detail = err.get("detail", raw)
        except Exception:
            detail = raw
        raise RuntimeError(f"API {method} {path}: {e.code} - {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Backend'e bağlanılamadı ({url}): {e.reason}") from e


def add_stock_roll(tonnage: float) -> dict:
    """Tek rulo ekler (POST /api/stock-rolls)."""
    return _request("POST", "/api/stock-rolls", {"tonnage": tonnage})


def add_order(
    order_id: str,
    m2: float,
    panel_width: float,
    panel_length: float = 1.0,
    aciklama: str | None = None,
) -> dict:
    """Tek sipariş ekler (POST /api/orders)."""
    body = {
        "order_id": order_id,
        "m2": m2,
        "panel_width": panel_width,
        "panel_length": panel_length,
        "status": "Pending",
    }
    if aciklama:
        body["aciklama"] = aciklama
    return _request("POST", "/api/orders", body)


def run_optimize(
    description: str,
    orders: list[dict],
    roll_settings: dict,
    material: dict | None = None,
    costs: dict | None = None,
    save_to_db: bool = True,
) -> dict:
    """Optimizasyon çalıştırır (POST /api/optimize). Sonuçta summary içinde totalFire, totalStock vardır."""
    if material is None:
        material = DEFAULT_MATERIAL.copy()
    if costs is None:
        costs = {"fireCost": 100.0, "setupCost": 50.0, "stockCost": 20.0}
    body = {
        "material": material,
        "orders": orders,
        "rollSettings": roll_settings,
        "costs": costs,
        "safetyStock": 0,
        "saveToDb": save_to_db,
        "description": description,
    }
    return _request("POST", "/api/optimize", body)


def _orders_to_payload(orders: list[tuple]) -> list[dict]:
    """(order_id, m2, panel_width, panel_length) listesini API payload'a çevirir."""
    return [
        {
            "orderId": o[0],
            "m2": o[1],
            "panelWidth": o[2],
            "panelLength": o[3] if len(o) > 3 else 1.0,
        }
        for o in orders
    ]


def main() -> None:
    """Yüksek m² siparişler ve çeşitli rulo setleriyle birden fazla test senaryosu çalıştırır."""
    print(f"Backend: {BASE_URL}")
    print("Malzeme: 0.5 mm, 2.7 → ~1.35 ton / 1000 m²")
    print("---")

    # ---------- Stok ruloları (farklı tonajlarda; eşleşme/fire/stok çeşitliliği için) ----------
    print("Stok ruloları ekleniyor...")
    add_stock_roll(10.0)
    add_stock_roll(12.0)
    add_stock_roll(15.0)
    add_stock_roll(18.0)
    add_stock_roll(8.0)
    add_stock_roll(20.0)
    print("  6 rulo eklendi (8, 10, 12, 15, 18, 20 ton).")

    # ---------- Sipariş setleri (yüksek m²; her set ~15–55 ton talep aralığı) ----------
    # Set A: 3 sipariş, toplam ~27 ton
    orders_a = [
        ("test-A1", 8_000, 1.0, 1.0),
        ("test-A2", 12_000, 1.2, 1.0),
        ("test-A3", 7_000, 0.9, 1.0),
    ]
    for oid, m2, pw, pl in orders_a:
        add_order(oid, float(m2), pw, pl, f"Yüksek m2 set A - {m2} m²")
    payload_a = _orders_to_payload(orders_a)

    # Set B: 4 sipariş, toplam ~40 ton
    orders_b = [
        ("test-B1", 10_000, 1.0, 1.0),
        ("test-B2", 9_000, 1.1, 1.0),
        ("test-B3", 11_000, 1.0, 1.0),
        ("test-B4", 5_000, 1.2, 1.0),
    ]
    for oid, m2, pw, pl in orders_b:
        add_order(oid, float(m2), pw, pl, f"Yüksek m2 set B - {m2} m²")
    payload_b = _orders_to_payload(orders_b)

    # Set C: 2 büyük sipariş, toplam ~35 ton
    orders_c = [
        ("test-C1", 15_000, 1.0, 1.0),
        ("test-C2", 20_000, 1.2, 1.0),
    ]
    for oid, m2, pw, pl in orders_c:
        add_order(oid, float(m2), pw, pl, f"Yüksek m2 set C - {m2} m²")
    payload_c = _orders_to_payload(orders_c)

    # Set D: 5 sipariş, dağılımlı, toplam ~50 ton
    orders_d = [
        ("test-D1", 8_000, 1.0, 1.0),
        ("test-D2", 10_000, 1.0, 1.0),
        ("test-D3", 12_000, 1.1, 1.0),
        ("test-D4", 9_000, 0.95, 1.0),
        ("test-D5", 11_000, 1.0, 1.0),
    ]
    for oid, m2, pw, pl in orders_d:
        add_order(oid, float(m2), pw, pl, f"Yüksek m2 set D - {m2} m²")
    payload_d = _orders_to_payload(orders_d)

    results_summary = []

    # ---------- test-1: Set A, manuel rulolar [10,12,15] = 37 ton, max 2 sipariş/rulo ----------
    print("Optimizasyon test-1 (set A, manuel 10+12+15 ton, max 2 sipariş/rulo)...")
    r1 = run_optimize(
        description="test-1",
        orders=payload_a,
        roll_settings={"rolls": [10.0, 12.0, 15.0], "maxOrdersPerRoll": 2, "maxRollsPerOrder": 3},
    )
    s1 = r1.get("summary", {})
    results_summary.append(("test-1", s1.get("totalCost"), s1.get("totalFire", 0), s1.get("totalStock", 0), s1.get("openedRolls", 0)))

    # ---------- test-2: Set A, otomatik toplam 40 ton (4–10 arası bölme), max 3 sipariş/rulo ----------
    print("Optimizasyon test-2 (set A, otomatik 40 ton, max 3 sipariş/rulo)...")
    r2 = run_optimize(
        description="test-2",
        orders=payload_a,
        roll_settings={
            "totalTonnage": 40.0,
            "minRollTon": 4,
            "maxRollTon": 10,
            "maxOrdersPerRoll": 3,
            "maxRollsPerOrder": 3,
        },
    )
    s2 = r2.get("summary", {})
    results_summary.append(("test-2", s2.get("totalCost"), s2.get("totalFire", 0), s2.get("totalStock", 0), s2.get("openedRolls", 0)))

    # ---------- test-3: Set B, manuel [20,20,10] = 50 ton (ihtiyaç ~47), max 2 sipariş/rulo ----------
    print("Optimizasyon test-3 (set B, manuel 20+20+10 ton, max 2 sipariş/rulo)...")
    r3 = run_optimize(
        description="test-3",
        orders=payload_b,
        roll_settings={"rolls": [20.0, 20.0, 10.0], "maxOrdersPerRoll": 2, "maxRollsPerOrder": 2},
    )
    s3 = r3.get("summary", {})
    results_summary.append(("test-3", s3.get("totalCost"), s3.get("totalFire", 0), s3.get("totalStock", 0), s3.get("openedRolls", 0)))

    # ---------- test-4: Set B, otomatik 50 ton, max 4 sipariş/rulo ----------
    print("Optimizasyon test-4 (set B, otomatik 50 ton, max 4 sipariş/rulo)...")
    r4 = run_optimize(
        description="test-4",
        orders=payload_b,
        roll_settings={
            "totalTonnage": 50.0,
            "minRollTon": 8,
            "maxRollTon": 15,
            "maxOrdersPerRoll": 4,
            "maxRollsPerOrder": 3,
        },
    )
    s4 = r4.get("summary", {})
    results_summary.append(("test-4", s4.get("totalCost"), s4.get("totalFire", 0), s4.get("totalStock", 0), s4.get("openedRolls", 0)))

    # ---------- test-5: Set C, büyük rulolar [25,25] = 50 ton (ihtiyaç ~47), max 2 sipariş/rulo ----------
    print("Optimizasyon test-5 (set C, manuel 25+25 ton, max 2 sipariş/rulo)...")
    r5 = run_optimize(
        description="test-5",
        orders=payload_c,
        roll_settings={"rolls": [25.0, 25.0], "maxOrdersPerRoll": 2, "maxRollsPerOrder": 2},
    )
    s5 = r5.get("summary", {})
    results_summary.append(("test-5", s5.get("totalCost"), s5.get("totalFire", 0), s5.get("totalStock", 0), s5.get("openedRolls", 0)))

    # ---------- test-6: Set C, fazla tonaj [12,12,12,12] = 48 ton (bilerek fire/stok bırakacak) ----------
    print("Optimizasyon test-6 (set C, manuel 4x12 ton, max 2 sipariş/rulo)...")
    r6 = run_optimize(
        description="test-6",
        orders=payload_c,
        roll_settings={"rolls": [12.0, 12.0, 12.0, 12.0], "maxOrdersPerRoll": 2, "maxRollsPerOrder": 3},
    )
    s6 = r6.get("summary", {})
    results_summary.append(("test-6", s6.get("totalCost"), s6.get("totalFire", 0), s6.get("totalStock", 0), s6.get("openedRolls", 0)))

    # ---------- test-7: Set D, otomatik 70 ton (ihtiyaç ~67.5), yüksek fire maliyeti ----------
    print("Optimizasyon test-7 (set D, otomatik 70 ton, yüksek fire maliyeti)...")
    r7 = run_optimize(
        description="test-7",
        orders=payload_d,
        roll_settings={
            "totalTonnage": 70.0,
            "minRollTon": 8,
            "maxRollTon": 14,
            "maxOrdersPerRoll": 4,
            "maxRollsPerOrder": 3,
        },
        costs={"fireCost": 250.0, "setupCost": 50.0, "stockCost": 25.0},
    )
    s7 = r7.get("summary", {})
    results_summary.append(("test-7", s7.get("totalCost"), s7.get("totalFire", 0), s7.get("totalStock", 0), s7.get("openedRolls", 0)))

    # ---------- test-8: Set D, manuel [15,15,15,15,15] = 75 ton (ihtiyaç ~67), stok maliyeti düşük ----------
    print("Optimizasyon test-8 (set D, manuel 5x15 ton, düşük stok maliyeti)...")
    r8 = run_optimize(
        description="test-8",
        orders=payload_d,
        roll_settings={"rolls": [15.0, 15.0, 15.0, 15.0, 15.0], "maxOrdersPerRoll": 3, "maxRollsPerOrder": 3},
        costs={"fireCost": 100.0, "setupCost": 60.0, "stockCost": 10.0},
    )
    s8 = r8.get("summary", {})
    results_summary.append(("test-8", s8.get("totalCost"), s8.get("totalFire", 0), s8.get("totalStock", 0), s8.get("openedRolls", 0)))

    # ---------- test-9: Set A, [20,20] = 40 ton (ihtiyaç ~36.5, kalan fire/stok) ----------
    print("Optimizasyon test-9 (set A, 2 rulo 20+20 ton, fire/stok çeşitliliği)...")
    r9 = run_optimize(
        description="test-9",
        orders=payload_a,
        roll_settings={"rolls": [20.0, 20.0], "maxOrdersPerRoll": 3, "maxRollsPerOrder": 2},
    )
    s9 = r9.get("summary", {})
    results_summary.append(("test-9", s9.get("totalCost"), s9.get("totalFire", 0), s9.get("totalStock", 0), s9.get("openedRolls", 0)))

    # ---------- test-10: Set B, [25,25] = 50 ton (ihtiyaç ~47) ----------
    print("Optimizasyon test-10 (set B, 25+25 ton, max 3 sipariş/rulo)...")
    r10 = run_optimize(
        description="test-10",
        orders=payload_b,
        roll_settings={"rolls": [25.0, 25.0], "maxOrdersPerRoll": 3, "maxRollsPerOrder": 2},
    )
    s10 = r10.get("summary", {})
    results_summary.append(("test-10", s10.get("totalCost"), s10.get("totalFire", 0), s10.get("totalStock", 0), s10.get("openedRolls", 0)))

    # ---------- Özet ----------
    print("---")
    print("Özet (maliyet | fire ton | stok ton | açılan rulo):")
    for name, cost, fire, stock, opened in results_summary:
        print(f"  {name}: {cost:.2f} | fire={fire:.3f} | stock={stock:.3f} | rulo={opened}")
    fire_any = any(s[2] > 0 for s in results_summary)
    stock_any = any(s[3] > 0 for s in results_summary)
    if not fire_any and not stock_any:
        print("\nUyarı: Hiç fire veya stok oluşmadı. Sayıları güncelleyip tekrar çalıştırabilirsiniz.")
    else:
        print("\nFire ve/veya stok oluşan senaryolar mevcut. Sonuçları dashboard > Sonuçlar sayfasında inceleyebilirsiniz.")
    print("Tamamlandı.")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        print(f"Hata: {e}", file=sys.stderr)
        sys.exit(1)

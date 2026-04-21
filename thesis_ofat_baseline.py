"""
OFAT duyarlılık analizi: kod içi referans girdi (test_calistir ile uyumlu).
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple  # noqa: F401

# Malzeme: ConfigurationForm varsayılanlarına yakın (mm, g/cm³)
DEFAULT_THICKNESS_MM = 0.75
DEFAULT_DENSITY_G_CM3 = 7.85

# Maliyet referansı (optimizasyonda birim başına katsayı; UI / ConfigurationForm ile uyumlu başlangıç)
DEFAULT_FIRE_COST = 100.0
DEFAULT_SETUP_COST = 50.0
DEFAULT_STOCK_COST = 30.0

# Tek-eksen OFAT dalgalarında (yalnızca fire VEYA stok VEYA kurulum) uygulanan çarpan uçları — sınır davranışı görmek için geniş aralık
OFAT_COST_AXIS_MULTS: Tuple[float, ...] = (0.5, 0.8, 1.0, 1.2, 1.5)

# Rulo / sipariş üst sınırları (istek gövdesi)
DEFAULT_MAX_ORDERS_PER_ROLL = 6
DEFAULT_MAX_ROLLS_PER_ORDER = 8

# Rulolar 4-13 ton bandında random bölünür (gerçek üretim rulosu boyutları)
DEFAULT_ROLLS_BAND: Tuple[int, int] = (4, 13)
DEFAULT_ROLLS_SEED: int = 42

# Toplam rulo tonajı: 5 sipariş × ortalama ~8 t talep × 2 (çift yüzey) ≈ 80 t kullanım
# 85 t toplam kapasite → 4-13 bandında ≈10-11 fiziksel rulo. Gerçek üretim ölçeğine uygun.
DEFAULT_TOTAL_ROLL_TONNAGE = 85.0

# Tek sipariş (eski tek-sipariş testleri için; OFAT çoklu sipariş kullanır)
DEFAULT_ORDER_M2 = 1700.0  # ≈10 ton (0.75 mm, 7.85 g/cm³)
DEFAULT_PANEL_WIDTH = 1.0
DEFAULT_PANEL_LENGTH = 1.0

# Çoklu sipariş OFAT: sipariş başı m² aralığı ve varsayılan sipariş adedi
# 800-2000 m² bandı ≈4.7-11.8 t/sipariş (orta ölçek, baseline 5 sipariş × ort 8 t = 40 t talep)
MULTI_ORDER_M2_MIN = 800.0
MULTI_ORDER_M2_MAX = 2000.0
OFAT_DEFAULT_ORDER_COUNT = 5

# Baseline'da tam ölçek kullanılır. OFAT demand ekseninde 0.8/1.0/1.2 çarpanı ile oynatılır.
OFAT_DEMAND_SCALE = 1.0


def multi_order_m2_values(n_orders: int) -> List[float]:
    """
    n sipariş için m² değerlerini [MULTI_ORDER_M2_MIN, MULTI_ORDER_M2_MAX] aralığında üretir.

    Tekdüze aralık kullanılır; tekrarlanabilir OFAT ve karşılaştırma için deterministiktir.

    Args:
        n_orders: Sipariş sayısı (en az 1)

    Returns:
        Sipariş başı m² listesi (artan)
    """
    n = max(1, int(n_orders))
    if n == 1:
        return [MULTI_ORDER_M2_MIN]
    lo, hi = float(MULTI_ORDER_M2_MIN), float(MULTI_ORDER_M2_MAX)
    step = (hi - lo) / (n - 1)
    return [round(lo + i * step, 2) for i in range(n)]


def baseline_orders_multi(n_orders: int, scale_m2: float = 1.0) -> List[Dict[str, Any]]:
    """
    800–3500 m² bandında profilli çoklu sipariş listesi üretir.

    Args:
        n_orders: 3–6 gibi sipariş adedi
        scale_m2: Tüm sipariş m² değerlerine uygulanan çarpan (OFAT talep dalgası)

    Returns:
        musteri_talepleri biçiminde liste
    """
    m2s = multi_order_m2_values(n_orders)
    s = float(scale_m2)
    return [
        {"m2": float(m) * s, "panelWidth": DEFAULT_PANEL_WIDTH, "panelLength": DEFAULT_PANEL_LENGTH}
        for m in m2s
    ]


def build_ofat_baseline_aciklama() -> str:
    """
    OFAT raporlarında kullanılan güncel baseline özet cümlesini üretir (çoklu sipariş + tonaj).

    Returns:
        Tek satırlık Türkçe açıklama
    """
    n = OFAT_DEFAULT_ORDER_COUNT
    raw = multi_order_m2_values(n)
    scaled = [round(m * OFAT_DEMAND_SCALE, 2) for m in raw]
    return (
        f"{n} sipariş (referans aralığı {int(MULTI_ORDER_M2_MIN)}–{int(MULTI_ORDER_M2_MAX)} m² doğrusal profil; "
        f"OFAT m² ölçeği ×{OFAT_DEMAND_SCALE} → ≈{scaled[0]:.1f}–{scaled[-1]:.1f} m²), "
        f"toplam rulo ton={DEFAULT_TOTAL_ROLL_TONNAGE}, "
        f"maxOrdersPerRoll={DEFAULT_MAX_ORDERS_PER_ROLL}, maxRollsPerOrder={DEFAULT_MAX_ROLLS_PER_ORDER}, "
        f"fire/setup/stock={DEFAULT_FIRE_COST}/{DEFAULT_SETUP_COST}/{DEFAULT_STOCK_COST}"
    )


# OFAT raporlarında "diğerleri sabit" cümlesi (modül yüklemesinde sabitlenir)
OFAT_BASELINE_ACIKLAMA = build_ofat_baseline_aciklama()


def baseline_orders(scale_m2: float = 1.0) -> List[Dict[str, Any]]:
    """
    Baseline sipariş listesini m² ölçek çarpanı ile döner.

    Args:
        scale_m2: m² çarpanı (OFAT talep ekseni)

    Returns:
        musteri_talepleri biçiminde liste
    """
    return [
        {
            "m2": DEFAULT_ORDER_M2 * scale_m2,
            "panelWidth": DEFAULT_PANEL_WIDTH,
            "panelLength": DEFAULT_PANEL_LENGTH,
        }
    ]


def baseline_costs(
    fire_mult: float = 1.0,
    stock_mult: float = 1.0,
    setup_mult: float = 1.0,
) -> Dict[str, float]:
    """
    Maliyet parametrelerini referans değerlerden çarpanla üretir.

    Args:
        fire_mult: Fire maliyeti çarpanı
        stock_mult: Stok maliyeti çarpanı
        setup_mult: Kurulum maliyeti çarpanı

    Returns:
        fireCost, setupCost, stockCost sözlüğü
    """
    return {
        "fire_cost": DEFAULT_FIRE_COST * fire_mult,
        "setup_cost": DEFAULT_SETUP_COST * setup_mult,
        "stock_cost": DEFAULT_STOCK_COST * stock_mult,
    }

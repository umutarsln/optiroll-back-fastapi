"""
Tez doğrulama: ön kontrol ve Infeasible durumları için makine-okur kodlar ve Türkçe ipuçları.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

# Ön kontrol (API ile hizalı) kodları
NO_ORDERS = "NO_ORDERS"
CAPACITY_LT_DEMAND = "CAPACITY_LT_DEMAND"
INVALID_MATERIAL = "INVALID_MATERIAL"
INVALID_ROLLS = "INVALID_ROLLS"
MAX_ROLLS_PER_ORDER_RULE = "MAX_ROLLS_PER_ORDER_RULE"
INVALID_PANEL = "INVALID_PANEL"

# Solver / yapısal
DUAL_SURFACE_MIN_TWO_ROLLS = "DUAL_SURFACE_MIN_TWO_ROLLS"
MAX_ORDERS_PER_ROLL_TOO_TIGHT = "MAX_ORDERS_PER_ROLL_TOO_TIGHT"
MAX_ROLLS_PER_ORDER_TOO_TIGHT = "MAX_ROLLS_PER_ORDER_TOO_TIGHT"
SURFACE_SYNC_INFEASIBLE = "SURFACE_SYNC_INFEASIBLE"
INFEASIBLE_UNKNOWN = "INFEASIBLE_UNKNOWN"


def hints_for_code(code: Optional[str]) -> List[str]:
    """
    Verilen failure_code için kullanıcıya gösterilecek kısa Türkçe ipuçları listesini döner.

    Args:
        code: Makine kodu veya None

    Returns:
        İpucu metinleri listesi
    """
    if not code:
        return ["Bilinmeyen durum: girdi ve kısıt parametrelerini kontrol edin."]
    table = {
        NO_ORDERS: ["En az bir sipariş ekleyin."],
        CAPACITY_LT_DEMAND: [
            "Toplam rulo tonajını artırın veya sipariş m² / panel ölçülerini azaltın.",
            "Çift yüzey (2×) talep hesabının ihtiyaç tonajına dahil olduğunu unutmayın.",
        ],
        INVALID_MATERIAL: ["Kalınlık ve yoğunluk değerlerini 0'dan büyük girin."],
        INVALID_ROLLS: ["Geçerli rulo tonajları girin (pozitif tam sayılar)."],
        MAX_ROLLS_PER_ORDER_RULE: [
            "Çift yüzey senaryosunda max rulo/sipariş en az 2 olmalıdır.",
        ],
        INVALID_PANEL: ["Panel genişliği ve uzunluğunu pozitif girin."],
        DUAL_SURFACE_MIN_TWO_ROLLS: [
            "Çift yüzeyde en az iki fiziksel rulo tanımlayın (rulo tonajları listesi).",
        ],
        MAX_ORDERS_PER_ROLL_TOO_TIGHT: [
            "Maksimum sipariş/rulo değerini artırın veya sipariş sayısını azaltın.",
        ],
        MAX_ROLLS_PER_ORDER_TOO_TIGHT: [
            "Maksimum rulo/sipariş değerini artırın; talep birden çok ruloya dağıtılamıyor olabilir.",
        ],
        SURFACE_SYNC_INFEASIBLE: [
            "Üst/alt yüzey senkron kısıtı çelişiyor olabilir; senkron seviyesini gevşetin (ör. serbest/dengeli).",
        ],
        INFEASIBLE_UNKNOWN: [
            "Model uygun çözüm bulamadı (Infeasible).",
            "Girdi özetini kontrol edin: rulo sayısı, max sipariş/rulo, max rulo/sipariş, talep tonajı.",
        ],
    }
    return table.get(code, list(table[INFEASIBLE_UNKNOWN]))


def classify_precheck(
    *,
    orders: Sequence[Dict[str, Any]],
    thickness: float,
    density: float,
    rolls: Sequence[int],
    max_orders_per_roll: int,
    max_rolls_per_order: int,
    total_tonnage_needed: float,
    total_roll_tonnage: int,
    surface_factor: float = 2.0,
) -> Tuple[Optional[str], List[str]]:
    """
    API ile uyumlu ön kontrolleri değerlendirir; ilk ihlalde (kod, ipuçları) döner.

    Args:
        orders: Sipariş dict listesi (m2, panelWidth, panelLength)
        thickness: Kalınlık (mm)
        density: Yoğunluk (g/cm³)
        rolls: Rulo tonajları (tam sayı ton)
        max_orders_per_roll: Rulo başına max sipariş
        max_rolls_per_order: Sipariş başına max rulo
        total_tonnage_needed: calculate_demand ile hesaplanan toplam talep (ton)
        total_roll_tonnage: Toplam rulo kapasitesi (ton)
        surface_factor: Talep çarpanı (KDS'de 2)

    Returns:
        (failure_code veya None geçtiyse, hints)
    """
    if len(orders) == 0:
        return NO_ORDERS, hints_for_code(NO_ORDERS)
    if thickness <= 0 or density <= 0:
        return INVALID_MATERIAL, hints_for_code(INVALID_MATERIAL)
    if not rolls or all(r <= 0 for r in rolls):
        return INVALID_ROLLS, hints_for_code(INVALID_ROLLS)
    if surface_factor >= 2.0 - 1e-12 and max_rolls_per_order < 2:
        return MAX_ROLLS_PER_ORDER_RULE, hints_for_code(MAX_ROLLS_PER_ORDER_RULE)
    if max_orders_per_roll < 1:
        return INVALID_ROLLS, hints_for_code(INVALID_ROLLS)
    if total_roll_tonnage < total_tonnage_needed - 0.01:
        return CAPACITY_LT_DEMAND, hints_for_code(CAPACITY_LT_DEMAND)
    return None, []


def classify_infeasible_structure(
    *,
    rolls: Sequence[int],
    num_orders: int,
    max_orders_per_roll: int,
    max_rolls_per_order: int,
    surface_factor: float,
    enforce_surface_sync: bool,
) -> Tuple[str, List[str]]:
    """
    Infeasible solver çıktısı için yapısal sezgilerle failure_code tahmin eder.

    Args:
        rolls: Rulo tonaj listesi
        num_orders: Sipariş sayısı
        max_orders_per_roll: Rulo başına max sipariş sayısı
        max_rolls_per_order: Sipariş başına max rulo
        surface_factor: Çift yüzey çarpanı
        enforce_surface_sync: Üst/alt rulo sayısı sert eşitleme

    Returns:
        (failure_code, hints)
    """
    if surface_factor >= 2.0 - 1e-12 and len([r for r in rolls if r > 0]) < 2:
        code = DUAL_SURFACE_MIN_TWO_ROLLS
        return code, hints_for_code(code)
    if num_orders > 0 and max_orders_per_roll < num_orders and max_orders_per_roll < 1:
        code = MAX_ORDERS_PER_ROLL_TOO_TIGHT
        return code, hints_for_code(code)
    if num_orders > 1 and max_orders_per_roll == 1:
        code = MAX_ORDERS_PER_ROLL_TOO_TIGHT
        return code, hints_for_code(code)
    if max_rolls_per_order <= 2 and num_orders >= 1:
        # Dar aralık; gerçek Infeasible başka nedenlerden de olabilir
        pass
    if enforce_surface_sync:
        code = SURFACE_SYNC_INFEASIBLE
        return code, hints_for_code(code)
    code = INFEASIBLE_UNKNOWN
    return code, hints_for_code(code)


def merge_hints(*parts: List[str]) -> List[str]:
    """
    İpucu listelerini tekrarsız birleştirir.

    Args:
        *parts: İpucu listeleri

    Returns:
        Birleştirilmiş liste
    """
    out: List[str] = []
    seen = set()
    for p in parts:
        for h in p:
            if h not in seen:
                seen.add(h)
                out.append(h)
    return out

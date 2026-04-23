"""
Optimizasyon mantığı - Modüler fonksiyonlar
"""
import pulp
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill
from datetime import datetime
import os
import random
from collections import defaultdict, deque
from typing import Any, Dict, List, Tuple, Optional, Set, Sequence
import logging
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Bu eşiğin (ton) altındaki rulo artığı stok değil fire sayılır (iş kuralı).
MIN_STOCK_THRESHOLD_TON = 0.5
# Raporlama ve stok/fire ayrımı kg tam sayılarıyla yapılır (1 kg = 0,001 t; LP içi ton sürekli kalır).
MIN_STOCK_THRESHOLD_KG = int(round(MIN_STOCK_THRESHOLD_TON * 1000.0))


def _ton_to_kg_int(ton: float) -> int:
    """
    Ton değerini en yakın tam sayı kg'ye çevirir (fiziksel birim kg; bobin kapasitesi burada kesilir).

    Args:
        ton: Rulo veya miktar (ton).

    Returns:
        Kilogram cinsinden tam sayı.
    """
    return int(round(float(ton) * 1000.0))


def _kg_int_to_ton(kg: int) -> float:
    """
    Tam sayı kg'yi tona çevirir; ek yuvarlama yok (ton değeri her zaman 0,001'in katıdır).

    Args:
        kg: Kilogram (tam sayı).

    Returns:
        Ton cinsinden değer.
    """
    return int(kg) / 1000.0


def _split_remainder_kg(rem_kg: int) -> Tuple[int, int]:
    """
    Açılmış ruloda kalan kg'yi iş kuralına göre fire veya üretim stoğuna böler (eşik MIN_STOCK_THRESHOLD_KG).

    Args:
        rem_kg: Kapasite (kg) eksi kullanılan (kg); negatifler sıfırlanır.

    Returns:
        (fire_kg, uretim_stok_kg) tam sayı çifti.
    """
    r = max(0, int(rem_kg))
    if r <= MIN_STOCK_THRESHOLD_KG:
        return r, 0
    return 0, r


def _split_remainder_for_reporting(rem_ton: float) -> Tuple[float, float]:
    """
    Ton cinsinden kalanı kg'ye taşıyıp böler; API ton alanları için (geriye dönük yardımcı).

    Args:
        rem_ton: Bobin kapasitesi eksi kullanılan (S - u).

    Returns:
        (fire_ton, uretim_stok_ton).
    """
    rem_kg = _ton_to_kg_int(rem_ton)
    fk, sk = _split_remainder_kg(rem_kg)
    return _kg_int_to_ton(fk), _kg_int_to_ton(sk)


class OptimizationInput(BaseModel):
    """Optimizasyon input modeli"""
    material: Dict[str, float]  # thickness, density
    orders: List[Dict[str, float]]  # m2, panelWidth
    rollSettings: Dict[str, float]  # totalTonnage, maxOrdersPerRoll
    costs: Dict[str, float]  # fireCost, setupCost, stockCost


class OptimizationResult(BaseModel):
    """Optimizasyon sonuç modeli"""
    status: str
    objective: float
    summary: Dict[str, float]
    cuttingPlan: List[Dict]
    rollStatus: List[Dict]
    fileId: str


def generate_rolls(total_tonnage: float, min_roll: int = 4, max_roll: int = 10) -> List[int]:
    """
    Ruloları min-max ton aralığında oluştur. Sıkı kısıtlarda (maxRuloPerSiparis)
    daha fazla rulo = daha fazla esneklik. Daha küçük max_roll = daha çok rulo.
    
    Args:
        total_tonnage: Toplam rulo tonajı
        min_roll: Minimum rulo tonajı
        max_roll: Maksimum rulo tonajı
    
    Returns:
        Rulo tonajları listesi
    """
    rolls = []
    remaining = int(total_tonnage)
    
    while remaining > max_roll:
        roll_tonnage = random.randint(min_roll, max_roll)
        rolls.append(roll_tonnage)
        remaining -= roll_tonnage
    
    if remaining > 0:
        if remaining < min_roll:
            rolls.append(min_roll)
        else:
            rolls.append(remaining)
    
    rolls.sort(reverse=True)
    return rolls


def calculate_demand(
    orders: List[Dict],
    thickness: float,
    density: float,
    panel_widths: Optional[List[float]] = None,
    panel_lengths: Optional[List[float]] = None,
    surface_factor: float = 1.0,
) -> Tuple[Dict, float]:
    """
    Siparişleri ton'a çevir. Tam sayı panel kısıtı için m²'yi panele yuvarlar.
    Panel = genişlik x uzunluk; kesim uzunluk ve katları şeklinde (örn. 3m → 3, 6, 9m).
    
    Args:
        orders: Sipariş listesi (m2, panelWidth, panelLength)
        thickness: Kalınlık (mm)
        density: Yoğunluk (g/cm³)
        panel_widths: Panel genişlikleri (yoksa order içinden alınır)
        panel_lengths: Panel kesim uzunlukları (yoksa 1.0 veya order içinden)
        surface_factor: Talebi yüzey sayısına göre çarpan (1: tek yüzey, 2: çift yüzey)
    
    Returns:
        (Demand dictionary, Toplam tonaj)
    """
    demand = {}
    for j, order in enumerate(orders):
        pw = (panel_widths[j] if panel_widths is not None else order['panelWidth'])
        pl = (panel_lengths[j] if panel_lengths is not None else order.get('panelLength', 1.0))
        if pl <= 0:
            pl = 1.0
        # Tam sayı panel: m² / (genişlik * uzunluk) → 3*33+1 fire örneği gibi
        panel_count = round(order['m2'] / (pw * pl)) if (pw * pl) > 0 else 0
        panel_count = max(1, panel_count)
        m2_eff = panel_count * pw * pl * max(1.0, float(surface_factor))
        demand[j] = round(m2_eff * (thickness / 1000) * density, 4)
    
    total_tonnage = sum(demand.values())
    return demand, total_tonnage


def build_roll_order_sequence(cutting_plan: List[Dict]) -> Dict[int, List[int]]:
    """
    Kesim planı satırlarından rulo başına sipariş kimlik sırasını üretir.
    Satırların listedeki sırası, o rulodaki işlem sırası kabul edilir.

    Args:
        cutting_plan: rollId ve orderId içeren kesim planı öğeleri

    Returns:
        rollId -> o ruloda geçen sipariş numaraları (1 tabanlı) sırası
    """
    by_roll: Dict[int, List[int]] = {}
    for row in cutting_plan:
        rid = int(row["rollId"])
        oid = int(row["orderId"])
        by_roll.setdefault(rid, []).append(oid)
    return by_roll


def calculate_return_gap_penalty(
    roll_sequences: Dict[int, List[int]],
    max_interleaving: int,
    penalty_per_excess: float,
) -> Tuple[float, List[Dict]]:
    """
    Aynı siparişe art arda dönüşlerde araya giren farklı sipariş sayısını ölçer.
    İki ziyaret arasında (önceki ve sonraki aynı sipariş indeksleri arası)
    farklı sipariş kimlikleri max_interleaving'den fazlaysa soft ceza uygular.

    Args:
        roll_sequences: Rulo başına sipariş sırası (orderId listesi)
        max_interleaving: İzin verilen en fazla araya giren farklı sipariş sayısı
        penalty_per_excess: Her fazla farklı sipariş için ceza birimi

    Returns:
        (toplam_ceza, ihlal kayıtları listesi)
    """
    violations: List[Dict] = []
    total_penalty = 0.0
    if max_interleaving < 0:
        max_interleaving = 0

    for roll_id, seq in roll_sequences.items():
        if len(seq) < 2:
            continue

        positions: Dict[int, List[int]] = defaultdict(list)
        for idx, oid in enumerate(seq):
            positions[oid].append(idx)

        for oid, idx_list in positions.items():
            for t in range(len(idx_list) - 1):
                p, q = idx_list[t], idx_list[t + 1]
                between = seq[p + 1 : q]
                distinct_others = len({x for x in between if x != oid})
                if distinct_others > max_interleaving:
                    excess = distinct_others - max_interleaving
                    if penalty_per_excess > 0:
                        total_penalty += excess * penalty_per_excess
                    violations.append({
                        "rollId": roll_id,
                        "orderId": oid,
                        "distinctInterleavedOrders": distinct_others,
                        "maxAllowed": max_interleaving,
                        "excess": excess,
                    })

    return round(total_penalty, 4), violations


def apply_sequence_local_improvement(
    cutting_plan: List[Dict],
    max_interleaving: int,
    penalty_per_excess: float,
) -> Tuple[List[Dict], Dict[int, List[int]]]:
    """
    Aynı rulodaki kesim satırlarının sırasını yeniden düzenleyerek sıra cezasını düşürmeyi dener.
    Her rulo için orijinal, ters ve sipariş numarasına göre artan/azalan sıralar karşılaştırılır.

    Args:
        cutting_plan: Kesim planı satırları
        max_interleaving: calculate_return_gap_penalty ile aynı parametre
        penalty_per_excess: Birim ceza

    Returns:
        (iyileştirilmiş kesim planı, rulo sipariş sıraları)
    """
    if not cutting_plan or penalty_per_excess <= 0:
        seq = build_roll_order_sequence(cutting_plan)
        return cutting_plan, seq

    rolls_order: List[int] = []
    by_roll: Dict[int, List[Dict]] = {}
    for row in cutting_plan:
        rid = int(row["rollId"])
        if rid not in by_roll:
            rolls_order.append(rid)
            by_roll[rid] = []
        by_roll[rid].append(row)

    def roll_only_penalty(roll_id: int, chunk: List[Dict]) -> float:
        """Yalnızca tek rulo sırası için sıra cezasını hesaplar."""
        seq_map = {roll_id: [int(r["orderId"]) for r in chunk]}
        p, _ = calculate_return_gap_penalty(seq_map, max_interleaving, penalty_per_excess)
        return p

    new_rows: List[Dict] = []
    for rid in rolls_order:
        chunk = by_roll[rid]
        candidates = [
            chunk,
            list(reversed(chunk)),
            sorted(chunk, key=lambda r: int(r["orderId"])),
            sorted(chunk, key=lambda r: -int(r["orderId"])),
        ]
        best = min(candidates, key=lambda c: roll_only_penalty(rid, c))
        new_rows.extend(best)

    seq = build_roll_order_sequence(new_rows)
    return new_rows, seq


def _partition_order_slices_for_surfaces(order_rows: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """
    Bir siparişe ait kesim satırlarını üst/alt yüzey listelerine ayırır.
    Modelden gelen upperTonnage/lowerTonnage varsa doğrudan kullanır; yoksa ton dengelemeli yedek ayırım yapar.

    Args:
        order_rows: Aynı orderId için kesim satırları

    Returns:
        (upper_rows, lower_rows)
    """
    has_model_split = any(
        (float(r.get("upperTonnage") or 0.0) > 1e-9) or (float(r.get("lowerTonnage") or 0.0) > 1e-9)
        for r in order_rows
    )
    if has_model_split:
        upper_rows = [r for r in order_rows if float(r.get("upperTonnage") or 0.0) > 1e-9]
        lower_rows = [r for r in order_rows if float(r.get("lowerTonnage") or 0.0) > 1e-9]
        upper_rows.sort(key=lambda r: int(r["rollId"]))
        lower_rows.sort(key=lambda r: int(r["rollId"]))
        return upper_rows, lower_rows

    sorted_rows = sorted(order_rows, key=lambda r: float(r.get("tonnage") or 0.0), reverse=True)
    upper_rows: List[Dict] = []
    lower_rows: List[Dict] = []
    upper_sum = 0.0
    lower_sum = 0.0
    for row in sorted_rows:
        ton = float(row.get("tonnage") or 0.0)
        if upper_sum <= lower_sum:
            upper_rows.append(row)
            upper_sum += ton
        else:
            lower_rows.append(row)
            lower_sum += ton
    upper_rows.sort(key=lambda r: int(r["rollId"]))
    lower_rows.sort(key=lambda r: int(r["rollId"]))
    return upper_rows, lower_rows


def calculate_roll_change_and_sync_metrics(cutting_plan: List[Dict]) -> Dict[str, int]:
    """
    Kesim planından operatör odaklı rulo değişim ve üst/alt eşzamanlılık metriklerini üretir.

    Args:
        cutting_plan: Çözümden çıkan kesim planı satırları

    Returns:
        {"rollChangeCount": int, "surfaceSyncViolations": int}
    """
    by_order: Dict[int, List[Dict]] = defaultdict(list)
    for row in cutting_plan:
        by_order[int(row["orderId"])].append(row)

    roll_change_count = 0
    surface_sync_violations = 0
    for order_rows in by_order.values():
        upper_rows, lower_rows = _partition_order_slices_for_surfaces(order_rows)
        upper_changes = max(0, len(upper_rows) - 1)
        lower_changes = max(0, len(lower_rows) - 1)
        roll_change_count += upper_changes + lower_changes
        if len(upper_rows) != len(lower_rows):
            surface_sync_violations += abs(len(upper_rows) - len(lower_rows))

    return {
        "rollChangeCount": int(roll_change_count),
        "surfaceSyncViolations": int(surface_sync_violations),
    }


def build_line_events(line_schedule: List[Dict]) -> Tuple[List[Dict], Dict[str, int]]:
    """
    Adım çizelgesinden üst/alt hatlar için kronolojik rulo tak-çıkar ve sipariş geçiş olaylarını üretir.

    Args:
        line_schedule: build_line_schedule çıktısı

    Returns:
        (line_events, line_transition_summary)
    """
    line_events: List[Dict] = []
    prev_upper: Optional[int] = None
    prev_lower: Optional[int] = None
    prev_order: Optional[int] = None
    total_changes = 0
    synchronous_changes = 0
    independent_changes = 0
    for step_row in line_schedule:
        step = int(step_row["step"])
        oid = int(step_row["orderId"])
        upper_id = int(step_row["upperRollId"]) if step_row.get("upperRollId") is not None else None
        lower_id = int(step_row["lowerRollId"]) if step_row.get("lowerRollId") is not None else None

        upper_changed = upper_id != prev_upper
        lower_changed = lower_id != prev_lower
        if upper_changed or lower_changed:
            total_changes += int(upper_changed) + int(lower_changed)
            if upper_changed and lower_changed:
                synchronous_changes += 1
            else:
                independent_changes += 1

        def emit_single_line_event(line_name: str, prev_id: Optional[int], next_id: Optional[int]) -> None:
            """Tek hat için tek aktif rulo üstünden çıkar/tak/devam olayını üretir."""
            if prev_id is not None and prev_id != next_id:
                line_events.append({
                    "timestampStep": step,
                    "line": line_name,
                    "action": "cikar",
                    "rollId": prev_id,
                    "orderIdFrom": prev_order,
                    "orderIdTo": oid,
                })
            if next_id is not None and next_id != prev_id:
                line_events.append({
                    "timestampStep": step,
                    "line": line_name,
                    "action": "tak",
                    "rollId": next_id,
                    "orderIdFrom": prev_order,
                    "orderIdTo": oid,
                })
            if next_id is not None and next_id == prev_id:
                line_events.append({
                    "timestampStep": step,
                    "line": line_name,
                    "action": "devam",
                    "rollId": next_id,
                    "orderIdFrom": prev_order,
                    "orderIdTo": oid,
                })

        emit_single_line_event("ust", prev_upper, upper_id)
        emit_single_line_event("alt", prev_lower, lower_id)

        prev_upper = upper_id
        prev_lower = lower_id
        prev_order = oid

    summary = {
        "totalChanges": int(total_changes),
        "synchronousChanges": int(synchronous_changes),
        "independentChanges": int(independent_changes),
    }
    return line_events, summary


def build_line_schedule(cutting_plan: List[Dict]) -> List[Dict]:
    """
    Kesim planından rulo bazlı tek hat (üst+alt) adım çizelgesi üretir.

    Args:
        cutting_plan: Çözümden gelen satırlar

    Returns:
        [{"step": int, "upperRollId": int|null, "lowerRollId": int|null, "cuts": [...]}]
    """
    by_order: Dict[int, List[Dict]] = defaultdict(list)
    for row in cutting_plan:
        by_order[int(row["orderId"])].append(row)

    schedule: List[Dict] = []
    step = 0
    for oid in sorted(by_order.keys()):
        order_rows = by_order[oid]
        upper_rows, lower_rows = _partition_order_slices_for_surfaces(order_rows)
        max_len = max(len(upper_rows), len(lower_rows), 1)
        for idx in range(max_len):
            step += 1
            upper_row = upper_rows[idx] if idx < len(upper_rows) else None
            lower_row = lower_rows[idx] if idx < len(lower_rows) else None
            cuts = []
            if upper_row is not None:
                cuts.append({
                    "orderId": oid,
                    "rollId": int(upper_row["rollId"]),
                    "tonnage": float(upper_row.get("tonnage") or 0.0),
                    "m2": float(upper_row.get("m2") or 0.0),
                    "upperTonnage": float(upper_row.get("upperTonnage") or 0.0),
                    "lowerTonnage": float(upper_row.get("lowerTonnage") or 0.0),
                })
            if lower_row is not None:
                cuts.append({
                    "orderId": oid,
                    "rollId": int(lower_row["rollId"]),
                    "tonnage": float(lower_row.get("tonnage") or 0.0),
                    "m2": float(lower_row.get("m2") or 0.0),
                    "upperTonnage": float(lower_row.get("upperTonnage") or 0.0),
                    "lowerTonnage": float(lower_row.get("lowerTonnage") or 0.0),
                })
            schedule.append({
                "step": step,
                "orderId": oid,
                "upperRollId": int(upper_row["rollId"]) if upper_row is not None else None,
                "lowerRollId": int(lower_row["rollId"]) if lower_row is not None else None,
                "cuts": cuts,
            })
    return schedule


def _rows_to_surface_queue(rows: List[Dict], is_upper: bool) -> deque:
    """
    Kesim satırlarından (tek yüzey listesi) kuyruk üretir: her eleman rollId, ton, m2.

    Args:
        rows: Üst veya alt yüzey dilim satırları
        is_upper: True ise üst tonajı alınır

    Returns:
        collections.deque sözlükleri
    """
    q: deque = deque()
    for r in rows:
        row_ton = float(r.get("tonnage") or 0.0)
        if is_upper:
            t = float(r.get("upperTonnage") or 0.0)
            if t < 1e-9 and row_ton > 1e-9:
                t = row_ton
        else:
            t = float(r.get("lowerTonnage") or 0.0)
            if t < 1e-9 and row_ton > 1e-9:
                t = row_ton
        if t < 1e-9:
            continue
        row_m2 = float(r.get("m2") or 0.0)
        ton_kg = _ton_to_kg_int(t)
        if ton_kg <= 0:
            continue
        m2_alloc = row_m2 * (t / row_ton) if row_ton > 1e-9 else row_m2
        m2_per_kg = (m2_alloc / ton_kg) if ton_kg > 0 else 0.0
        q.append({
            "rollId": int(r["rollId"]),
            "ton": float(t),
            "tonKg": int(ton_kg),
            "m2": float(m2_alloc),
            "m2PerKg": float(m2_per_kg),
        })
    return q


def _segment_ton_for_upper_row(r: Dict) -> float:
    """
    Üst yüzey dilimi için segment tonajını döndürür (üst alan yoksa satır tonajına düşer).

    Args:
        r: Kesim planı satırı

    Returns:
        Ton cinsinden segment büyüklüğü
    """
    row_ton = float(r.get("tonnage") or 0.0)
    t = float(r.get("upperTonnage") or 0.0)
    if t < 1e-9 and row_ton > 1e-9:
        t = row_ton
    return float(t)


def _segment_ton_for_lower_row(r: Dict) -> float:
    """
    Alt yüzey dilimi için segment tonajını döndürür (alt alan yoksa satır tonajına düşer).

    Args:
        r: Kesim planı satırı

    Returns:
        Ton cinsinden segment büyüklüğü
    """
    row_ton = float(r.get("tonnage") or 0.0)
    t = float(r.get("lowerTonnage") or 0.0)
    if t < 1e-9 and row_ton > 1e-9:
        t = row_ton
    return float(t)


def _try_siki_ton_aligned_rows(
    upper_rows: List[Dict],
    lower_rows: List[Dict],
) -> Tuple[List[Dict], List[Dict]]:
    """
    Üst ve alt dilim ton çoklu kümeleri eşleşiyorsa satırları azalan tona göre hizalı sıraya çevirir.

    Aynı rank'taki dilimler eş ton olduğunda simetrik kuyruk tam dilim adımlarında biter;
    böylece üst ve alt rulo değişimleri aynı adımda hizalanır.

    Args:
        upper_rows: Üst yüzey satırları
        lower_rows: Alt yüzey satırları

    Returns:
        (üst_satırlar, alt_satırlar) — eşleşme yoksa orijinal listeler
    """
    if not upper_rows or not lower_rows or len(upper_rows) != len(lower_rows):
        return upper_rows, lower_rows
    u_tons = [_segment_ton_for_upper_row(r) for r in upper_rows]
    l_tons = [_segment_ton_for_lower_row(r) for r in lower_rows]
    u_sorted = sorted(u_tons, reverse=True)
    l_sorted = sorted(l_tons, reverse=True)
    tol = 0.05
    for i in range(len(u_sorted)):
        if abs(u_sorted[i] - l_sorted[i]) > tol:
            return upper_rows, lower_rows
    u_order = sorted(range(len(upper_rows)), key=lambda idx: -u_tons[idx])
    l_order = sorted(range(len(lower_rows)), key=lambda idx: -l_tons[idx])
    u_aligned = [upper_rows[i] for i in u_order]
    l_aligned = [lower_rows[i] for i in l_order]
    return u_aligned, l_aligned


def build_symmetric_steps_for_order(
    oid: int,
    order_rows: List[Dict],
    sync_level: str = "dengeli",
) -> List[Dict]:
    """
    Bir sipariş için üst/alt kuyrukları eş zamanlı tüketerek her adımda ust_ton == alt_ton olacak adımlar üretir.

    Args:
        oid: Sipariş kimliği
        order_rows: Bu siparişe ait kesim satırları
        sync_level: 'siki' iken ton çoklu kümeleri uyuyorsa dilim sırası hizalanır

    Returns:
        Operasyon sözlükleri (upperRollId, lowerRollId, cuts); boş ise simetrik üretilemedi
    """
    upper_rows, lower_rows = _partition_order_slices_for_surfaces(order_rows)
    if str(sync_level) == "siki":
        upper_rows, lower_rows = _try_siki_ton_aligned_rows(upper_rows, lower_rows)
    uq = _rows_to_surface_queue(upper_rows, True)
    lq = _rows_to_surface_queue(lower_rows, False)
    if not uq or not lq:
        return []
    sum_u = sum(float(x["ton"]) for x in uq)
    sum_l = sum(float(x["ton"]) for x in lq)
    if abs(sum_u - sum_l) > 0.05:
        logger.warning(
            "Siparis %s ust toplam %.4f ile alt toplam %.4f farkli; simetrik kuyruk riskli",
            oid,
            sum_u,
            sum_l,
        )
    steps: List[Dict] = []
    eps = 1e-9
    while uq and lq:
        u = uq[0]
        l = lq[0]
        u_kg = int(u.get("tonKg") or 0)
        l_kg = int(l.get("tonKg") or 0)
        step_kg = min(u_kg, l_kg)
        if step_kg <= 0:
            if u_kg <= 0:
                uq.popleft()
            if l_kg <= 0:
                lq.popleft()
            continue
        step_ton = _kg_int_to_ton(step_kg)
        u_m2_step = float(u["m2"]) if step_kg == u_kg else float(u["m2PerKg"]) * step_kg
        l_m2_step = float(l["m2"]) if step_kg == l_kg else float(l["m2PerKg"]) * step_kg
        cuts = [
            {
                "orderId": oid,
                "rollId": int(u["rollId"]),
                "tonnage": round(step_ton, 4),
                "m2": round(u_m2_step, 4),
                "upperTonnage": round(step_ton, 4),
                "lowerTonnage": 0.0,
            },
            {
                "orderId": oid,
                "rollId": int(l["rollId"]),
                "tonnage": round(step_ton, 4),
                "m2": round(l_m2_step, 4),
                "upperTonnage": 0.0,
                "lowerTonnage": round(step_ton, 4),
            },
        ]
        steps.append({
            "orderId": oid,
            "upperRollId": int(u["rollId"]),
            "lowerRollId": int(l["rollId"]),
            "cuts": cuts,
        })
        u["tonKg"] = u_kg - step_kg
        u["ton"] = _kg_int_to_ton(int(u["tonKg"]))
        u["m2"] = float(u["m2"]) - u_m2_step
        l["tonKg"] = l_kg - step_kg
        l["ton"] = _kg_int_to_ton(int(l["tonKg"]))
        l["m2"] = float(l["m2"]) - l_m2_step
        if float(u["ton"]) < eps or int(u["tonKg"]) <= 0:
            uq.popleft()
        if float(l["ton"]) < eps or int(l["tonKg"]) <= 0:
            lq.popleft()
    if uq or lq:
        logger.warning(
            "Siparis %s sonunda kuyruk kaldi (ust=%d alt=%d); toplam eslesmesi bozuk olabilir",
            oid,
            len(uq),
            len(lq),
        )
    return steps


def build_symmetric_ops_by_order(
    cutting_plan: List[Dict],
    sync_level: str = "dengeli",
) -> Dict[int, List[Dict]]:
    """
    Kesim planından sipariş başına simetrik (ust==alt ton) atomik operasyon listesi üretir.

    Args:
        cutting_plan: LP kesim planı
        sync_level: Hat senkron seviyesi (sıkı modda dilim hizalama denemesi)

    Returns:
        orderId -> operasyon listesi (sipariş içi sıra sabit). Herhangi bir siparişte simetrik
        üretim başarısızsa boş sözlük döner (çağıran yedek yola geçer).
    """
    by_order: Dict[int, List[Dict]] = defaultdict(list)
    for row in cutting_plan:
        by_order[int(row["orderId"])].append(row)
    out: Dict[int, List[Dict]] = {}
    for oid in sorted(by_order.keys()):
        steps = build_symmetric_steps_for_order(oid, by_order[oid], str(sync_level))
        if not steps:
            return {}
        out[oid] = steps
    return out


def _extract_legacy_index_pairing_ops(cutting_plan: List[Dict]) -> List[Dict]:
    """
    Eski indeks eşlemeli operasyon çıkarımı (simetrik üretim başarısız olursa yedek).

    Args:
        cutting_plan: Kesim planı

    Returns:
        Operasyon listesi
    """
    by_order: Dict[int, List[Dict]] = defaultdict(list)
    for row in cutting_plan:
        by_order[int(row["orderId"])].append(row)

    ops: List[Dict] = []
    for oid in sorted(by_order.keys()):
        order_rows = by_order[oid]
        upper_rows, lower_rows = _partition_order_slices_for_surfaces(order_rows)
        max_len = max(len(upper_rows), len(lower_rows), 1)
        for idx in range(max_len):
            upper_row = upper_rows[idx] if idx < len(upper_rows) else None
            lower_row = lower_rows[idx] if idx < len(lower_rows) else None
            if upper_row is None and lower_row is None:
                continue
            cuts: List[Dict] = []
            if upper_row is not None:
                ut = float(upper_row.get("upperTonnage") or 0.0) or float(upper_row.get("tonnage") or 0.0)
                um2 = float(upper_row.get("m2") or 0.0)
                row_ton = float(upper_row.get("tonnage") or 1.0)
                cuts.append({
                    "orderId": oid,
                    "rollId": int(upper_row["rollId"]),
                    "tonnage": round(ut, 4),
                    "m2": round(um2 * (ut / row_ton) if row_ton > 1e-9 else um2, 4),
                    "upperTonnage": round(float(upper_row.get("upperTonnage") or 0.0), 4),
                    "lowerTonnage": 0.0,
                })
            if lower_row is not None:
                lt = float(lower_row.get("lowerTonnage") or 0.0) or float(lower_row.get("tonnage") or 0.0)
                lm2 = float(lower_row.get("m2") or 0.0)
                tot = float(lower_row.get("tonnage") or 0.0)
                cuts.append({
                    "orderId": oid,
                    "rollId": int(lower_row["rollId"]),
                    "tonnage": round(lt, 4),
                    "m2": round(lm2 * (lt / tot) if tot > 1e-9 else lm2, 4),
                    "upperTonnage": 0.0,
                    "lowerTonnage": round(float(lower_row.get("lowerTonnage") or 0.0), 4),
                })
            ops.append({
                "orderId": oid,
                "upperRollId": int(upper_row["rollId"]) if upper_row is not None else None,
                "lowerRollId": int(lower_row["rollId"]) if lower_row is not None else None,
                "cuts": cuts,
            })
    return ops


def extract_atomic_operations(
    cutting_plan: List[Dict],
    sync_level: str = "dengeli",
) -> List[Dict]:
    """
    Kesim planından atomik hat adımlarını üretir (simetrik kuyruk; başarısızsa eski eşleme).

    Args:
        cutting_plan: Çözüm kesim planı satırları
        sync_level: Simetrik dilim üretiminde kullanılacak senkron seviyesi

    Returns:
        step numarasız operasyon sözlükleri listesi (sipariş ID sırasıyla birleştirilmiş)
    """
    by_o = build_symmetric_ops_by_order(cutting_plan, str(sync_level))
    if not by_o:
        return _extract_legacy_index_pairing_ops(cutting_plan)
    return [op for oid in sorted(by_o.keys()) for op in by_o[oid]]


def _operation_transition_cost(op_a: Dict, op_b: Dict, sync_level: str) -> float:
    """
    İki ardışık operasyon arası geçiş maliyetini hesaplar (rulo değişimi + sipariş cezası).

    Args:
        op_a: Önceki operasyon
        op_b: Sonraki operasyon
        sync_level: serbest | dengeli | siki

    Returns:
        Skaler maliyet (MILP amaç katsayısı)
    """
    ua = op_a.get("upperRollId")
    la = op_a.get("lowerRollId")
    ub = op_b.get("upperRollId")
    lb = op_b.get("lowerRollId")
    cu = 1.0 if ua != ub else 0.0
    cl = 1.0 if la != lb else 0.0
    base = cu + cl
    if sync_level == "dengeli":
        base += 0.35 * abs(cu - cl)
    elif sync_level == "siki":
        if cu != cl:
            base += 2.5
    order_penalty = 0.08 if int(op_a["orderId"]) != int(op_b["orderId"]) else 0.0
    return base + order_penalty


def _greedy_operation_order(ops: List[Dict], sync_level: str) -> List[int]:
    """
    Operasyon sırasını maliyeti azaltan açgözlü yöntemle üretir (MILP yedeği).

    Args:
        ops: Atomik operasyon listesi
        sync_level: Senkron seviye etiketi

    Returns:
        ops indekslerinin sırası
    """
    n = len(ops)
    if n <= 1:
        return list(range(n))
    remaining = set(range(n))
    # Başlangıç: toplam çıkış maliyeti en düşük düğüm
    best_start = min(remaining, key=lambda i: sum(_operation_transition_cost(ops[i], ops[j], sync_level) for j in remaining if j != i))
    order_indices = [best_start]
    remaining.remove(best_start)
    while remaining:
        last = order_indices[-1]
        nxt = min(remaining, key=lambda j: _operation_transition_cost(ops[last], ops[j], sync_level))
        order_indices.append(nxt)
        remaining.remove(nxt)
    return order_indices


def _solve_operation_order_milp(
    ops: List[Dict],
    sync_level: str,
    time_limit_seconds: int,
) -> Optional[List[int]]:
    """
    ATSP benzeri sıralamayı konum değişkenleriyle MILP olarak çözer.

    Args:
        ops: Atomik operasyonlar
        sync_level: Senkron seviye
        time_limit_seconds: CBC zaman sınırı

    Returns:
        Optimal permütasyon indeksleri veya çözülemezse None
    """
    n = len(ops)
    if n <= 1:
        return list(range(n))
    P = list(range(n))
    K = list(range(n))
    model = pulp.LpProblem("HatAdimiSirasi", pulp.LpMinimize)
    z = pulp.LpVariable.dicts("z", (P, K), cat="Binary")
    for k in K:
        model += pulp.lpSum(z[p][k] for p in P) == 1, f"op_at_most_once_{k}"
    for p in P:
        model += pulp.lpSum(z[p][k] for k in K) == 1, f"pos_filled_{p}"

    y_vars: Dict[Tuple[int, int, int], pulp.LpVariable] = {}
    obj_terms: List = []
    for p in range(n - 1):
        for k1 in K:
            for k2 in K:
                if k1 == k2:
                    continue
                yk = pulp.LpVariable(f"y_{p}_{k1}_{k2}", cat="Binary")
                y_vars[(p, k1, k2)] = yk
                model += yk <= z[p][k1], f"yk_le_z1_{p}_{k1}_{k2}"
                model += yk <= z[p + 1][k2], f"yk_le_z2_{p}_{k1}_{k2}"
                model += yk >= z[p][k1] + z[p + 1][k2] - 1, f"yk_ge_lin_{p}_{k1}_{k2}"
                c = _operation_transition_cost(ops[k1], ops[k2], sync_level)
                obj_terms.append(yk * c)
    model += pulp.lpSum(obj_terms), "Toplam_gecis"

    import multiprocessing

    cpu_count = multiprocessing.cpu_count()
    model.solve(
        pulp.PULP_CBC_CMD(
            msg=0,
            timeLimit=max(5, int(time_limit_seconds)),
            gapRel=0.02,
            threads=cpu_count,
        )
    )
    st = pulp.LpStatus[model.status]
    if st != "Optimal":
        return None
    perm: List[int] = [-1] * n
    for p in P:
        for k in K:
            if pulp.value(z[p][k]) is not None and pulp.value(z[p][k]) > 0.5:
                perm[p] = k
                break
    if len(perm) != n or set(perm) != set(range(n)):
        return None
    return perm


def _strip_internal_op_fields(op: Dict) -> Dict:
    """
    Zamanlama yardımcı alanlarını (alt çizgi ile başlayan anahtarlar) kaldırır.

    Args:
        op: Ham operasyon sözlüğü

    Returns:
        API ve enrich için uygun kopya
    """
    return {k: v for k, v in op.items() if not str(k).startswith("_")}


def _flatten_ops_with_precedence(
    by_o: Dict[int, List[Dict]],
) -> Tuple[List[Dict], List[Tuple[int, int]]]:
    """
    Sipariş zincirlerini tek düz listeye çevirir ve sipariş içi öncelik kenarlarını üretir.

    Args:
        by_o: orderId -> simetrik operasyon zinciri (iç sıra korunur)

    Returns:
        (düz_operasyonlar, (önceki_indeks, sonraki_indeks) öncelik listesi)
    """
    flat: List[Dict] = []
    prec: List[Tuple[int, int]] = []
    for oid in sorted(by_o.keys()):
        chain = by_o[oid]
        prev_idx: Optional[int] = None
        for sub_i, op in enumerate(chain):
            op2 = dict(op)
            idx = len(flat)
            op2["_flat_idx"] = idx
            op2["_sub_idx"] = sub_i
            op2["_chain_oid"] = oid
            flat.append(op2)
            if prev_idx is not None:
                prec.append((prev_idx, idx))
            prev_idx = idx
    return flat, prec


def _total_transition_cost(seq: List[int], ops: List[Dict], sync_level: str) -> float:
    """
    Verilen indeks dizisi için ardışık operasyon geçiş maliyetlerinin toplamını hesaplar.

    Args:
        seq: ops indekslerinin üretim sırası
        ops: Operasyon listesi
        sync_level: serbest | dengeli | siki

    Returns:
        Toplam geçiş maliyeti
    """
    if len(seq) <= 1:
        return 0.0
    total = 0.0
    for i in range(len(seq) - 1):
        total += _operation_transition_cost(ops[seq[i]], ops[seq[i + 1]], sync_level)
    return total


def _valid_precedence_order(seq: List[int], prec_edges: List[Tuple[int, int]]) -> bool:
    """
    Dizinin tüm öncelik kenarlarını (a, b) için a'dan önce b gelmesini sağlayıp sağlamadığını kontrol eder.

    Args:
        seq: Permütasyon (ops indeksleri)
        prec_edges: Öncelik kenarları

    Returns:
        Geçerliyse True
    """
    pos = {seq[i]: i for i in range(len(seq))}
    for a, b in prec_edges:
        if pos.get(a, -1) >= pos.get(b, -2):
            return False
    return True


def _greedy_precedence_operation_order(
    ops: List[Dict],
    prec_edges: List[Tuple[int, int]],
    sync_level: str,
) -> List[int]:
    """
    Öncelik kısıtlı DAG üzerinde açgözlü sıra üretir (MILP yedeği ve büyük n için).

    Args:
        ops: Düz operasyon listesi
        prec_edges: Sipariş içi zincir öncelikleri
        sync_level: Geçiş maliyeti ağırlığı

    Returns:
        ops indekslerinin üretim sırası
    """
    n = len(ops)
    if n <= 1:
        return list(range(n))
    succ: Dict[int, List[int]] = defaultdict(list)
    pred_count = [0] * n
    for a, b in prec_edges:
        succ[a].append(b)
        pred_count[b] += 1
    remaining = {i for i in range(n) if pred_count[i] == 0}
    if not remaining:
        return _greedy_operation_order(ops, sync_level)
    order_indices: List[int] = []
    first = min(
        remaining,
        key=lambda i: sum(
            _operation_transition_cost(ops[i], ops[j], sync_level) for j in remaining if j != i
        ),
    )
    order_indices.append(first)
    remaining.remove(first)
    for b in succ[first]:
        pred_count[b] -= 1
        if pred_count[b] == 0:
            remaining.add(b)
    while remaining:
        last = order_indices[-1]
        nxt = min(
            remaining,
            key=lambda j: _operation_transition_cost(ops[last], ops[j], sync_level),
        )
        order_indices.append(nxt)
        remaining.remove(nxt)
        for b in succ[nxt]:
            pred_count[b] -= 1
            if pred_count[b] == 0:
                remaining.add(b)
    if len(order_indices) < n:
        for i in range(n):
            if i not in order_indices:
                order_indices.append(i)
    return order_indices


def _solve_precedence_constrained_order_milp(
    ops: List[Dict],
    prec_edges: List[Tuple[int, int]],
    sync_level: str,
    time_limit_seconds: int,
) -> Optional[List[int]]:
    """
    Tüm operasyonların permütasyonunu konum değişkenleriyle çözer; sipariş içi öncelikleri doğrusal kısıtlar.

    Args:
        ops: Düz operasyon listesi
        prec_edges: pos[b] >= pos[a] + 1 kısıtları
        sync_level: Geçiş maliyeti
        time_limit_seconds: CBC süre sınırı

    Returns:
        Optimal indeks dizisi veya çözülemezse None
    """
    n = len(ops)
    if n <= 1:
        return list(range(n))
    P = list(range(n))
    K = list(range(n))
    model = pulp.LpProblem("HatAdimiOncelikli", pulp.LpMinimize)
    z = pulp.LpVariable.dicts("zp", (P, K), cat="Binary")
    for k in K:
        model += pulp.lpSum(z[p][k] for p in P) == 1, f"zp_row_{k}"
    for p in P:
        model += pulp.lpSum(z[p][k] for k in K) == 1, f"zp_col_{p}"

    pos_expr = {k: pulp.lpSum(p * z[p][k] for p in P) for k in K}
    for a, b in prec_edges:
        model += pos_expr[b] >= pos_expr[a] + 1, f"zp_prec_{a}_{b}"

    y_vars: Dict[Tuple[int, int, int], pulp.LpVariable] = {}
    obj_terms: List = []
    for p in range(n - 1):
        for k1 in K:
            for k2 in K:
                if k1 == k2:
                    continue
                yk = pulp.LpVariable(f"yp_{p}_{k1}_{k2}", cat="Binary")
                y_vars[(p, k1, k2)] = yk
                model += yk <= z[p][k1], f"yp_le1_{p}_{k1}_{k2}"
                model += yk <= z[p + 1][k2], f"yp_le2_{p}_{k1}_{k2}"
                model += yk >= z[p][k1] + z[p + 1][k2] - 1, f"yp_ge_{p}_{k1}_{k2}"
                c = _operation_transition_cost(ops[k1], ops[k2], sync_level)
                obj_terms.append(yk * c)
    model += pulp.lpSum(obj_terms), "zp_obj"

    import multiprocessing

    cpu_count = multiprocessing.cpu_count()
    model.solve(
        pulp.PULP_CBC_CMD(
            msg=0,
            timeLimit=max(5, int(time_limit_seconds)),
            gapRel=0.02,
            threads=cpu_count,
        )
    )
    st = pulp.LpStatus[model.status]
    if st != "Optimal":
        return None
    perm: List[int] = [-1] * n
    for p in P:
        for k in K:
            if pulp.value(z[p][k]) is not None and pulp.value(z[p][k]) > 0.5:
                perm[p] = k
                break
    if len(perm) != n or set(perm) != set(range(n)):
        return None
    return perm


def _improve_precedence_sequence_by_adjacent_swaps(
    seq: List[int],
    ops: List[Dict],
    prec_edges: List[Tuple[int, int]],
    sync_level: str,
    max_passes: int = 80,
) -> List[int]:
    """
    Önceliği bozmayan bitişik yer değiştirmelerle geçiş maliyetini iyileştirir (2-opt benzeri).

    Args:
        seq: Başlangıç permütasyonu (ops indeksleri)
        ops: Operasyon listesi
        prec_edges: Öncelik kenarları
        sync_level: Geçiş maliyeti
        max_passes: Dış döngü üst sınırı

    Returns:
        İyileştirilmiş permütasyon
    """
    best = seq[:]
    best_cost = _total_transition_cost(best, ops, sync_level)
    passes = 0
    improved = True
    while improved and passes < max_passes:
        improved = False
        passes += 1
        for i in range(len(best) - 1):
            cand = best[:]
            cand[i], cand[i + 1] = cand[i + 1], cand[i]
            if not _valid_precedence_order(cand, prec_edges):
                continue
            c = _total_transition_cost(cand, ops, sync_level)
            if c + 1e-9 < best_cost:
                best = cand
                best_cost = c
                improved = True
                break
    return best


def _greedy_path_from_cost_matrix(cost: List[List[float]]) -> List[int]:
    """
    Maliyet matrisinden açgözlü Hamilton yolu (sipariş sırası) üretir.

    Args:
        cost: cost[i][j] = i bloğundan sonra j bloğunun başına geçiş maliyeti

    Returns:
        0..n-1 indeks permütasyonu
    """
    n = len(cost)
    if n <= 1:
        return list(range(n))
    remaining = set(range(n))
    best_start = min(
        remaining,
        key=lambda i: sum(float(cost[i][j]) for j in remaining if j != i),
    )
    seq = [best_start]
    remaining.remove(best_start)
    while remaining:
        last = seq[-1]
        nxt = min(remaining, key=lambda j: float(cost[last][j]))
        seq.append(nxt)
        remaining.remove(nxt)
    return seq


def _solve_tsp_path_from_cost_matrix(
    cost: List[List[float]],
    time_limit_seconds: int,
) -> Optional[List[int]]:
    """
    Maliyet matrisi üzerinden sipariş blok sırası için MILP (konum ataması) çözer.

    Args:
        cost: n x n geçiş maliyetleri (diyagonal kullanılmaz)
        time_limit_seconds: CBC süre sınırı

    Returns:
        Optimal indeks dizisi veya çözülemezse None
    """
    n = len(cost)
    if n <= 1:
        return list(range(n))
    P = list(range(n))
    K = list(range(n))
    model = pulp.LpProblem("SiparisBlokSirasi", pulp.LpMinimize)
    z = pulp.LpVariable.dicts("zo", (P, K), cat="Binary")
    for k in K:
        model += pulp.lpSum(z[p][k] for p in P) == 1, f"zo_row_{k}"
    for p in P:
        model += pulp.lpSum(z[p][k] for k in K) == 1, f"zo_col_{p}"

    obj_terms: List = []
    for p in range(n - 1):
        for k1 in K:
            for k2 in K:
                if k1 == k2:
                    continue
                yk = pulp.LpVariable(f"yo_{p}_{k1}_{k2}", cat="Binary")
                model += yk <= z[p][k1], f"yo_le1_{p}_{k1}_{k2}"
                model += yk <= z[p + 1][k2], f"yo_le2_{p}_{k1}_{k2}"
                model += yk >= z[p][k1] + z[p + 1][k2] - 1, f"yo_ge_{p}_{k1}_{k2}"
                obj_terms.append(yk * float(cost[k1][k2]))
    model += pulp.lpSum(obj_terms), "zo_obj"

    import multiprocessing

    cpu_count = multiprocessing.cpu_count()
    model.solve(
        pulp.PULP_CBC_CMD(
            msg=0,
            timeLimit=max(5, int(time_limit_seconds)),
            gapRel=0.02,
            threads=cpu_count,
        )
    )
    st = pulp.LpStatus[model.status]
    if st != "Optimal":
        return None
    perm: List[int] = [-1] * n
    for p in P:
        for k in K:
            if pulp.value(z[p][k]) is not None and pulp.value(z[p][k]) > 0.5:
                perm[p] = k
                break
    if len(perm) != n or set(perm) != set(range(n)):
        return None
    return perm


def enrich_line_schedule_with_actions(ordered_ops: List[Dict]) -> List[Dict]:
    """
    Sıralı operasyonlara adım numarası ve operatör dostu aksiyon alanlarını ekler.

    Args:
        ordered_ops: Sırası kesinleşmiş operasyon sözlükleri (upperRollId, lowerRollId, orderId, cuts)

    Returns:
        API'ye uygun lineSchedule satırları
    """
    last_step_index_by_order: Dict[int, int] = {}
    for idx, _op in enumerate(ordered_ops):
        last_step_index_by_order[int(_op["orderId"])] = idx

    seen_orders: Set[int] = set()
    prev_upper: Optional[int] = None
    prev_lower: Optional[int] = None
    prev_order: Optional[int] = None
    out: List[Dict] = []

    def line_action(prev_id: Optional[int], new_id: Optional[int]) -> str:
        """Tek hat için kısa aksiyon kodu döner."""
        if prev_id == new_id:
            return "devam"
        if new_id is not None and (prev_id is None or prev_id != new_id):
            return "takildi"
        if prev_id is not None and new_id is None:
            return "cikarildi"
        return "devam"

    for step_idx, op in enumerate(ordered_ops, start=1):
        oid = int(op["orderId"])
        u = op.get("upperRollId")
        l = op.get("lowerRollId")
        ui = int(u) if u is not None else None
        li = int(l) if l is not None else None

        ua = line_action(prev_upper, ui)
        la = line_action(prev_lower, li)

        is_last_for_order = last_step_index_by_order.get(oid, -1) == step_idx - 1
        if is_last_for_order and prev_order is not None and oid == prev_order:
            oa = "tamamlandi"
        elif prev_order is None:
            oa = "basladi"
        elif oid == prev_order:
            oa = "devam"
        elif oid in seen_orders:
            oa = "geri_donus"
        else:
            oa = "degisti"

        parts: List[str] = []
        if prev_order is None:
            parts.append("Üretim başladı")
        if ua != "devam" or la != "devam":
            if ua == "takildi" and la == "takildi":
                parts.append("Üst + alt rulo takıldı")
            elif ua == "takildi":
                pu = prev_upper
                parts.append(f"Üst: R{ui} takıldı" + (f" (R{pu} çıkarıldı)" if pu is not None and pu != ui else ""))
            elif la == "takildi":
                pl = prev_lower
                parts.append(f"Alt: R{li} takıldı" + (f" (R{pl} çıkarıldı)" if pl is not None and pl != li else ""))
            elif ua == "cikarildi":
                parts.append(f"Üst rulo çıkarıldı (R{prev_upper})")
            elif la == "cikarildi":
                parts.append(f"Alt rulo çıkarıldı (R{prev_lower})")
            elif ua == "devam" and la == "takildi":
                pl = prev_lower
                parts.append(f"Alt: R{li} takıldı" + (f" (R{pl} çıkarıldı)" if pl is not None and pl != li else ""))
            elif la == "devam" and ua == "takildi":
                pu = prev_upper
                parts.append(f"Üst: R{ui} takıldı" + (f" (R{pu} çıkarıldı)" if pu is not None and pu != ui else ""))
        if prev_order is not None and oid != prev_order:
            if oa == "geri_donus":
                parts.append(f"Sipariş #{oid}'e geri dönüş")
            else:
                parts.append(f"Sipariş değişimi → #{oid}")

        if is_last_for_order:
            parts.append(f"Sipariş #{oid} bu fazda tamamlandı")

        summary = " · ".join([p for p in parts if p]) or "Devam"
        if summary == "Devam" and prev_order is not None and oid == prev_order and ua == "devam" and la == "devam":
            summary = "Aynı rulolarla üretim devam"

        row: Dict = {
            "step": step_idx,
            "orderId": oid,
            "upperRollId": ui,
            "lowerRollId": li,
            "upperAction": ua,
            "lowerAction": la,
            "orderAction": oa,
            "actionSummary": summary,
            "prevUpperRollId": prev_upper,
            "prevLowerRollId": prev_lower,
            "cuts": op.get("cuts") or [],
        }
        out.append(row)
        seen_orders.add(oid)
        prev_upper = ui
        prev_lower = li
        prev_order = oid
    return out


def schedule_production_steps(
    cutting_plan: List[Dict],
    sync_level: str = "dengeli",
    time_limit_seconds: int = 45,
) -> List[Dict]:
    """
    LP kesim planından hat adım çizelgesi üretir.

    Sipariş içi: üst/alt ton eşit simetrik kuyruk. Çoklu siparişte tüm atomik adımlar tek listede
    sıralanır; sipariş içi zincir sırası öncelik kısıtıyla korunur (A-B-A ara girme mümkün).

    Args:
        cutting_plan: Çözüm kesim planı
        sync_level: Hat senkron seviyesi (geçiş maliyeti ağırlığı)
        time_limit_seconds: MILP çözücü zaman sınırı

    Returns:
        lineSchedule API listesi (zengin aksiyon alanları dahil)
    """
    sl = str(sync_level)
    by_o = build_symmetric_ops_by_order(cutting_plan, sl)
    if not by_o:
        ops = _extract_legacy_index_pairing_ops(cutting_plan)
        if not ops:
            return []
        n = len(ops)
        milp_threshold = 22
        if n <= milp_threshold:
            solved = _solve_operation_order_milp(ops, sl, time_limit_seconds)
            perm = solved if solved is not None else _greedy_operation_order(ops, sl)
        else:
            perm = _greedy_operation_order(ops, sl)
        perm = _improve_precedence_sequence_by_adjacent_swaps(perm, ops, [], sl)
        ordered = [ops[i] for i in perm]
        return enrich_line_schedule_with_actions(ordered)

    oids = sorted(by_o.keys())
    if len(oids) == 1:
        chain = [_strip_internal_op_fields(op) for op in by_o[oids[0]]]
        return enrich_line_schedule_with_actions(chain)

    flat_ops, prec_edges = _flatten_ops_with_precedence(by_o)
    n = len(flat_ops)
    milp_threshold = 22
    if n <= milp_threshold:
        solved = _solve_precedence_constrained_order_milp(
            flat_ops, prec_edges, sl, time_limit_seconds
        )
        perm = (
            solved
            if solved is not None
            else _greedy_precedence_operation_order(flat_ops, prec_edges, sl)
        )
    else:
        perm = _greedy_precedence_operation_order(flat_ops, prec_edges, sl)
    perm = _improve_precedence_sequence_by_adjacent_swaps(perm, flat_ops, prec_edges, sl)
    ordered = [_strip_internal_op_fields(flat_ops[i]) for i in perm]
    return enrich_line_schedule_with_actions(ordered)


def solve_optimization(
    thickness: float,
    density: float,
    orders: List[Dict],
    panel_widths: List[float],
    rolls: List[float],
    max_orders_per_roll: int,
    max_rolls_per_order: int,
    fire_cost: float,
    setup_cost: float,
    stock_cost: float,
    time_limit_seconds: int = 120,
    panel_lengths: Optional[List[float]] = None,
    surface_factor: float = 1.0,
    require_dual_roll_allocation: bool = False,
    max_interleaving_orders: int = 2,
    interleaving_penalty_cost: float = 0.0,
    enforce_surface_sync: bool = False,
    sync_level: str = "dengeli",
    sync_penalty_weight: float = 0.0,
    roll_open_mask: Optional[Sequence[bool]] = None,
) -> Tuple[str, Optional[Dict]]:
    """
    Optimizasyon modelini çöz. Panel uzunluğu ile kesim: uzunluk ve katları (örn. 3m → 3*33+1 fire).
    surface_factor>=2 (çift yüzey): talep 2× ile hesaplanır; sipariş başına en az iki fiziksel rulo (w_u+w_l toplamı >= 2).
    Üst ve alt yüzey tonajı ayrı değişkenlerle modellenir: her sipariş j için sum_i x_u[i,j] = sum_i x_l[i,j] = D[j]/2
    (iki yüzey aynı m² → eşit metal). Aynı fiziksel rulo aynı sipariş için üst ve alt yüzeye aynı anda takılamaz:
    her (i,j) için w_u[i,j]+w_l[i,j] <= 1; dolayısıyla x_u[i,j] ve x_l[i,j]’den en fazla biri pozitif olabilir.
    require_dual_roll_allocation geri uyumluluk için yok sayılır.
    Siparişe dönüş sırası için araya giren sipariş üst sınırını aşan kesim sıralarına soft ceza eklenebilir.
    enforce_surface_sync=True ise çift yüzeyde her sipariş için üstte kullanılan rulo adedi ile altta kullanılan
    rulo adedi eşitlenir (sert kısıt).
    sync_level='dengeli' ise üst/alt bağımsız değişim farklarına ceza eklenebilir.
    roll_open_mask: ``len(rolls)`` uzunluğunda dizi; ``False`` olan indeksteki rulolar açılamaz
    (``y[i]=0``). Karşılaştırma testlerinde yalnızca 6 t veya yalnızca 6,4 t aileleri için kullanılır.
    
    Returns:
        (Status, Results dictionary veya None)
    """
    if roll_open_mask is not None:
        if len(roll_open_mask) != len(rolls):
            raise ValueError("roll_open_mask uzunluğu rolls ile aynı olmalıdır.")
    if panel_lengths is None:
        panel_lengths = [1.0] * len(orders)
    elif len(panel_lengths) != len(orders):
        panel_lengths = panel_lengths + [1.0] * (len(orders) - len(panel_lengths))
    # Kümeler
    I = list(range(len(rolls)))
    J = list(range(len(orders)))
    
    # Rulo tonajları
    S = {i: rolls[i] for i in I}
    
    # Talep miktarları (tam sayı panel: m² / (genişlik * uzunluk))
    talep_m2 = {}
    for j in J:
        pw = panel_widths[j]
        pl = panel_lengths[j] if j < len(panel_lengths) else 1.0
        if pl <= 0:
            pl = 1.0
        panel_count = max(1, round(orders[j]['m2'] / (pw * pl))) if (pw * pl) > 0 else 1
        talep_m2[j] = panel_count * pw * pl * max(1.0, float(surface_factor))
    D = {j: round(talep_m2[j] * (thickness / 1000) * density, 4) for j in J}
    dual_surface = float(surface_factor) >= 2.0 - 1e-12
    D_half = {j: D[j] / 2.0 for j in J} if dual_surface else {}

    # Model oluştur
    model = pulp.LpProblem("Kesme_Stoku_Optimizasyonu", pulp.LpMinimize)

    # Değişkenler: çift yüzeyde üst/alt akış ayrı; tek yüzeyde tek x_ij
    if dual_surface:
        x_u = pulp.LpVariable.dicts("x_u", [(i, j) for i in I for j in J], lowBound=0, cat="Continuous")
        x_l = pulp.LpVariable.dicts("x_l", [(i, j) for i in I for j in J], lowBound=0, cat="Continuous")
        sync_diff = pulp.LpVariable.dicts("sync_diff", J, lowBound=0, cat="Continuous")
    else:
        x = pulp.LpVariable.dicts("x", [(i, j) for i in I for j in J], lowBound=0, cat="Continuous")

    u = pulp.LpVariable.dicts("u", I, lowBound=0, cat='Continuous')
    R = pulp.LpVariable.dicts("R", I, lowBound=0, cat='Continuous')
    F = pulp.LpVariable.dicts("F", I, lowBound=0, cat='Continuous')
    b = pulp.LpVariable.dicts("b", I, cat='Binary')
    y = pulp.LpVariable.dicts("y", I, cat='Binary')
    # Çift yüzey: rulo–sipariş çifti ya üst ya alt yüzeye (ikisi birden değil); tek yüzeyde tek w_ij.
    if dual_surface:
        w_u = pulp.LpVariable.dicts("w_u", [(i, j) for i in I for j in J], cat='Binary')
        w_l = pulp.LpVariable.dicts("w_l", [(i, j) for i in I for j in J], cat='Binary')
    else:
        w = pulp.LpVariable.dicts("w", [(i, j) for i in I for j in J], cat='Binary')
    
    # Amaç fonksiyonu
    sync_penalty_term = 0
    if dual_surface and sync_level == "dengeli" and sync_penalty_weight > 0:
        sync_penalty_term = pulp.lpSum([sync_diff[j] * sync_penalty_weight for j in J])
    # Beraberlik kırma: kullanılmayan rulolarda R≈S ile h*R toplamı hangi çiftin açıldığından
    # bağımsız kalabiliyor; CBC rastgele yüksek indeksli ruloları seçebilir. Küçük ε ile düşük
    # rollId (indeks) tercih edilir — aynı toplam stok maliyetinde dar bobin (önce listelenen) öne geçer.
    roll_index_tie_eps = 1e-5
    roll_index_tie_term = roll_index_tie_eps * pulp.lpSum([(i + 1) * y[i] for i in I])
    # Parçalanma azaltıcı bağ cezası: siparişi gereksiz çok rulo-dilimine bölmeyi zayıf tie-break ile caydırır.
    # setup_cost rulonun açılmasını zaten fiyatlar; bu terim açık rulolar içinde "kaç farklı (rulo,sipariş) bağı"
    # oluştuğunu küçültür ve tam/az parçalı eşleşmeleri öne taşır.
    split_link_tie_eps = 1e-4
    if dual_surface:
        split_link_tie_term = split_link_tie_eps * pulp.lpSum(
            [w_u[(i, j)] + w_l[(i, j)] for i in I for j in J]
        )
    else:
        split_link_tie_term = split_link_tie_eps * pulp.lpSum([w[(i, j)] for i in I for j in J])
    model += (
        pulp.lpSum([F[i] * fire_cost for i in I]) +
        pulp.lpSum([R[i] * stock_cost for i in I]) +
        pulp.lpSum([y[i] * setup_cost for i in I]) +
        sync_penalty_term +
        roll_index_tie_term +
        split_link_tie_term,
        "Toplam_Maliyet",
    )
    
    # Talep kısıtı (çift yüzey: her yüzey tam D[j]/2)
    for j in J:
        if dual_surface:
            model += (
                pulp.lpSum([x_u[(i, j)] for i in I]) == D_half[j],
                f"Talep_Ust_Yuzey_{j}",
            )
            model += (
                pulp.lpSum([x_l[(i, j)] for i in I]) == D_half[j],
                f"Talep_Alt_Yuzey_{j}",
            )
        else:
            model += pulp.lpSum([x[(i, j)] for i in I]) == D[j], f"Talep_{j}"

    # Kapasite kısıtı
    for i in I:
        model += u[i] <= S[i], f"Kapasite_{i}"

    # Kullanım tanımı (rulo i üzerindeki toplam ton = tüm sipariş ve yüzeyler)
    for i in I:
        if dual_surface:
            model += (
                u[i]
                == pulp.lpSum([x_u[(i, j)] + x_l[(i, j)] for j in J]),
                f"Kullanim_{i}",
            )
        else:
            model += u[i] == pulp.lpSum([x[(i, j)] for j in J]), f"Kullanim_{i}"

    # Rulo kullanım tetikleyici
    for i in I:
        model += u[i] <= S[i] * y[i], f"Rulo_Kullanim_{i}"

    # Setup / min lot: çift yüzeyde üst ve alt ayrı w ile bağlanır; aynı (i,j)’de en fazla bir yüzey.
    min_demand = min(D.values()) if D else 0.5
    min_lot = max(0.01, min(0.5, min_demand / 2))
    for i in I:
        for j in J:
            if dual_surface:
                model += x_u[(i, j)] <= S[i] * w_u[(i, j)], f"Setup_U_{i}_{j}"
                model += x_l[(i, j)] <= S[i] * w_l[(i, j)], f"Setup_L_{i}_{j}"
                model += x_u[(i, j)] >= min_lot * w_u[(i, j)], f"Min_Lot_U_{i}_{j}"
                model += x_l[(i, j)] >= min_lot * w_l[(i, j)], f"Min_Lot_L_{i}_{j}"
                model += (
                    w_u[(i, j)] + w_l[(i, j)] <= 1,
                    f"Rulo_Siparis_Tek_Yuzey_{i}_{j}",
                )
            else:
                flow = x[(i, j)]
                model += flow <= S[i] * w[(i, j)], f"Setup_{i}_{j}"
                model += flow >= min_lot * w[(i, j)], f"Min_Lot_Size_{i}_{j}"

    # w / (w_u,w_l) ve y_i bağlantısı
    for i in I:
        for j in J:
            if dual_surface:
                model += w_u[(i, j)] <= y[i], f"W_U_Y_Link_{i}_{j}"
                model += w_l[(i, j)] <= y[i], f"W_L_Y_Link_{i}_{j}"
            else:
                model += w[(i, j)] <= y[i], f"W_Y_Link_{i}_{j}"

    # Rulo başına maksimum sipariş kısıtı (çift yüzeyde aynı sipariş iki yüzeyde sayılmaz: w_u+w_l <= 1)
    for i in I:
        if dual_surface:
            model += (
                pulp.lpSum([w_u[(i, j)] + w_l[(i, j)] for j in J]) <= max_orders_per_roll * y[i],
                f"Max_Siparis_Per_Rulo_{i}",
            )
        else:
            model += pulp.lpSum([w[(i, j)] for j in J]) <= max_orders_per_roll * y[i], f"Max_Siparis_Per_Rulo_{i}"

    # Sipariş başına maksimum rulo kısıtı
    for j in J:
        if dual_surface:
            model += (
                pulp.lpSum([w_u[(i, j)] + w_l[(i, j)] for i in I]) <= max_rolls_per_order,
                f"Max_Rulo_Per_Siparis_{j}",
            )
        else:
            model += pulp.lpSum([w[(i, j)] for i in I]) <= max_rolls_per_order, f"Max_Rulo_Per_Siparis_{j}"

    # Eşzamanlı mod: üst/alt rulo değişimlerinin aynı fazda olmasını desteklemek için
    # her siparişte üst ve alt yüzeyde kullanılan rulo adedini eşitler (sert kısıt).
    if dual_surface and enforce_surface_sync:
        for j in J:
            model += (
                pulp.lpSum([w_u[(i, j)] for i in I]) == pulp.lpSum([w_l[(i, j)] for i in I]),
                f"SurfaceSync_RollCount_{j}",
            )
    elif dual_surface and sync_level == "dengeli":
        for j in J:
            upper_count = pulp.lpSum([w_u[(i, j)] for i in I])
            lower_count = pulp.lpSum([w_l[(i, j)] for i in I])
            model += sync_diff[j] >= upper_count - lower_count, f"SyncDiff_Pos_{j}"
            model += sync_diff[j] >= lower_count - upper_count, f"SyncDiff_Neg_{j}"

    # Çift yüzey: sipariş başına en az iki fiziksel rulo (üst ve alt için farklı rulo–sipariş atamaları)
    if float(surface_factor) >= 2.0 - 1e-12 and len(I) >= 2:
        for j in J:
            model += (
                pulp.lpSum([w_u[(i, j)] + w_l[(i, j)] for i in I]) >= 2,
                f"DualSurface_MinTwoRolls_{j}",
            )
    elif float(surface_factor) >= 2.0 - 1e-12 and len(I) < 2:
        logger.warning(
            "surface_factor>=2 ama tek rulo var: sipariş başına en az iki rulo kısıtı uygulanamaz (Infeasible riski)."
        )

    # Denge kısıtı
    for i in I:
        model += R[i] + F[i] == S[i] - u[i], f"Denge_{i}"
    
    # Stok/Fire ayrımı: R[i] >= EPS*b[i] ile b=1 iken R en az EPS olmalı.
    # EPS=0.01 sabitken kalan (S-u) < 0.01 t olduğunda b=1 ile dengeli değil; çözücü b=0 zorlar,
    # küçük kalanı tamamen F'ye yazar — yüksek cf ile dar bobin (5,89 t) dalı pahalı görünür.
    s_min_cap = min(float(S[ii]) for ii in I) if I else 1.0
    EPS = max(1e-7, min(0.01, s_min_cap * 1e-6, min_demand * 1e-6))
    for i in I:
        model += R[i] <= S[i] * b[i], f"Stok_ust_{i}"
        model += R[i] >= EPS * b[i], f"Stok_alt_{i}"
        model += F[i] <= S[i] * (1 - b[i]), f"Fire_ust_{i}"
    # Açılmayan rulo (y=0): kesim firesi yok; bobin rafta → F=0, R=S−u ve h×R ile stok tutma (raporla uyum).
    for i in I:
        model += F[i] <= S[i] * y[i], f"Fire_sadece_acilan_ruloda_{i}"
    # Rapor 0,5 t kuralı ile aynı: kalan (S−u) ≤ 0,5 ise b=0 (tamamı fire maliyeti cf), aksi halde b=1 (tamamı h).
    # Açılmayan ruloda (1−y) ile kısıtlar gevşetilir; y=0 iken F=0 zaten R=S zorlar.
    s_max_cap = max((float(S[ii]) for ii in I), default=1.0)
    M_rem = 2.0 * s_max_cap + 10.0
    T_rem = float(MIN_STOCK_THRESHOLD_TON)
    eps_rem = 1e-4
    for i in I:
        rem_lin = S[i] - u[i]
        model += (
            rem_lin <= T_rem + M_rem * b[i] + M_rem * (1 - y[i]),
            f"Rem_fire_stok_esik_ust_{i}",
        )
        model += (
            rem_lin >= T_rem + eps_rem - M_rem * (1 - b[i]) - M_rem * (1 - y[i]),
            f"Rem_fire_stok_esik_alt_{i}",
        )

    if roll_open_mask is not None:
        for i in I:
            if not bool(roll_open_mask[i]):
                model += y[i] == 0, f"Rulo_acma_yasak_{i}"
    
    # Modeli çöz (main.py'deki optimize parametrelerle)
    import multiprocessing
    cpu_count = multiprocessing.cpu_count()
    model.solve(pulp.PULP_CBC_CMD(
        msg=0,
        timeLimit=time_limit_seconds,
        gapRel=0.05,  # %5 gap toleransı (optimal değil ama yeterince iyi)
        threads=cpu_count,  # Tüm CPU çekirdeklerini kullan
        options=['-maxNodes 50000']  # Maksimum node sayısını sınırla
    ))
    status = pulp.LpStatus[model.status]
    
    if status != 'Optimal':
        return status, None
    
    # Sonuçları çıkar (birim tonaj = bir panel: genişlik * uzunluk * kalınlık * yoğunluk)
    cutting_plan = []
    birim_tonaj = {
        j: (panel_widths[j] * (panel_lengths[j] if j < len(panel_lengths) else 1.0) * (thickness / 1000) * density)
        for j in J
    }
    rho = (thickness / 1000) * density
    for i in I:
        for j in J:
            if dual_surface:
                tu = float(pulp.value(x_u[(i, j)]) or 0.0)
                tl = float(pulp.value(x_l[(i, j)]) or 0.0)
                if tu <= 0.0001 and tl <= 0.0001:
                    continue
                miktar_ton = tu + tl
                miktar_m2 = miktar_ton / rho if rho > 0 else 0.0
                panel_count = int(round(miktar_ton / birim_tonaj[j])) if birim_tonaj[j] > 0 else 0
                pl = panel_lengths[j] if j < len(panel_lengths) else 1.0
                cutting_plan.append({
                    "rollId": i + 1,
                    "orderId": j + 1,
                    "panelCount": panel_count,
                    "panelWidth": panel_widths[j],
                    "panelLength": pl,
                    "tonnage": round(miktar_ton, 4),
                    "upperTonnage": round(tu, 4),
                    "lowerTonnage": round(tl, 4),
                    "m2": round(miktar_m2, 2),
                })
            else:
                if pulp.value(x[(i, j)]) > 0.0001:
                    miktar_ton = pulp.value(x[(i, j)])
                    miktar_m2 = miktar_ton / rho if rho > 0 else 0.0
                    panel_count = int(round(miktar_ton / birim_tonaj[j])) if birim_tonaj[j] > 0 else 0
                    pl = panel_lengths[j] if j < len(panel_lengths) else 1.0
                    cutting_plan.append({
                        "rollId": i + 1,
                        "orderId": j + 1,
                        "panelCount": panel_count,
                        "panelWidth": panel_widths[j],
                        "panelLength": pl,
                        "tonnage": round(miktar_ton, 4),
                        "m2": round(miktar_m2, 2),
                    })

    max_int = max(0, int(max_interleaving_orders))
    pen_unit = float(interleaving_penalty_cost or 0.0)
    if pen_unit > 0:
        cutting_plan, roll_sequences = apply_sequence_local_improvement(
            cutting_plan, max_int, pen_unit
        )
    else:
        roll_sequences = build_roll_order_sequence(cutting_plan)

    sequence_penalty, sequence_violations = calculate_return_gap_penalty(
        roll_sequences, max_int, pen_unit
    )

    roll_order_sequences_json = {str(k): v for k, v in roll_sequences.items()}

    roll_status = []
    for i in I:
        kullanilan = pulp.value(u[i])
        fire = pulp.value(F[i])
        stok = pulp.value(R[i])
        if dual_surface:
            kullanilan_siparis = sum(
                1
                for j in J
                if float(pulp.value(x_u[(i, j)]) or 0.0) + float(pulp.value(x_l[(i, j)]) or 0.0)
                > 0.0001
            )
        else:
            kullanilan_siparis = sum([1 for j in J if pulp.value(x[(i, j)]) > 0.0001])
        
        roll_status.append({
            "rollId": i + 1,
            "totalTonnage": S[i],
            "used": round(kullanilan, 4),
            "remaining": round(S[i] - kullanilan, 4),
            "fire": round(fire, 4),
            "stock": round(stok, 4),
            "ordersUsed": kullanilan_siparis,
            "unusedRollTonnage": 0.0,
        })
    
    # Raporlama: bobin kapasitesi ve kalanlar kg tam sayı defterine yazılır (ton = kg/1000); 0,5 t eşiği kg ile.
    # LP'nin R/F/b seçimi burada yok sayılır — cf yüksekken küçük artığı "stok" göstermemek için.
    # Açılmamış rulo: üretim fire/stoku yok; bobin rafta → unusedRollTonnage = kapasite (kg defteri).
    toplam_fire_kg = 0
    toplam_stok_kg = 0
    toplam_eldeki_kg = 0
    for idx, i in enumerate(I):
        item = roll_status[idx]
        cap_kg = _ton_to_kg_int(float(S[i]))
        cap_ton = _kg_int_to_ton(cap_kg)
        y_val = float(pulp.value(y[i]) or 0.0)
        if y_val <= 0.5:
            item["totalTonnage"] = cap_ton
            item["used"] = 0.0
            item["remaining"] = cap_ton
            item["stock"] = 0.0
            item["fire"] = 0.0
            item["unusedRollTonnage"] = cap_ton
            toplam_eldeki_kg += cap_kg
            continue
        used_val = float(pulp.value(u[i]) or 0.0)
        used_kg = min(cap_kg, max(0, _ton_to_kg_int(used_val)))
        rem_kg = cap_kg - used_kg
        fire_kg, stock_kg = _split_remainder_kg(rem_kg)
        used_kg = cap_kg - fire_kg - stock_kg
        item["totalTonnage"] = cap_ton
        item["unusedRollTonnage"] = 0.0
        item["used"] = _kg_int_to_ton(used_kg)
        item["fire"] = _kg_int_to_ton(fire_kg)
        item["stock"] = _kg_int_to_ton(stock_kg)
        item["remaining"] = _kg_int_to_ton(fire_kg + stock_kg)
        toplam_fire_kg += fire_kg
        toplam_stok_kg += stock_kg

    toplam_fire = _kg_int_to_ton(toplam_fire_kg)
    toplam_stok = _kg_int_to_ton(toplam_stok_kg)
    toplam_eldeki = _kg_int_to_ton(toplam_eldeki_kg)

    acilan_rulo = sum([1 for i in I if pulp.value(y[i]) > 0.5])
    # Stok tutma (h): hem açılmış rulodaki üretim stoğu (kalan > 0,5 t) hem açılmamış rafta bobin (elde).
    toplam_stok_tutma_ton = toplam_stok + toplam_eldeki
    cost_fire_lira = round(toplam_fire * fire_cost, 2)
    cost_stock_lira = round(toplam_stok_tutma_ton * stock_cost, 2)
    cost_stock_production_lira = round(toplam_stok * stock_cost, 2)
    cost_stock_shelf_lira = max(0.0, round(cost_stock_lira - cost_stock_production_lira, 2))
    cost_setup_lira = round(acilan_rulo * setup_cost, 2)
    cost_sequence_penalty_lira = round(sequence_penalty, 2)
    # Fire/stok yeniden sınıflandığı için maliyeti buna göre güncelle (TL satırlarıyla tutarlı).
    guncel_maliyet = round(
        float(cost_fire_lira)
        + float(cost_stock_lira)
        + float(cost_setup_lira)
        + float(cost_sequence_penalty_lira),
        2,
    )

    summary = {
        "totalCost": round(guncel_maliyet, 2),
        "totalFire": toplam_fire,
        "totalStock": toplam_stok,
        "totalUnusedInventoryTon": toplam_eldeki,
        "totalStockHoldingTon": round(toplam_stok_tutma_ton, 6),
        "openedRolls": acilan_rulo,
        "sequencePenalty": round(sequence_penalty, 4),
        "interleavingViolationCount": len(sequence_violations),
        "costFireLira": cost_fire_lira,
        "costStockLira": cost_stock_lira,
        "costStockProductionLira": cost_stock_production_lira,
        "costStockShelfLira": cost_stock_shelf_lira,
        "costSetupLira": cost_setup_lira,
        "costSequencePenaltyLira": cost_sequence_penalty_lira,
    }
    sync_metrics = calculate_roll_change_and_sync_metrics(cutting_plan)
    summary["rollChangeCount"] = sync_metrics["rollChangeCount"]
    summary["surfaceSyncViolations"] = sync_metrics["surfaceSyncViolations"]
    line_schedule = schedule_production_steps(cutting_plan, str(sync_level), time_limit_seconds)
    line_events, line_transitions_summary = build_line_events(line_schedule)
    line_transitions_summary["stepCount"] = len(line_schedule)

    lp_objective = round(pulp.value(model.objective), 2)
    results = {
        "status": status,
        "objective": round(lp_objective + sequence_penalty, 2),
        "summary": summary,
        "cuttingPlan": cutting_plan,
        "rollStatus": roll_status,
        "sequencePenalty": round(sequence_penalty, 4),
        "sequenceViolations": sequence_violations,
        "rollOrderSequences": roll_order_sequences_json,
        "rollChangeCount": sync_metrics["rollChangeCount"],
        "surfaceSyncViolations": sync_metrics["surfaceSyncViolations"],
        "lineEvents": line_events,
        "lineSchedule": line_schedule,
        "lineTransitionsSummary": line_transitions_summary,
        "syncLevel": sync_level,
        "model_vars": (
            {
                "x_u": x_u,
                "x_l": x_l,
                "u": u,
                "R": R,
                "F": F,
                "y": y,
                "w_u": w_u,
                "w_l": w_l,
            }
            if dual_surface
            else {
                "x": x,
                "u": u,
                "R": R,
                "F": F,
                "y": y,
                "w": w,
            }
        ),
        "data": {
            "I": I,
            "J": J,
            "S": S,
            "D": D,
            "panel_widths": panel_widths,
            "panel_lengths": panel_lengths,
            "orders": orders,
            "thickness": thickness,
            "density": density,
            "max_orders_per_roll": max_orders_per_roll,
            "fire_cost": fire_cost,
            "setup_cost": setup_cost,
            "stock_cost": stock_cost
        }
    }
    
    return status, results


def create_excel_report(
    results: Dict[str, Any],
    file_id: str,
    *,
    scenario_meta: Dict[str, Any],
) -> str:
    """
    Dashboard optimizasyonu için tez raporu şablonuyla Excel üretir: ayrıntılı sayfalar,
    üretim adımları ve gömülü grafikler (kesim şeması, üretim adımları, maliyet kırılımı).

    Args:
        results: solve_optimization çıktısı
        file_id: Çalıştırma dosya kimliği (dosya adı için)
        scenario_meta: thesis_xlsx_report.scenario_meta_from_dashboard_inputs çıktısı

    Returns:
        Yazılan .xlsx dosyasının tam yolu
    """
    from thesis_chart_builder import (
        kesim_semasi_from_results,
        stacked_bar_kirilim,
        uretim_adimlari_grafigi_from_results,
    )
    from thesis_xlsx_report import build_cozum_raporu_xlsx, sonuc_from_optimizer_results

    sonuc = sonuc_from_optimizer_results(results)
    sonuclar_klasoru = "sonuclar"
    if not os.path.exists(sonuclar_klasoru):
        os.makedirs(sonuclar_klasoru)

    grafik_klasor = os.path.join(sonuclar_klasoru, f"grafik_{file_id}")
    os.makedirs(grafik_klasor, exist_ok=True)

    baslik_orta = str(scenario_meta.get("senaryo_adi") or "Çözüm")
    alt_b = str(scenario_meta.get("aciklama") or "")

    kesim_png = kesim_semasi_from_results(
        results,
        os.path.join(grafik_klasor, "kesim_semasi.png"),
        baslik=f"Kesim Şeması — {baslik_orta}",
        alt_baslik=alt_b,
    )
    adim_png = uretim_adimlari_grafigi_from_results(
        results,
        os.path.join(grafik_klasor, "uretim_adimlari.png"),
        baslik=f"Üretim Adımları — {baslik_orta}",
        alt_baslik=alt_b,
    )

    maliyet_png: Optional[str] = None
    m = scenario_meta.get("maliyetler") or {}
    sm = results.get("summary") or {}
    tf = float(sm.get("totalFire", 0.0) or 0.0)
    ts = float(sm.get("totalStock", 0.0) or 0.0)
    opened = int(sm.get("openedRolls", 0) or 0)
    fc = float(m.get("fire_cost", 0) or 0)
    sc = float(m.get("stock_cost", 0) or 0)
    scu = float(m.get("setup_cost", 0) or 0)
    fm = tf * fc
    stm = ts * sc
    suma = opened * scu
    try:
        maliyet_png = stacked_bar_kirilim(
            ["Çözüm"],
            {"fire": [fm], "stok": [stm], "setup": [suma]},
            os.path.join(grafik_klasor, "maliyet_kirilim.png"),
            baslik="Maliyet kırılımı (fire + stok + rulo açma)",
            y_label="Maliyet",
            alt_baslik=alt_b,
        )
    except Exception as exc:
        logger.warning("Maliyet kırılım grafiği oluşturulamadı: %s", str(exc))

    embed = [p for p in (kesim_png, adim_png, maliyet_png) if p]
    excel_path = os.path.join(sonuclar_klasoru, f"cozum_raporu_{file_id}.xlsx")
    build_cozum_raporu_xlsx(
        scenario_meta,
        sonuc,
        excel_path,
        grafik_yollari=embed if embed else None,
    )
    return excel_path


def _chunk_rows(rows: List[List], chunk_size: int) -> List[List[List]]:
    """
    Tablo satırlarını PDF sayfalarına bölmek için parçalara ayırır.

    Args:
        rows: Bölünecek satır listesi
        chunk_size: Her sayfadaki satır sayısı

    Returns:
        Satır parçaları listesi
    """
    if chunk_size <= 0:
        return [rows]
    return [rows[i:i + chunk_size] for i in range(0, len(rows), chunk_size)]


def _build_roll_stacked_segments(results: Dict) -> Dict[int, Dict[str, List]]:
    """
    Rulo bazlı stacked bar grafiği için sipariş/stock/fire segmentlerini üretir.

    Args:
        results: Optimizasyon sonuç sözlüğü

    Returns:
        Rulo bazlı segment sözlüğü
    """
    cutting_plan = results.get("cuttingPlan", [])
    roll_status = results.get("rollStatus", [])
    segments: Dict[int, Dict[str, List]] = {}

    for roll in roll_status:
        roll_id = int(roll.get("rollId", 0) or 0)
        segments[roll_id] = {
            "order_ids": [],
            "tonnages": [],
            "stock": [float(roll.get("stock", 0) or 0)],
            "fire": [float(roll.get("fire", 0) or 0)],
        }

    for item in cutting_plan:
        roll_id = int(item.get("rollId", 0) or 0)
        if roll_id not in segments:
            segments[roll_id] = {"order_ids": [], "tonnages": [], "stock": [0.0], "fire": [0.0]}
        segments[roll_id]["order_ids"].append(int(item.get("orderId", 0) or 0))
        segments[roll_id]["tonnages"].append(float(item.get("tonnage", 0) or 0))

    return segments


def create_pdf_report(results: Dict, file_id: str) -> str:
    """
    Sonuç ekranındaki özet, tablolar ve grafiklerle PDF raporu oluşturur.

    Args:
        results: Optimizasyon sonuçları
        file_id: Dosya ID

    Returns:
        Üretilen PDF dosya yolu
    """
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    summary = results.get("summary", {})
    cutting_plan = results.get("cuttingPlan", [])
    roll_status = results.get("rollStatus", [])

    total_tonnage = sum(float(r.get("totalTonnage", 0) or 0) for r in roll_status)
    total_used = sum(float(r.get("used", 0) or 0) for r in roll_status)
    total_fire = sum(float(r.get("fire", 0) or 0) for r in roll_status)
    material_usage_pct = (total_used / total_tonnage * 100) if total_tonnage > 0 else 0.0
    fire_pct = (total_fire / total_tonnage * 100) if total_tonnage > 0 else 0.0

    sonuclar_klasoru = "sonuclar"
    if not os.path.exists(sonuclar_klasoru):
        os.makedirs(sonuclar_klasoru)
    pdf_path = os.path.join(sonuclar_klasoru, f"cozum_raporu_{file_id}.pdf")

    with PdfPages(pdf_path) as pdf:
        # SAYFA 1: Özet KPI + görsel metrikler
        fig1, axes = plt.subplots(2, 2, figsize=(11.69, 8.27))
        fig1.suptitle(f"Optimizasyon Sonuç Özeti - #{file_id}", fontsize=16, fontweight="bold")

        kpi_labels = ["Toplam Maliyet", "Toplam Fire (ton)", "Toplam Stok (ton)", "Açılan Rulo"]
        kpi_values = [
            f"{float(summary.get('totalCost', 0) or 0):,.2f} ₺",
            f"{float(summary.get('totalFire', 0) or 0):.4f}",
            f"{float(summary.get('totalStock', 0) or 0):.4f}",
            f"{int(summary.get('openedRolls', 0) or 0)}",
        ]
        for idx, ax in enumerate(axes.flatten()):
            ax.axis("off")
            ax.text(0.5, 0.62, kpi_labels[idx], ha="center", va="center", fontsize=12, color="#444")
            ax.text(0.5, 0.40, kpi_values[idx], ha="center", va="center", fontsize=18, fontweight="bold", color="#153b6a")
            ax.add_patch(plt.Rectangle((0.05, 0.05), 0.90, 0.90, fill=False, edgecolor="#d1d5db", linewidth=1.2, transform=ax.transAxes))

        fig1.text(0.5, 0.02, f"Malzeme Kullanımı: %{material_usage_pct:.2f} | Fire Faktörü: %{fire_pct:.2f}", ha="center", fontsize=10, color="#555")
        pdf.savefig(fig1, bbox_inches="tight")
        plt.close(fig1)

        # SAYFA 2+: Kesim Planı tabloları
        cutting_rows = [[
            int(i.get("orderId", 0) or 0),
            int(i.get("rollId", 0) or 0),
            int(i.get("panelCount", 0) or 0),
            f"{float(i.get('panelWidth', 0) or 0):.2f}",
            f"{float(i.get('tonnage', 0) or 0):.4f}",
            f"{float(i.get('m2', 0) or 0):.2f}",
        ] for i in cutting_plan]
        cutting_chunks = _chunk_rows(cutting_rows, 24)
        for page_idx, chunk in enumerate(cutting_chunks, start=1):
            fig, ax = plt.subplots(figsize=(11.69, 8.27))
            ax.axis("off")
            ax.set_title(f"Kesim Planı (Sayfa {page_idx}/{len(cutting_chunks)})", fontsize=14, fontweight="bold", pad=12)
            tbl = ax.table(
                cellText=chunk if chunk else [["-", "-", "-", "-", "-", "-"]],
                colLabels=["Sipariş", "Rulo", "Panel", "Panel Genişliği", "Tonaj", "m²"],
                loc="center",
                cellLoc="center",
            )
            tbl.auto_set_font_size(False)
            tbl.set_fontsize(9)
            tbl.scale(1, 1.3)
            pdf.savefig(fig, bbox_inches="tight")
            plt.close(fig)

        # SAYFA N: Rulo Durumu tablosu + stacked bar grafik
        fig3, (ax_table, ax_chart) = plt.subplots(2, 1, figsize=(11.69, 8.27), gridspec_kw={"height_ratios": [1.1, 1.6]})
        fig3.suptitle("Rulo Durumu ve Kullanım Şeması", fontsize=14, fontweight="bold")

        ax_table.axis("off")
        roll_rows = [[
            int(r.get("rollId", 0) or 0),
            f"{float(r.get('totalTonnage', 0) or 0):.2f}",
            f"{float(r.get('used', 0) or 0):.4f}",
            f"{float(r.get('remaining', 0) or 0):.4f}",
            f"{float(r.get('stock', 0) or 0):.4f}",
            f"{float(r.get('fire', 0) or 0):.4f}",
            int(r.get("ordersUsed", 0) or 0),
        ] for r in roll_status]
        tbl2 = ax_table.table(
            cellText=roll_rows if roll_rows else [["-", "-", "-", "-", "-", "-", "-"]],
            colLabels=["Rulo", "Toplam", "Kullanılan", "Kalan", "Stok", "Fire", "Sipariş"],
            loc="center",
            cellLoc="center",
        )
        tbl2.auto_set_font_size(False)
        tbl2.set_fontsize(9)
        tbl2.scale(1, 1.2)

        segments = _build_roll_stacked_segments(results)
        roll_ids = sorted(segments.keys())
        cmap = plt.get_cmap("tab20")
        for ridx, roll_id in enumerate(roll_ids):
            left = 0.0
            order_ids = segments[roll_id]["order_ids"]
            tonnages = segments[roll_id]["tonnages"]
            for seg_idx, ton in enumerate(tonnages):
                color = cmap((order_ids[seg_idx] % 20) / 20.0)
                ax_chart.barh(f"Rulo {roll_id}", ton, left=left, color=color, edgecolor="white", linewidth=0.3)
                left += ton
            stock_ton = float(segments[roll_id]["stock"][0] if segments[roll_id]["stock"] else 0.0)
            fire_ton = float(segments[roll_id]["fire"][0] if segments[roll_id]["fire"] else 0.0)
            if stock_ton > 0:
                ax_chart.barh(f"Rulo {roll_id}", stock_ton, left=left, color="#10b981", edgecolor="white", linewidth=0.3)
                left += stock_ton
            if fire_ton > 0:
                ax_chart.barh(f"Rulo {roll_id}", fire_ton, left=left, color="#ef4444", edgecolor="white", linewidth=0.3)

        ax_chart.set_xlabel("Tonaj")
        ax_chart.set_ylabel("Rulo")
        ax_chart.grid(axis="x", linestyle="--", alpha=0.3)
        pdf.savefig(fig3, bbox_inches="tight")
        plt.close(fig3)

    return pdf_path


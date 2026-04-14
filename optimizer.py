"""
Optimizasyon mantığı - Modüler fonksiyonlar
"""
# Bu eşiğin (ton) altındaki rulo artığı stok değil fire sayılır (iş kuralı).
MIN_STOCK_THRESHOLD_TON = 0.5

import pulp
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill
from datetime import datetime
import os
import random
from collections import defaultdict, deque
from typing import Dict, List, Tuple, Optional, Set
import logging
from pydantic import BaseModel

logger = logging.getLogger(__name__)


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
        m2_alloc = row_m2 * (t / row_ton) if row_ton > 1e-9 else row_m2
        q.append({
            "rollId": int(r["rollId"]),
            "ton": float(t),
            "m2": float(m2_alloc),
        })
    return q


def build_symmetric_steps_for_order(oid: int, order_rows: List[Dict]) -> List[Dict]:
    """
    Bir sipariş için üst/alt kuyrukları eş zamanlı tüketerek her adımda ust_ton == alt_ton olacak adımlar üretir.

    Args:
        oid: Sipariş kimliği
        order_rows: Bu siparişe ait kesim satırları

    Returns:
        Operasyon sözlükleri (upperRollId, lowerRollId, cuts); boş ise simetrik üretilemedi
    """
    upper_rows, lower_rows = _partition_order_slices_for_surfaces(order_rows)
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
    eps = 1e-6
    while uq and lq:
        u = uq[0]
        l = lq[0]
        step = min(float(u["ton"]), float(l["ton"]))
        if step < eps:
            if float(u["ton"]) < eps:
                uq.popleft()
            if float(l["ton"]) < eps:
                lq.popleft()
            continue
        u_ton = float(u["ton"])
        l_ton = float(l["ton"])
        u_m2_step = float(u["m2"]) * (step / u_ton) if u_ton > eps else 0.0
        l_m2_step = float(l["m2"]) * (step / l_ton) if l_ton > eps else 0.0
        cuts = [
            {
                "orderId": oid,
                "rollId": int(u["rollId"]),
                "tonnage": round(step, 4),
                "m2": round(u_m2_step, 4),
                "upperTonnage": round(step, 4),
                "lowerTonnage": 0.0,
            },
            {
                "orderId": oid,
                "rollId": int(l["rollId"]),
                "tonnage": round(step, 4),
                "m2": round(l_m2_step, 4),
                "upperTonnage": 0.0,
                "lowerTonnage": round(step, 4),
            },
        ]
        steps.append({
            "orderId": oid,
            "upperRollId": int(u["rollId"]),
            "lowerRollId": int(l["rollId"]),
            "cuts": cuts,
        })
        u["ton"] = float(u["ton"]) - step
        u["m2"] = float(u["m2"]) - u_m2_step
        l["ton"] = float(l["ton"]) - step
        l["m2"] = float(l["m2"]) - l_m2_step
        if float(u["ton"]) < eps:
            uq.popleft()
        if float(l["ton"]) < eps:
            lq.popleft()
    if uq or lq:
        logger.warning(
            "Siparis %s sonunda kuyruk kaldi (ust=%d alt=%d); toplam eslesmesi bozuk olabilir",
            oid,
            len(uq),
            len(lq),
        )
    return steps


def build_symmetric_ops_by_order(cutting_plan: List[Dict]) -> Dict[int, List[Dict]]:
    """
    Kesim planından sipariş başına simetrik (ust==alt ton) atomik operasyon listesi üretir.

    Args:
        cutting_plan: LP kesim planı

    Returns:
        orderId -> operasyon listesi (sipariş içi sıra sabit). Herhangi bir siparişte simetrik
        üretim başarısızsa boş sözlük döner (çağıran yedek yola geçer).
    """
    by_order: Dict[int, List[Dict]] = defaultdict(list)
    for row in cutting_plan:
        by_order[int(row["orderId"])].append(row)
    out: Dict[int, List[Dict]] = {}
    for oid in sorted(by_order.keys()):
        steps = build_symmetric_steps_for_order(oid, by_order[oid])
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


def extract_atomic_operations(cutting_plan: List[Dict]) -> List[Dict]:
    """
    Kesim planından atomik hat adımlarını üretir (simetrik kuyruk; başarısızsa eski eşleme).

    Args:
        cutting_plan: Çözüm kesim planı satırları

    Returns:
        step numarasız operasyon sözlükleri listesi (sipariş ID sırasıyla birleştirilmiş)
    """
    by_o = build_symmetric_ops_by_order(cutting_plan)
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

    Sipariş içi: üst/alt ton eşit simetrik kuyruk. Siparişler arası: MILP veya sezgisel blok sırası.

    Args:
        cutting_plan: Çözüm kesim planı
        sync_level: Hat senkron seviyesi (geçiş maliyeti ağırlığı)
        time_limit_seconds: MILP çözücü zaman sınırı

    Returns:
        lineSchedule API listesi (zengin aksiyon alanları dahil)
    """
    by_o = build_symmetric_ops_by_order(cutting_plan)
    if not by_o:
        ops = _extract_legacy_index_pairing_ops(cutting_plan)
        if not ops:
            return []
        n = len(ops)
        milp_threshold = 22
        if n <= milp_threshold:
            solved = _solve_operation_order_milp(ops, str(sync_level), time_limit_seconds)
            perm = solved if solved is not None else _greedy_operation_order(ops, str(sync_level))
        else:
            perm = _greedy_operation_order(ops, str(sync_level))
        ordered = [ops[i] for i in perm]
        return enrich_line_schedule_with_actions(ordered)

    oids = sorted(by_o.keys())
    if len(oids) == 1:
        return enrich_line_schedule_with_actions(by_o[oids[0]])

    k = len(oids)
    cost_mat: List[List[float]] = [[0.0] * k for _ in range(k)]
    for i, oi in enumerate(oids):
        for j, oj in enumerate(oids):
            if i == j:
                cost_mat[i][j] = 0.0
            else:
                last_op = by_o[oi][-1]
                first_op = by_o[oj][0]
                cost_mat[i][j] = _operation_transition_cost(last_op, first_op, str(sync_level))

    order_milp_limit = 14
    if k <= order_milp_limit:
        idx_perm = _solve_tsp_path_from_cost_matrix(cost_mat, time_limit_seconds)
        if idx_perm is None:
            idx_perm = _greedy_path_from_cost_matrix(cost_mat)
    else:
        idx_perm = _greedy_path_from_cost_matrix(cost_mat)

    flat: List[Dict] = []
    for idx in idx_perm:
        flat.extend(by_o[oids[idx]])
    return enrich_line_schedule_with_actions(flat)


def solve_optimization(
    thickness: float,
    density: float,
    orders: List[Dict],
    panel_widths: List[float],
    rolls: List[int],
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
    
    Returns:
        (Status, Results dictionary veya None)
    """
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
    model += (
        pulp.lpSum([F[i] * fire_cost for i in I]) +
        pulp.lpSum([R[i] * stock_cost for i in I]) +
        pulp.lpSum([y[i] * setup_cost for i in I]) +
        sync_penalty_term,
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
    
    # Stok/Fire ayrımı - gevşetilmiş
    # b_i = 1 ise "Stok" (parça > 0), b_i = 0 ise "Fire"
    # Fire üst sınırı kaldırıldı - daha fazla esneklik
    EPS = 0.01
    for i in I:
        model += R[i] <= S[i] * b[i], f"Stok_ust_{i}"
        model += R[i] >= EPS * b[i], f"Stok_alt_{i}"
        model += F[i] <= S[i] * (1 - b[i]), f"Fire_ust_{i}"
    
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
            "ordersUsed": kullanilan_siparis
        })
    
    # İş kuralı: Kalan miktar 0,5 ton üstüyse stoğa, 0,5 ton ve altı fire sayılır
    toplam_fire = 0.0
    toplam_stok = 0.0
    for item in roll_status:
        kalan = float(item["stock"]) + float(item["fire"])
        if kalan > MIN_STOCK_THRESHOLD_TON:
            item["stock"] = round(kalan, 4)
            item["fire"] = 0.0
            toplam_stok += kalan
        else:
            item["stock"] = 0.0
            item["fire"] = round(kalan, 4)
            toplam_fire += kalan
    
    acilan_rulo = sum([1 for i in I if pulp.value(y[i]) > 0.5])
    # Fire/stok yeniden sınıflandığı için maliyeti buna göre güncelle
    guncel_maliyet = (
        toplam_fire * fire_cost
        + toplam_stok * stock_cost
        + acilan_rulo * setup_cost
        + sequence_penalty
    )

    summary = {
        "totalCost": round(guncel_maliyet, 2),
        "totalFire": round(toplam_fire, 4),
        "totalStock": round(toplam_stok, 4),
        "openedRolls": acilan_rulo,
        "sequencePenalty": round(sequence_penalty, 4),
        "interleavingViolationCount": len(sequence_violations),
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


def create_excel_report(results: Dict, file_id: str) -> str:
    """
    Excel raporu oluştur
    
    Args:
        results: Optimizasyon sonuçları
        file_id: Dosya ID
    
    Returns:
        Dosya yolu
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill
    
    wb = Workbook()
    wb.remove(wb.active)
    
    data = results['data']
    I = data['I']
    J = data['J']
    S = data['S']
    D = data['D']
    panel_widths = data['panel_widths']
    orders = data['orders']
    n_orders = len(orders)
    panel_lengths = data.get('panel_lengths') or [1.0] * n_orders
    if len(panel_lengths) < n_orders:
        panel_lengths = panel_lengths + [1.0] * (n_orders - len(panel_lengths))
    thickness = data['thickness']
    density = data['density']

    # SAYFA 1: KULLANICI VERİ GİRİŞİ
    ws_veri = wb.create_sheet("Kullanici_Veri_Girisi", 0)
    ws_veri['A1'] = "KULLANICI VERİ GİRİŞİ"
    ws_veri['A1'].font = Font(bold=True, size=14)
    ws_veri.merge_cells('A1:B1')
    
    row = 3
    ws_veri.cell(row=row, column=1, value="MALZEME ÖZELLİKLERİ").font = Font(bold=True, size=12)
    ws_veri.merge_cells(f'A{row}:B{row}')
    row += 1
    ws_veri.cell(row=row, column=1, value="Kalınlık (mm)").font = Font(bold=True)
    ws_veri.cell(row=row, column=2, value=thickness)
    row += 1
    ws_veri.cell(row=row, column=1, value="Yoğunluk (g/cm³)").font = Font(bold=True)
    ws_veri.cell(row=row, column=2, value=density)
    row += 2
    
    ws_veri.cell(row=row, column=1, value="RULO TONAJLARI (Ton)").font = Font(bold=True, size=12)
    ws_veri.merge_cells(f'A{row}:B{row}')
    row += 1
    ws_veri.cell(row=row, column=1, value="Rulo No").font = Font(bold=True)
    ws_veri.cell(row=row, column=2, value="Tonaj (Ton)").font = Font(bold=True)
    row += 1
    for i in I:
        ws_veri.cell(row=row, column=1, value=f"Rulo {i+1}")
        ws_veri.cell(row=row, column=2, value=S[i])
        row += 1
    row += 1
    
    ws_veri.cell(row=row, column=1, value="SİPARİŞ MİKTARLARI (m²)").font = Font(bold=True, size=12)
    ws_veri.merge_cells(f'A{row}:D{row}')
    row += 1
    ws_veri.cell(row=row, column=1, value="Sipariş No").font = Font(bold=True)
    ws_veri.cell(row=row, column=2, value="Miktar (m²)").font = Font(bold=True)
    ws_veri.cell(row=row, column=3, value="Panel Genişliği (m)").font = Font(bold=True)
    ws_veri.cell(row=row, column=4, value="Panel Uzunluğu (m)").font = Font(bold=True)
    row += 1
    for j in J:
        ws_veri.cell(row=row, column=1, value=f"Sipariş {j+1}")
        ws_veri.cell(row=row, column=2, value=orders[j]['m2'])
        ws_veri.cell(row=row, column=3, value=panel_widths[j])
        ws_veri.cell(row=row, column=4, value=panel_lengths[j] if j < len(panel_lengths) else 1.0)
        row += 1
    
    ws_veri.column_dimensions['A'].width = 35
    ws_veri.column_dimensions['B'].width = 20
    ws_veri.column_dimensions['C'].width = 20
    ws_veri.column_dimensions['D'].width = 20
    
    # SAYFA 2: ÖZET
    ws_ozet = wb.create_sheet("Ozet")
    ws_ozet['A1'] = "KESME STOKU OPTİMİZASYON RAPORU - ÖZET"
    ws_ozet['A1'].font = Font(bold=True, size=14)
    ws_ozet.merge_cells('A1:B1')
    
    sum_row = results["summary"]
    seq_pen = float(sum_row.get("sequencePenalty", 0) or 0)
    viol_n = int(sum_row.get("interleavingViolationCount", 0) or 0)
    ozet_data = [
        ["Metrik", "Değer"],
        ["Toplam Maliyet", f"{sum_row['totalCost']:.2f}"],
        ["Sıra Cezası (siparişe dönüş)", f"{seq_pen:.4f}"],
        ["Sıra İhlal Sayısı", str(viol_n)],
        ["Toplam Fire (Ton)", f"{sum_row['totalFire']:.4f}"],
        ["Toplam Stok (Ton)", f"{sum_row['totalStock']:.4f}"],
        ["Açılan Rulo Sayısı", f"{sum_row['openedRolls']}"],
    ]
    
    for row_idx, row_data in enumerate(ozet_data, start=3):
        for col_idx, value in enumerate(row_data, start=1):
            cell = ws_ozet.cell(row=row_idx, column=col_idx, value=value)
            if row_idx == 3:
                cell.font = Font(bold=True)
                cell.fill = PatternFill(start_color="CCCCCC", end_color="CCCCCC", fill_type="solid")
    
    ws_ozet.column_dimensions['A'].width = 25
    ws_ozet.column_dimensions['B'].width = 20
    
    # SAYFA 3: KESİM PLANI
    ws_kesim = wb.create_sheet("Kesim_Plani")
    ws_kesim['A1'] = "KESİM PLANI"
    ws_kesim['A1'].font = Font(bold=True, size=14)
    ws_kesim.merge_cells('A1:H1')
    
    kesim_data = [["Sipariş ID", "Rulo ID", "Panel Sayısı", "Panel Genişliği (m)", "Panel Uzunluğu (m)",
                   "Kesilen Miktar (Ton)", "Kesilen Miktar (m²)", "Sipariş Toplam (Ton)"]]
    
    for item in results['cuttingPlan']:
        pl = item.get('panelLength', 1.0)
        kesim_data.append([
            f"Sipariş {item['orderId']}",
            f"Rulo {item['rollId']}",
            f"{item['panelCount']}",
            f"{item['panelWidth']:.2f}",
            f"{pl:.2f}",
            f"{item['tonnage']:.4f}",
            f"{item['m2']:.2f}",
            f"{D[item['orderId']-1]:.4f}"
        ])
    
    for row_idx, row_data in enumerate(kesim_data, start=3):
        for col_idx, value in enumerate(row_data, start=1):
            cell = ws_kesim.cell(row=row_idx, column=col_idx, value=value)
            if row_idx == 3:
                cell.font = Font(bold=True)
                cell.fill = PatternFill(start_color="CCCCCC", end_color="CCCCCC", fill_type="solid")
    
    for col in ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H']:
        ws_kesim.column_dimensions[col].width = 20
    
    # SAYFA 4: RULO DURUMU
    ws_rulo = wb.create_sheet("Rulo_Durumu")
    ws_rulo['A1'] = "RULO DURUMU"
    ws_rulo['A1'].font = Font(bold=True, size=14)
    ws_rulo.merge_cells('A1:H1')
    
    rulo_data = [["Rulo ID", "Başlangıç Kapasitesi (Ton)", "Kullanılan (Ton)", 
                  "Kalan (Ton)", "Stok (Ton)", "Fire (Ton)", "Kullanılan Sipariş Sayısı", "Durum"]]
    
    for item in results['rollStatus']:
        if item['fire'] > 0.0001:
            durum = "Fire"
        elif item['stock'] > 0.0001:
            durum = "Stok"
        elif item['used'] > 0.0001:
            durum = "Tamamen Kullanıldı"
        else:
            durum = "Kullanılmadı"
        
        rulo_data.append([
            f"Rulo {item['rollId']}",
            f"{item['totalTonnage']:.2f}",
            f"{item['used']:.4f}",
            f"{item['remaining']:.4f}",
            f"{item['stock']:.4f}",
            f"{item['fire']:.4f}",
            f"{item['ordersUsed']}/{data['max_orders_per_roll']}",
            durum
        ])
    
    for row_idx, row_data in enumerate(rulo_data, start=3):
        for col_idx, value in enumerate(row_data, start=1):
            cell = ws_rulo.cell(row=row_idx, column=col_idx, value=value)
            if row_idx == 3:
                cell.font = Font(bold=True)
                cell.fill = PatternFill(start_color="CCCCCC", end_color="CCCCCC", fill_type="solid")
    
    for col in ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H']:
        ws_rulo.column_dimensions[col].width = 20
    
    # Dosyayı kaydet
    sonuclar_klasoru = "sonuclar"
    if not os.path.exists(sonuclar_klasoru):
        os.makedirs(sonuclar_klasoru)
    
    excel_path = os.path.join(sonuclar_klasoru, f"cozum_raporu_{file_id}.xlsx")
    wb.save(excel_path)
    
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


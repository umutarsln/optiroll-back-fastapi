"""
Tez doğrulama: UI bağımsız `test_calistir` ve KDS (orta + dengeli) ile uyumlu çözüm yolu.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from optimizer import calculate_demand, solve_optimization
from thesis_failure_codes import (
    classify_infeasible_structure,
    classify_precheck,
    hints_for_code,
    merge_hints,
)
from thesis_kesim_rapor import build_kesim_snapshot, kesim_json_kisa

from main import (
    SURFACE_FACTOR_OPTIMIZE,
    CostsInput,
    MaterialInput,
    OptimizeRequest,
    OrderInput,
    RollSettingsInput,
    _build_mode_profile,
    _build_sync_profile,
)


def split_total_tonnage_to_n_rolls(total_tonnage: float, n: int) -> List[int]:
    """
    Toplam tonajı n fiziksel ruloya böler (tam sayı ton; her rulo en az 1 t).

    Args:
        total_tonnage: Toplam kapasite (ton)
        n: İstenen rulo sayısı (en az 2; toplam tamsayı tondan fazla olamaz)

    Returns:
        Uzunluğu n olan tonaj listesi
    """
    t = max(2, int(round(float(total_tonnage))))
    n_roll = max(2, min(int(n), t))
    base = t // n_roll
    rem = t % n_roll
    return [base + (1 if i < rem else 0) for i in range(n_roll)]


def split_total_tonnage_to_two_rolls(total_tonnage: float) -> List[int]:
    """
    Toplam rulo tonajını çift yüzey uyumu için iki fiziksel ruloya böler.

    Args:
        total_tonnage: Toplam kapasite (ton)

    Returns:
        İki tam sayı tonaj; toplamları toplam kapasiteye eşit (yuvarlanmış)
    """
    t = max(2, int(round(float(total_tonnage))))
    first = (t + 1) // 2
    second = t - first
    if second < 1:
        second = 1
        first = t - second
    return [first, second]


def split_total_tonnage_band(
    total_tonnage: float,
    *,
    min_ton: int = 4,
    max_ton: int = 13,
    seed: int = 42,
) -> List[int]:
    """
    Toplam tonajı min-max ton bandında (gerçek üretim rulosu) deterministik olarak
    rastgele rulolara böler. main/main.py'deki 4-10 ton mantığının genelleştirilmiş
    hâli; OFAT/tez senaryolarında çoklu fiziksel rulo üretimi için kullanılır.

    Kural:
      - Her rulo [min_ton, max_ton] tam sayı ton
      - Toplam tonaj hedefe yuvarlanmış olarak eşit
      - Kalan < min_ton ise önceki rulolara max_ton sınırında dağıtılır

    Args:
        total_tonnage: Toplam kapasite (ton); en az 2 olmalı
        min_ton: Alt sınır (varsayılan 4)
        max_ton: Üst sınır (varsayılan 13)
        seed: Deterministik random seed

    Returns:
        Rulo tonajları listesi (azalan sırada)
    """
    import random as _rnd

    rnd = _rnd.Random(int(seed))
    t = max(2, int(round(float(total_tonnage))))
    lo = max(1, int(min_ton))
    hi = max(lo, int(max_ton))
    rulolar: List[int] = []
    kalan = t
    while kalan > hi:
        v = rnd.randint(lo, hi)
        rulolar.append(v)
        kalan -= v
    if kalan > 0:
        if kalan < lo:
            dagitilacak = kalan
            idx = 0
            while dagitilacak > 0 and idx < len(rulolar):
                if rulolar[idx] < hi:
                    eklenecek = min(1, dagitilacak, hi - rulolar[idx])
                    rulolar[idx] += eklenecek
                    dagitilacak -= eklenecek
                idx += 1
            if dagitilacak > 0:
                rulolar.append(lo)
        elif lo <= kalan <= hi:
            rulolar.append(kalan)
        else:
            while kalan > hi:
                rulolar.append(hi)
                kalan -= hi
            if kalan >= lo:
                rulolar.append(kalan)
    if len(rulolar) < 2:
        # Çift yüzey için minimum 2 rulo zorunluluğu
        half = max(1, rulolar[0] // 2)
        rulolar = [rulolar[0] - half, half]
    rulolar.sort(reverse=True)
    return rulolar


def test_calistir(
    rulo_uzunlugu: float,
    musteri_talepleri: List[Dict[str, Any]],
    *,
    thickness_mm: float = 0.75,
    density_g_cm3: float = 7.85,
    fire_cost: float = 100.0,
    setup_cost: float = 50.0,
    stock_cost: float = 30.0,
    max_orders_per_roll: int = 6,
    max_rolls_per_order: int = 8,
    max_interleaving_orders: int = 2,
    interleaving_penalty_cost: float = 0.0,
    time_limit_seconds: int = 120,
    physical_roll_count: Optional[int] = None,
    rolls_override: Optional[List[int]] = None,
    rolls_band: Optional[Tuple[int, int]] = None,
    rolls_seed: int = 42,
) -> Dict[str, Any]:
    """
    Kesme optimizasyonunu KDS ile uyumlu parametrelerle çalıştırır (çift yüzey 2×, orta mod, dengeli senkron).

    Args:
        rulo_uzunlugu: Toplam rulo kapasitesi (ton); içeride iki veya physical_roll_count adet ruloya bölünür.
        musteri_talepleri: Sipariş listesi; her öğe m2, panelWidth ve isteğe bağlı panelLength içerir.
        thickness_mm: Malzeme kalınlığı (mm)
        density_g_cm3: Yoğunluk (g/cm³)
        fire_cost: Fire birim maliyeti
        setup_cost: Rulo açma (kurulum) birim maliyeti
        stock_cost: Stok birim maliyeti
        max_orders_per_roll: Rulo başına en fazla sipariş (istek gövdesi üst sınırı)
        max_rolls_per_order: Sipariş başına en fazla rulo
        max_interleaving_orders: Araya giren sipariş soft ceza eşiği
        interleaving_penalty_cost: Araya sipariş ceza birimi
        time_limit_seconds: CBC zaman sınırı
        physical_roll_count: None ise iki ruloya bölünür; 2–24 arası ise toplam tonaj bu sayıda fiziksel ruloya bölünür

    Returns:
        Sözlük: kullanilan_rulo_sayisi, rulo_degisim_sayisi (kesim dilimi), yuzey_es_zaman_ihlal_sayisi,
        uretim_hatti_rulo_gecis_sayisi (hat üst/alt rulo ID değişimi, totalChanges), eşzamanlı/bağımsız geçiş sayıları,
        toplam_fire, toplam_stok, toplam_maliyet, mesaj, phase, solver_status, failure_code, hints, raw (results)
    """
    orders_inputs: List[OrderInput] = []
    for o in musteri_talepleri:
        pl = float(o.get("panelLength", 1.0) or 1.0)
        orders_inputs.append(
            OrderInput(
                m2=float(o["m2"]),
                panelWidth=float(o["panelWidth"]),
                panelLength=pl,
            )
        )

    if rolls_override is not None:
        rolls_split = [int(x) for x in rolls_override if x and x > 0]
        if not rolls_split:
            rolls_split = split_total_tonnage_to_two_rolls(rulo_uzunlugu)
    elif rolls_band is not None:
        lo, hi = int(rolls_band[0]), int(rolls_band[1])
        rolls_split = split_total_tonnage_band(
            rulo_uzunlugu, min_ton=lo, max_ton=hi, seed=int(rolls_seed)
        )
    elif physical_roll_count is None:
        rolls_split = split_total_tonnage_to_two_rolls(rulo_uzunlugu)
    else:
        rolls_split = split_total_tonnage_to_n_rolls(rulo_uzunlugu, physical_roll_count)

    req = OptimizeRequest(
        material=MaterialInput(thickness=thickness_mm, density=density_g_cm3),
        orders=orders_inputs,
        rollSettings=RollSettingsInput(
            rolls=[float(x) for x in rolls_split],
            maxOrdersPerRoll=max_orders_per_roll,
            maxRollsPerOrder=max_rolls_per_order,
        ),
        costs=CostsInput(fireCost=fire_cost, setupCost=setup_cost, stockCost=stock_cost),
        maxInterleavingOrders=max_interleaving_orders,
        interleavingPenaltyCost=interleaving_penalty_cost,
        strategy_modes=None,
        sync_levels=["dengeli"],
    )

    panel_lengths = [float(o.panelLength or 1.0) for o in req.orders]
    panel_widths = [float(o.panelWidth) for o in req.orders]
    orders_list = [
        {
            "m2": float(o.m2),
            "panelWidth": float(o.panelWidth),
            "panelLength": panel_lengths[i],
        }
        for i, o in enumerate(req.orders)
    ]

    rolls_int = [int(round(x)) for x in (req.rollSettings.rolls or []) if x and x > 0]
    if not rolls_int:
        rolls_int = [int(x) for x in rolls_split]

    D, total_tonnage_needed = calculate_demand(
        orders_list,
        req.material.thickness,
        req.material.density,
        panel_widths=panel_widths,
        panel_lengths=panel_lengths,
        surface_factor=SURFACE_FACTOR_OPTIMIZE,
    )
    total_roll_tonnage = sum(rolls_int)

    sync_prof = _build_sync_profile("dengeli")
    mode_prof = _build_mode_profile("orta", req)

    perr, phints = classify_precheck(
        orders=orders_list,
        thickness=req.material.thickness,
        density=req.material.density,
        rolls=rolls_int,
        max_orders_per_roll=int(req.rollSettings.maxOrdersPerRoll),
        max_rolls_per_order=int(req.rollSettings.maxRollsPerOrder),
        total_tonnage_needed=float(sum(D.values())),
        total_roll_tonnage=total_roll_tonnage,
        surface_factor=SURFACE_FACTOR_OPTIMIZE,
    )

    if perr:
        return _finalize_response(
            kullanilan_rulo_sayisi=0,
            toplam_fire=0.0,
            toplam_stok=0.0,
            toplam_maliyet=0.0,
            mesaj=f"Ön kontrol: {perr}",
            phase="precheck",
            solver_status=None,
            failure_code=perr,
            hints=phints,
            raw={"precheck_failed": True, "demand_by_order": D},
            demand_by_order=D,
            rolls_int=rolls_int,
            results=None,
        )

    status, results = solve_optimization(
        thickness=req.material.thickness,
        density=req.material.density,
        orders=orders_list,
        panel_widths=panel_widths,
        panel_lengths=panel_lengths,
        rolls=rolls_int,
        max_orders_per_roll=int(mode_prof["max_orders_per_roll"]),
        max_rolls_per_order=int(mode_prof["max_rolls_per_order"]),
        fire_cost=float(mode_prof["fire_cost"]),
        setup_cost=float(mode_prof["setup_cost"]),
        stock_cost=float(mode_prof["stock_cost"]),
        time_limit_seconds=time_limit_seconds,
        surface_factor=SURFACE_FACTOR_OPTIMIZE,
        require_dual_roll_allocation=False,
        max_interleaving_orders=int(mode_prof["max_interleaving_orders"]),
        interleaving_penalty_cost=float(mode_prof["interleaving_penalty_cost"]),
        enforce_surface_sync=bool(sync_prof["enforce_surface_sync"]),
        sync_level=str(sync_prof["sync_level"]),
        sync_penalty_weight=float(sync_prof["sync_penalty_weight"]),
    )

    if status == "Optimal" and results is not None:
        summary = results.get("summary") or {}
        opened = int(summary.get("openedRolls", 0) or 0)
        tf = float(summary.get("totalFire", 0.0) or 0.0)
        ts = float(summary.get("totalStock", 0.0) or 0.0)
        tc = float(summary.get("totalCost", 0.0) or 0.0)
        return _finalize_response(
            kullanilan_rulo_sayisi=opened,
            toplam_fire=tf,
            toplam_stok=ts,
            toplam_maliyet=tc,
            mesaj="Optimal çözüm bulundu.",
            phase="solver",
            solver_status=status,
            failure_code=None,
            hints=[],
            raw={"results": results, "demand_by_order": D},
            demand_by_order=D,
            rolls_int=rolls_int,
            results=results,
        )

    code, ihints = classify_infeasible_structure(
        rolls=rolls_int,
        num_orders=len(orders_list),
        max_orders_per_roll=int(mode_prof["max_orders_per_roll"]),
        max_rolls_per_order=int(mode_prof["max_rolls_per_order"]),
        surface_factor=SURFACE_FACTOR_OPTIMIZE,
        enforce_surface_sync=bool(sync_prof["enforce_surface_sync"]),
    )
    return _finalize_response(
        kullanilan_rulo_sayisi=0,
        toplam_fire=0.0,
        toplam_stok=0.0,
        toplam_maliyet=0.0,
        mesaj=f"Çözücü durumu: {status}",
        phase="solver",
        solver_status=status,
        failure_code=code,
        hints=merge_hints(ihints, hints_for_code(code)),
        raw={"results": results, "demand_by_order": D, "solver_status": status},
        demand_by_order=D,
        rolls_int=rolls_int,
        results=results,
    )


def _finalize_response(
    *,
    kullanilan_rulo_sayisi: int,
    toplam_fire: float,
    toplam_stok: float,
    toplam_maliyet: float,
    mesaj: str,
    phase: str,
    solver_status: Optional[str],
    failure_code: Optional[str],
    hints: List[str],
    raw: Dict[str, Any],
    demand_by_order: Optional[Dict[int, float]] = None,
    rolls_int: Optional[List[int]] = None,
    results: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    test_calistir dönüş sözlüğünü tek formatta oluşturur.

    Args:
        kullanilan_rulo_sayisi: Açılan rulo sayısı
        toplam_fire: Toplam fire (ton)
        toplam_stok: Toplam stok (ton)
        toplam_maliyet: Özet toplam maliyet
        mesaj: Kısa açıklama
        phase: precheck veya solver
        solver_status: PuLP durumu veya None
        failure_code: Makine kodu veya None
        hints: Türkçe ipuçları
        raw: Ham ek veri
        demand_by_order: Sipariş başına talep tonu (kesim özeti için)
        rolls_int: Rulo kapasite ton listesi
        results: Çözücü sonuç sözlüğü (kesim planı için)

    Returns:
        Dış API ile uyumlu sözlük; `rulo_degisim_sayisi` çözücü `rollChangeCount`, `yuzey_es_zaman_ihlal_sayisi` yüzey eşzamanlılık ihlali
    """
    out: Dict[str, Any] = {
        "kullanilan_rulo_sayisi": kullanilan_rulo_sayisi,
        "toplam_fire": round(toplam_fire, 4),
        "toplam_stok": round(toplam_stok, 4),
        "toplam_maliyet": round(toplam_maliyet, 2),
        "mesaj": mesaj,
        "phase": phase,
        "solver_status": solver_status,
        "failure_code": failure_code,
        "hints": hints,
        "raw": raw,
    }
    rulo_deg = 0
    sync_ihlal = 0
    hat_gecis = 0
    hat_es = 0
    hat_bag = 0
    if results is not None:
        sm = results.get("summary") or {}
        rulo_deg = int(sm.get("rollChangeCount", 0) or 0)
        sync_ihlal = int(sm.get("surfaceSyncViolations", 0) or 0)
        lts = results.get("lineTransitionsSummary") or {}
        hat_gecis = int(lts.get("totalChanges", 0) or 0)
        hat_es = int(lts.get("synchronousChanges", 0) or 0)
        hat_bag = int(lts.get("independentChanges", 0) or 0)
    out["rulo_degisim_sayisi"] = rulo_deg
    out["yuzey_es_zaman_ihlal_sayisi"] = sync_ihlal
    out["uretim_hatti_rulo_gecis_sayisi"] = hat_gecis
    out["uretim_hatti_es_zamanli_gecis_sayisi"] = hat_es
    out["uretim_hatti_bagimsiz_gecis_sayisi"] = hat_bag
    if demand_by_order is not None and rolls_int is not None:
        snap = build_kesim_snapshot(demand_by_order, rolls_int, results)
        raw["kesim_snapshot"] = snap
        out["toplam_talep_ton"] = snap.get("toplam_talep_ton")
        out["toplam_rulo_kapasitesi_ton"] = snap.get("toplam_rulo_kapasitesi_ton")
        out["rulo_kapasiteleri_str"] = snap.get("rulo_kapasiteleri_str")
        out["kesim_senaryosu_metni"] = snap.get("kesim_senaryosu_metni", "")
        out["kesim_detay_json"] = kesim_json_kisa(snap)
    return out

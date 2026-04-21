"""
OFAT duyarlılık analizi: dalga bazlı tek eksen değişimi + suite klasör yapısı.

Her eksen için ayrı klasör: `test_<N>_<axis>_<aralik>/` altında
 - `ofat_rapor.xlsx` (noktalar + Grafikler)
 - `rapor.md`
 - `metrikler.csv`
 - `axis_noktalari/<carpan_X>/cozum_raporu.xlsx` (her nokta için 5 sayfalık tam rapor)
 - `grafikler/` (fire, stok, maliyet, rulo, kapasite line)

Suite kökünde `_karsilastirma/` altına tüm eksenlerin özet XLSX'i ve normalize trend grafiklerini yazar.
"""

from __future__ import annotations

import argparse
import os
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from thesis_chart_builder import (
    kesim_semasi_from_results,
    ofat_eksen_line_grafikleri,
    ofat_eksenler_normalize,
    uretim_adimlari_grafigi_from_results,
)
from thesis_kesim_rapor import (
    ofat_delta_vs_referans_satir,
    ofat_ne_degisti_aciklamasi,
)
from thesis_ofat_baseline import (
    DEFAULT_FIRE_COST,
    DEFAULT_MAX_ORDERS_PER_ROLL,
    DEFAULT_MAX_ROLLS_PER_ORDER,
    DEFAULT_ROLLS_BAND,
    DEFAULT_ROLLS_SEED,
    DEFAULT_SETUP_COST,
    DEFAULT_STOCK_COST,
    DEFAULT_TOTAL_ROLL_TONNAGE,
    OFAT_BASELINE_ACIKLAMA,
    OFAT_COST_AXIS_MULTS,
    OFAT_DEFAULT_ORDER_COUNT,
    OFAT_DEMAND_SCALE,
    baseline_costs,
    baseline_orders_multi,
)
from thesis_report_common import (
    axis_deger_klasoru,
    baseline_ozet_md_yaz,
    coklu_satir_csv_yaz,
    index_md_yaz,
    karsilastirma_klasoru_hazirla,
    karsilastirma_md_yaz,
    rapor_md_yaz,
    safe_slug,
    senaryo_klasoru_hazirla,
    simdi_ts,
    suite_kok_olustur,
)
from thesis_test_harness import split_total_tonnage_to_two_rolls, test_calistir
from thesis_xlsx_report import build_cozum_raporu_xlsx, karsilastirma_xlsx, scenario_meta_from_test_calistir

LEVELS_COST_MULT: Sequence[float] = list(OFAT_COST_AXIS_MULTS)
LEVELS_DEMAND_MULT: Sequence[float] = [0.8, 1.0, 1.2]
# Baseline 85 t; alt/üst noktalar yeter kapasite / dar kapasite senaryolarını temsil eder
# (4-13 bandı ile baseline'da ≈10 rulo; 60 t → 7-8 rulo, 120 t → 14-15 rulo)
LEVELS_ROLL_TOTAL_TONS: Sequence[float] = [60.0, 85.0, 120.0]
LEVELS_MAX_ORDERS_PER_ROLL: Sequence[int] = [6, 8, 10]
LEVELS_MAX_ROLLS_PER_ORDER: Sequence[int] = [2, 4, 8]


def _ofat_baseline_orders(scale_m2: float = 1.0) -> List[Dict[str, Any]]:
    """
    OFAT dalgalarında kullanılan sabit sipariş listesi.

    Args:
        scale_m2: OFAT_DEMAND_SCALE üzerine uygulanacak ek m² çarpanı

    Returns:
        musteri_talepleri biçiminde sipariş listesi
    """
    return baseline_orders_multi(OFAT_DEFAULT_ORDER_COUNT, OFAT_DEMAND_SCALE * scale_m2)


def _run_one(
    label: str,
    rulo_ton: float,
    orders: List[Dict[str, Any]],
    **kwargs: Any,
) -> Dict[str, Any]:
    """
    Tek test_calistir koşusunu yapar ve ek meta/girdi maliyetlerini döner.

    Args:
        label: Eksen değeri açıklaması
        rulo_ton: Toplam rulo tonajı
        orders: Sipariş listesi
        **kwargs: test_calistir argümanları; ofat_ne_degisti_override CSV açıklaması için

    Returns:
        test_calistir çıktısına axis_value_label, input_*Cost ve opsiyonel override eklenmiş sözlük
    """
    kw = dict(kwargs)
    override = kw.pop("ofat_ne_degisti_override", None)
    fc = float(kw.pop("fire_cost", DEFAULT_FIRE_COST))
    sc = float(kw.pop("stock_cost", DEFAULT_STOCK_COST))
    stc = float(kw.pop("setup_cost", DEFAULT_SETUP_COST))
    # Tüm OFAT noktalarında 4-13 ton rulo bandı uygulanır (aksi belirtilmedikçe)
    kw.setdefault("rolls_band", DEFAULT_ROLLS_BAND)
    kw.setdefault("rolls_seed", DEFAULT_ROLLS_SEED)
    r = test_calistir(
        rulo_ton,
        orders,
        fire_cost=fc,
        stock_cost=sc,
        setup_cost=stc,
        **kw,
    )
    r["axis_value_label"] = label
    r["input_fireCost"] = fc
    r["input_setupCost"] = stc
    r["input_stockCost"] = sc
    r["_context"] = {
        "rulo_ton": rulo_ton,
        "orders": list(orders),
        "max_orders_per_roll": int(kwargs.get("max_orders_per_roll", DEFAULT_MAX_ORDERS_PER_ROLL)),
        "max_rolls_per_order": int(kwargs.get("max_rolls_per_order", DEFAULT_MAX_ROLLS_PER_ORDER)),
    }
    if override:
        r["ofat_ne_degisti_override"] = override
    return r


def wave_cost_fire(out_rows: List[Dict[str, Any]]) -> None:
    """
    fireCost OFAT dalgasını çalıştırır; her çarpan için bir nokta.

    Args:
        out_rows: Nokta sonuçlarının eklendiği liste
    """
    for m in LEVELS_COST_MULT:
        c = baseline_costs(fire_mult=m, stock_mult=1.0, setup_mult=1.0)
        out_rows.append(_result_and_raw("fireCost", m, _run_one(
            f"fireCost×{m}", DEFAULT_TOTAL_ROLL_TONNAGE, _ofat_baseline_orders(1.0),
            fire_cost=c["fire_cost"], stock_cost=c["stock_cost"], setup_cost=c["setup_cost"])))


def wave_cost_stock(out_rows: List[Dict[str, Any]]) -> None:
    """
    stockCost OFAT dalgasını çalıştırır.

    Args:
        out_rows: Nokta sonuçlarının eklendiği liste
    """
    for m in LEVELS_COST_MULT:
        c = baseline_costs(fire_mult=1.0, stock_mult=m, setup_mult=1.0)
        out_rows.append(_result_and_raw("stockCost", m, _run_one(
            f"stockCost×{m}", DEFAULT_TOTAL_ROLL_TONNAGE, _ofat_baseline_orders(1.0),
            fire_cost=c["fire_cost"], stock_cost=c["stock_cost"], setup_cost=c["setup_cost"])))


def wave_cost_setup(out_rows: List[Dict[str, Any]]) -> None:
    """
    setupCost OFAT dalgasını çalıştırır.

    Args:
        out_rows: Nokta sonuçlarının eklendiği liste
    """
    for m in LEVELS_COST_MULT:
        c = baseline_costs(fire_mult=1.0, stock_mult=1.0, setup_mult=m)
        out_rows.append(_result_and_raw("setupCost", m, _run_one(
            f"setupCost×{m}", DEFAULT_TOTAL_ROLL_TONNAGE, _ofat_baseline_orders(1.0),
            fire_cost=c["fire_cost"], stock_cost=c["stock_cost"], setup_cost=c["setup_cost"])))


def wave_fire_setup_tradeoff(out_rows: List[Dict[str, Any]]) -> None:
    """
    Fire ve kurulum birim maliyetlerinin birlikte değişimi (göreli profil).

    Args:
        out_rows: Nokta sonuçlarının eklendiği liste
    """
    scenarios: List[Tuple[int, str, float, float, float, str]] = [
        (1, "fire düşük (×0.5), kurulum referans", 0.5, 1.0, 1.0,
         "Fire birimi düşük: fire tonu artışı kuruluma göre ucuz kalabilir."),
        (2, "fire yüksek (×1.5), kurulum referans", 1.5, 1.0, 1.0,
         "Fire birimi yüksek: fire azaltma baskısı artar; ek rulo kurulum maliyetiyle dengelenir."),
        (3, "referans maliyetler", 1.0, 1.0, 1.0,
         "Referans: thesis_ofat_baseline birim maliyetleri."),
        (4, "kurulum düşük (×0.5), fire referans", 1.0, 1.0, 0.5,
         "Kurulum ucuz: ek rulo açma nispeten cazip (fire sabit birimde)."),
        (5, "kurulum yüksek (×2), fire referans", 1.0, 1.0, 2.0,
         "Kurulum pahalı: rulo değişimini azaltma eğilimi güçlenebilir."),
    ]
    for aid, lbl, fmul, stmul, supmul, expl in scenarios:
        c = baseline_costs(fire_mult=fmul, stock_mult=stmul, setup_mult=supmul)
        override = (
            f"Fire/kurulum göreli profil #{aid}: birim fire/setup/stock = {c['fire_cost']:.2f}/"
            f"{c['setup_cost']:.2f}/{c['stock_cost']:.2f}. {expl}"
        )
        out_rows.append(_result_and_raw("fireSetupTradeoff", aid, _run_one(
            lbl, DEFAULT_TOTAL_ROLL_TONNAGE, _ofat_baseline_orders(1.0),
            fire_cost=c["fire_cost"], stock_cost=c["stock_cost"], setup_cost=c["setup_cost"],
            ofat_ne_degisti_override=override)))


def wave_max_orders_per_roll(out_rows: List[Dict[str, Any]]) -> None:
    """
    maxOrdersPerRoll OFAT dalgasını çalıştırır.

    Args:
        out_rows: Nokta sonuçlarının eklendiği liste
    """
    for v in LEVELS_MAX_ORDERS_PER_ROLL:
        out_rows.append(_result_and_raw("maxOrdersPerRoll", v, _run_one(
            f"maxOrdersPerRoll={v} (üst bant, hat geçiş OFAT)",
            DEFAULT_TOTAL_ROLL_TONNAGE, _ofat_baseline_orders(1.0),
            max_orders_per_roll=v, max_rolls_per_order=DEFAULT_MAX_ROLLS_PER_ORDER)))


def wave_max_rolls_per_order(out_rows: List[Dict[str, Any]]) -> None:
    """
    maxRollsPerOrder OFAT dalgasını çalıştırır.

    Args:
        out_rows: Nokta sonuçlarının eklendiği liste
    """
    for v in LEVELS_MAX_ROLLS_PER_ORDER:
        out_rows.append(_result_and_raw("maxRollsPerOrder", v, _run_one(
            f"maxRollsPerOrder={v}", DEFAULT_TOTAL_ROLL_TONNAGE, _ofat_baseline_orders(1.0),
            max_orders_per_roll=DEFAULT_MAX_ORDERS_PER_ROLL, max_rolls_per_order=v)))


def wave_demand(out_rows: List[Dict[str, Any]]) -> None:
    """
    Talep (m²) ölçek OFAT dalgasını çalıştırır.

    Args:
        out_rows: Nokta sonuçlarının eklendiği liste
    """
    for m in LEVELS_DEMAND_MULT:
        out_rows.append(_result_and_raw("demand_m2", m, _run_one(
            f"demand_m2×{m}", DEFAULT_TOTAL_ROLL_TONNAGE, _ofat_baseline_orders(m))))


def wave_roll_total(out_rows: List[Dict[str, Any]]) -> None:
    """
    Toplam rulo tonajı OFAT dalgasını çalıştırır.

    Args:
        out_rows: Nokta sonuçlarının eklendiği liste
    """
    for ton in LEVELS_ROLL_TOTAL_TONS:
        out_rows.append(_result_and_raw("rollTotal", ton, _run_one(
            f"totalRollTon={ton}t", ton, _ofat_baseline_orders(1.0))))


def wave_order_count(out_rows: List[Dict[str, Any]]) -> None:
    """
    Sipariş sayısı OFAT dalgasını çalıştırır.

    Args:
        out_rows: Nokta sonuçlarının eklendiği liste
    """
    for n in (3, 4, 5, 6):
        out_rows.append(_result_and_raw("orderCount", n, _run_one(
            f"{n} sipariş (800–3500 m² profili ×{OFAT_DEMAND_SCALE})",
            DEFAULT_TOTAL_ROLL_TONNAGE, baseline_orders_multi(n, OFAT_DEMAND_SCALE))))


def _result_and_raw(axis: str, axis_value: Any, r: Dict[str, Any]) -> Dict[str, Any]:
    """
    Tek OFAT noktasının düz CSV satırı + ham test_calistir sözlüğünü birleştirir.

    Args:
        axis: Eksen adı
        axis_value: Eksen değeri
        r: _run_one dönüşü

    Returns:
        {row: CSV satırı, raw_r: test_calistir, context: girdi paketleri} sözlüğü
    """
    ne = r.get("ofat_ne_degisti_override") or ofat_ne_degisti_aciklamasi(axis, axis_value, OFAT_BASELINE_ACIKLAMA)
    row = {
        "axis": axis,
        "axis_value": axis_value,
        "axis_value_label": r.get("axis_value_label", ""),
        "ofat_ne_degisti": ne,
        "input_fireCost": r.get("input_fireCost", ""),
        "input_setupCost": r.get("input_setupCost", ""),
        "input_stockCost": r.get("input_stockCost", ""),
        "phase": r.get("phase"),
        "solver_status": r.get("solver_status"),
        "failure_code": r.get("failure_code"),
        "totalFire": r.get("toplam_fire"),
        "openedRolls": r.get("kullanilan_rulo_sayisi"),
        "rollChangeCount": r.get("rulo_degisim_sayisi"),
        "lineRollTransitionCount": r.get("uretim_hatti_rulo_gecis_sayisi"),
        "lineSynchronousChanges": r.get("uretim_hatti_es_zamanli_gecis_sayisi"),
        "lineIndependentChanges": r.get("uretim_hatti_bagimsiz_gecis_sayisi"),
        "surfaceSyncViolations": r.get("yuzey_es_zaman_ihlal_sayisi"),
        "totalStock": r.get("toplam_stok"),
        "totalCost": r.get("toplam_maliyet"),
        "toplam_talep_ton": r.get("toplam_talep_ton"),
        "toplam_rulo_kapasitesi_ton": r.get("toplam_rulo_kapasitesi_ton"),
        "rulo_kapasiteleri_str": r.get("rulo_kapasiteleri_str"),
        "kesim_senaryosu_metni": (r.get("kesim_senaryosu_metni") or "")[:500],
        "notes": r.get("mesaj", ""),
    }
    ctx = r.get("_context") or {}
    raw = r.get("raw") or {}
    results = raw.get("results") if isinstance(raw, dict) else None
    row["kullanilan_ton"] = sum(float(x.get("used", 0) or 0) for x in (results.get("rollStatus") or [])) if results else 0.0
    return {"row": row, "raw_r": r, "context": ctx}


def _axis_value_esit(a: Any, b: Any) -> bool:
    """
    int/float karışımı axis değerlerini güvenle karşılaştırır.

    Args:
        a: Birinci değer
        b: İkinci değer

    Returns:
        Eşitlik
    """
    if a == b:
        return True
    try:
        return abs(float(a) - float(b)) < 1e-9
    except (TypeError, ValueError):
        return False


def _referans_secer(rows: Sequence[Dict[str, Any]], reference_axis_value: Any) -> Optional[Dict[str, Any]]:
    """
    Referans satırı bulur (eksen değeri eşleşen ilk satır; bulunamazsa ortadaki Optimal).

    Args:
        rows: Tüm nokta satırları
        reference_axis_value: Eksen referans değeri

    Returns:
        Satır sözlüğü veya None
    """
    for row in rows:
        if _axis_value_esit(row.get("axis_value"), reference_axis_value):
            return row
    opts = [r for r in rows if r.get("solver_status") == "Optimal"]
    if opts:
        return opts[len(opts) // 2]
    return rows[0] if rows else None


def _stamp_referans_deltas(chunk: List[Dict[str, Any]], reference_axis_value: Any) -> None:
    """
    Aynı dalgada referansa göre Δfire, Δacilan_rulo, Δkesim_deg, Δhat_gecis, Δstok ekler.

    Args:
        chunk: _result_and_raw.row listeleri
        reference_axis_value: Referans değeri
    """
    if not chunk:
        return
    ref = _referans_secer(chunk, reference_axis_value)
    for row in chunk:
        row["referans_axis_value"] = ref.get("axis_value") if ref else ""
        row["referansa_gore_fark"] = ofat_delta_vs_referans_satir(row, ref) if ref else ""


def _ofat_axis_islem(
    suite_kok: str,
    test_no: int,
    axis: str,
    ekseni_acik_adi: str,
    aciklama: str,
    bundle: List[Dict[str, Any]],
    ref_axis_value: Any,
) -> Dict[str, Any]:
    """
    Tek OFAT ekseni için suite alt klasörünü doldurur (alt raporlar, nokta XLSX'leri, grafikler).

    Args:
        suite_kok: Suite kök klasörü
        test_no: 1-based sıra numarası
        axis: Eksen slug (fireCost vb.)
        ekseni_acik_adi: Kısa insan okunur eksen adı
        aciklama: Eksen uzun açıklaması
        bundle: _result_and_raw çıktısı listesi (row + raw_r + context)
        ref_axis_value: Referans axis değeri (delta için)

    Returns:
        Eksen düzey özet sözlük (kullanıcıya dönüş/pipeline için)
    """
    slug = f"{safe_slug(axis)}_{safe_slug(aciklama, 60)}"
    paths = senaryo_klasoru_hazirla(suite_kok, test_no, slug, grafikler_alt_klasor=True)
    rows = [b["row"] for b in bundle]
    _stamp_referans_deltas(rows, ref_axis_value)

    axis_noktalari_dir = os.path.join(paths["klasor"], "axis_noktalari")
    os.makedirs(axis_noktalari_dir, exist_ok=True)
    nokta_xlsx_yollari: List[str] = []
    for b in bundle:
        axis_value = b["row"].get("axis_value")
        ctx = b["context"]
        raw_r = b["raw_r"]
        nokta_klasor = os.path.join(axis_noktalari_dir, axis_deger_klasoru(axis_value))
        os.makedirs(nokta_klasor, exist_ok=True)
        meta = scenario_meta_from_test_calistir(
            senaryo_adi=f"{ekseni_acik_adi} = {axis_value}",
            sonuc=raw_r,
            siparisler=ctx.get("orders") or [],
            max_siparis_per_rulo=ctx.get("max_orders_per_roll"),
            max_rulo_per_siparis=ctx.get("max_rolls_per_order"),
            fire_cost=raw_r.get("input_fireCost", DEFAULT_FIRE_COST),
            setup_cost=raw_r.get("input_setupCost", DEFAULT_SETUP_COST),
            stock_cost=raw_r.get("input_stockCost", DEFAULT_STOCK_COST),
            aciklama=f"OFAT {axis} = {axis_value}",
        )
        nokta_xlsx = os.path.join(nokta_klasor, "cozum_raporu.xlsx")
        raw = raw_r.get("raw") or {}
        results = raw.get("results") if isinstance(raw, dict) else None
        kesim_png = kesim_semasi_from_results(
            results,
            os.path.join(nokta_klasor, "kesim_semasi.png"),
            baslik=f"{ekseni_acik_adi} = {axis_value} — Kesim Şeması",
        )
        adim_png = uretim_adimlari_grafigi_from_results(
            results,
            os.path.join(nokta_klasor, "uretim_adimlari.png"),
            baslik=f"{ekseni_acik_adi} = {axis_value} — Üretim Adımları",
        )
        embed_graf = [p for p in (kesim_png, adim_png) if p]
        build_cozum_raporu_xlsx(meta, raw_r, nokta_xlsx,
                                 grafik_yollari=embed_graf if embed_graf else None)
        nokta_xlsx_yollari.append(nokta_xlsx)

    axis_values = [b["row"].get("axis_value") for b in bundle]
    grafik_paths = ofat_eksen_line_grafikleri(
        axis, axis_values, rows, paths["grafikler_dir"],
        referans_axis_value=ref_axis_value, alt_baslik=OFAT_BASELINE_ACIKLAMA,
    )

    kolonlar = [
        "axis", "axis_value", "axis_value_label", "ofat_ne_degisti",
        "input_fireCost", "input_setupCost", "input_stockCost",
        "phase", "solver_status", "failure_code",
        "totalFire", "openedRolls", "rollChangeCount", "lineRollTransitionCount",
        "lineSynchronousChanges", "lineIndependentChanges", "surfaceSyncViolations",
        "totalStock", "totalCost", "kullanilan_ton",
        "toplam_talep_ton", "toplam_rulo_kapasitesi_ton", "rulo_kapasiteleri_str",
        "referans_axis_value", "referansa_gore_fark",
        "kesim_senaryosu_metni", "notes",
    ]
    metrikler_csv = paths["metrikler_csv"]
    coklu_satir_csv_yaz(metrikler_csv, rows, kolonlar)

    ozet_satirlar = [
        {
            "Eksen Değeri": r.get("axis_value"),
            "Etiket": r.get("axis_value_label"),
            "Phase": r.get("phase"),
            "Solver": r.get("solver_status"),
            "Fire (ton)": r.get("totalFire"),
            "Stok (ton)": r.get("totalStock"),
            "Açılan Rulo": r.get("openedRolls"),
            "Rulo Değişim": r.get("rollChangeCount"),
            "Hat Geçiş": r.get("lineRollTransitionCount"),
            "Toplam Maliyet": r.get("totalCost"),
            "Δ(ref)": r.get("referansa_gore_fark", ""),
        }
        for r in rows
    ]
    ofat_xlsx = os.path.join(paths["klasor"], "ofat_rapor.xlsx")
    karsilastirma_xlsx(
        ozet_satirlar, ofat_xlsx,
        grafik_yollari=grafik_paths,
        baslik=f"OFAT — {ekseni_acik_adi}",
        ek_sheetler={
            "Tum_Sutunlar": [list(rows[0].keys())] + [[r.get(k, "") for k in rows[0].keys()] for r in rows]
            if rows else [[]]
        },
    )

    rapor_md_yaz(
        paths["rapor_md"],
        baslik=f"OFAT — {ekseni_acik_adi}",
        girdi_ozeti=f"Baseline: {OFAT_BASELINE_ACIKLAMA}",
        sonuc={
            "phase": "ofat_axis",
            "solver_status": f"{sum(1 for r in rows if r.get('solver_status')=='Optimal')}/{len(rows)} Optimal",
            "failure_code": "",
            "passed": "",
            "hints": [],
        },
        metrikler={
            "Eksen": axis,
            "Nokta Sayısı": len(rows),
            "Referans Değer": ref_axis_value,
            "Açıklama": aciklama,
        },
        kesim_senaryosu_metni="",
        dosya_listesi=[
            "ofat_rapor.xlsx",
            "metrikler.csv",
            "axis_noktalari/<carpan_X>/cozum_raporu.xlsx",
            "grafikler/",
        ],
        ek_bolumler=[
            {
                "baslik": "Nokta Özetleri",
                "icerik": "\n".join(
                    f"- {r.get('axis_value')}: {r.get('solver_status')} · fire={r.get('totalFire')} · "
                    f"maliyet={r.get('totalCost')} · rulo={r.get('openedRolls')}"
                    for r in rows
                ),
            }
        ],
    )

    return {
        "axis": axis,
        "ekseni_acik_adi": ekseni_acik_adi,
        "klasor": os.path.basename(paths["klasor"]),
        "rows": rows,
        "grafikler": grafik_paths,
        "xlsx": ofat_xlsx,
    }


def run_ofat_suite(suite_kok: str) -> List[Dict[str, Any]]:
    """
    Tüm OFAT dalgalarını sırayla çalıştırır ve suite klasörünü doldurur.

    Args:
        suite_kok: Suite kök klasörü (zaman damgalı)

    Returns:
        Her eksen için özet sözlükler listesi
    """
    eksenler: List[Tuple[str, str, str, Any, Callable[[List[Dict[str, Any]]], None]]] = [
        ("fireCost", "Maliyet — fireCost", "carpan_0p5_ile_1p5", 1.0, wave_cost_fire),
        ("stockCost", "Maliyet — stockCost", "carpan_0p5_ile_1p5", 1.0, wave_cost_stock),
        ("setupCost", "Maliyet — setupCost", "carpan_0p5_ile_1p5", 1.0, wave_cost_setup),
        ("fireSetupTradeoff", "Maliyet — fire vs kurulum profil", "profil_1_5", 3, wave_fire_setup_tradeoff),
        ("maxOrdersPerRoll", "Kısıt — maxOrdersPerRoll", "6_8_10", 8, wave_max_orders_per_roll),
        ("maxRollsPerOrder", "Kısıt — maxRollsPerOrder", "2_4_8", 4, wave_max_rolls_per_order),
        ("demand_m2", "Talep — m²", "carpan_0p8_1p0_1p2", 1.0, wave_demand),
        ("rollTotal", "Kapasite — toplam rulo ton", "60_85_120", 85.0, wave_roll_total),
        ("orderCount", "Parça — sipariş sayısı", "3_4_5_6", 5, wave_order_count),
    ]
    eksen_ozetleri: List[Dict[str, Any]] = []
    for i, (axis, ad, aralik, ref_val, fn) in enumerate(eksenler, start=1):
        print(f"[{i}/{len(eksenler)}] {ad} — {aralik}")
        chunk: List[Dict[str, Any]] = []
        fn(chunk)
        eksen_ozetleri.append(
            _ofat_axis_islem(suite_kok, i, axis, ad, aralik, chunk, ref_val)
        )
        print(f"    → {len(chunk)} nokta, {sum(1 for b in chunk if b['row'].get('solver_status')=='Optimal')} Optimal")
    return eksen_ozetleri


def _karsilastirma_yaz(suite_kok: str, eksen_ozetleri: List[Dict[str, Any]]) -> None:
    """
    Suite düzeyinde `_karsilastirma/` klasörü ve ozet XLSX / md / csv yazar.

    Args:
        suite_kok: Suite kök klasörü
        eksen_ozetleri: run_ofat_suite çıktısı
    """
    karsilastirma = karsilastirma_klasoru_hazirla(suite_kok)

    eksen_adlari = [e["axis"] for e in eksen_ozetleri]
    eksen_row_gruplari = [e["rows"] for e in eksen_ozetleri]
    normalize_graf = ofat_eksenler_normalize(
        eksen_adlari, eksen_row_gruplari, karsilastirma["grafikler_dir"]
    )

    ozet_satirlar = []
    for e in eksen_ozetleri:
        rows = e["rows"]
        opts = [r for r in rows if r.get("solver_status") == "Optimal"]
        fires = [float(r.get("totalFire") or 0) for r in opts]
        maliyetler = [float(r.get("totalCost") or 0) for r in opts]
        ozet_satirlar.append(
            {
                "Eksen": e["axis"],
                "Ad": e["ekseni_acik_adi"],
                "Klasör": e["klasor"],
                "Nokta": len(rows),
                "Optimal": len(opts),
                "Fire min": min(fires) if fires else "",
                "Fire max": max(fires) if fires else "",
                "Maliyet min": min(maliyetler) if maliyetler else "",
                "Maliyet max": max(maliyetler) if maliyetler else "",
            }
        )

    ek_sheetler: Dict[str, List[List[Any]]] = {}
    for e in eksen_ozetleri:
        rows = e["rows"]
        if not rows:
            continue
        headers = list(rows[0].keys())
        ek_sheetler[e["axis"][:31]] = [headers] + [[r.get(k, "") for k in headers] for r in rows]

    karsilastirma_xlsx(
        ozet_satirlar, karsilastirma["karsilastirma_xlsx"],
        grafik_yollari=normalize_graf,
        baslik="OFAT — EKSEN ÖZETİ",
        ek_sheetler=ek_sheetler,
    )

    birlesik: List[Dict[str, Any]] = []
    for e in eksen_ozetleri:
        birlesik.extend(e["rows"])
    if birlesik:
        kolonlar = list(birlesik[0].keys())
        coklu_satir_csv_yaz(karsilastirma["karsilastirma_csv"], birlesik, kolonlar)

    md_basliklari = ["Eksen", "Ad", "Klasör", "Nokta", "Optimal", "Fire min", "Fire max", "Maliyet min", "Maliyet max"]
    md_satirlar = [[r[h] for h in md_basliklari] for r in ozet_satirlar]
    karsilastirma_md_yaz(
        karsilastirma["karsilastirma_md"],
        baslik="OFAT Karşılaştırma",
        aciklama=OFAT_BASELINE_ACIKLAMA,
        tablo_basliklari=md_basliklari,
        tablo_satirlari=md_satirlar,
        grafik_listesi=[
            os.path.relpath(p, os.path.dirname(karsilastirma["karsilastirma_md"]))
            for p in normalize_graf
        ],
        ek_yorum="Her eksenin kendi grafikleri `_karsilastirma/../test_<N>_<eksen>/grafikler/` altında; "
                 "normalize trend ise `_karsilastirma/grafikler/`.",
    )


def main() -> None:
    """
    CLI giriş noktası: suite klasörünü oluşturur, tüm OFAT dalgalarını çalıştırır,
    INDEX.md + baseline + karşılaştırma dosyalarını yazar.
    """
    parser = argparse.ArgumentParser(description="OFAT duyarlılık analizi (suite XLSX + grafikler)")
    parser.add_argument("--out", default="", help="Suite kök (boşsa backend/reports/ofat_suite_<ts>)")
    args = parser.parse_args()

    base = os.path.dirname(os.path.abspath(__file__))
    ts = simdi_ts()
    if args.out:
        suite_kok = args.out
        os.makedirs(suite_kok, exist_ok=True)
    else:
        suite_kok = suite_kok_olustur(os.path.join(base, "reports"), f"ofat_suite_{ts}")

    eksen_ozetleri = run_ofat_suite(suite_kok)

    baseline_ozet_md_yaz(
        os.path.join(suite_kok, "baseline_ozeti.md"),
        "OFAT — Baseline Özeti",
        [
            f"Malzeme: 0.75 mm × 7.85 g/cm³",
            f"Toplam rulo: {DEFAULT_TOTAL_ROLL_TONNAGE} t — iki parça: {split_total_tonnage_to_two_rolls(DEFAULT_TOTAL_ROLL_TONNAGE)}",
            f"Birim maliyet referansı: fire/setup/stock = {DEFAULT_FIRE_COST}/{DEFAULT_SETUP_COST}/{DEFAULT_STOCK_COST}",
            f"Maliyet eksen çarpanları: {list(LEVELS_COST_MULT)}",
            f"Talep eksen çarpanları: {LEVELS_DEMAND_MULT}",
            f"Toplam ton eksen noktaları: {LEVELS_ROLL_TOTAL_TONS}",
            f"maxOrdersPerRoll eksen: {list(LEVELS_MAX_ORDERS_PER_ROLL)}; maxRollsPerOrder: {list(LEVELS_MAX_ROLLS_PER_ORDER)}",
            f"Baseline açıklama: {OFAT_BASELINE_ACIKLAMA}",
        ],
    )

    tablo = [["#", "Klasör", "Eksen", "Nokta", "Optimal"]]
    for i, e in enumerate(eksen_ozetleri, start=1):
        opts = sum(1 for r in e["rows"] if r.get("solver_status") == "Optimal")
        tablo.append([str(i), e["klasor"], e["ekseni_acik_adi"], str(len(e["rows"])), f"{opts}/{len(e['rows'])}"])
    index_md_yaz(
        os.path.join(suite_kok, "INDEX.md"),
        baslik="OFAT Duyarlılık Suite",
        ts=ts,
        baseline_ozeti=f"- {OFAT_BASELINE_ACIKLAMA}",
        senaryolar_tablosu=tablo,
        ek_aciklama="Her eksen için `test_<N>_<axis>/` altında `ofat_rapor.xlsx`, `axis_noktalari/`, `grafikler/`.",
    )

    _karsilastirma_yaz(suite_kok, eksen_ozetleri)

    print(f"\nOFAT suite yazıldı: {suite_kok}")


if __name__ == "__main__":
    main()

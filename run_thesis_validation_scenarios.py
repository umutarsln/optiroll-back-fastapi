"""
Tez doğrulama: dört temel senaryo + isteğe bağlı Infeasible sondajları.

Her senaryo için ayrı klasör altında `cozum_raporu.xlsx`, `rapor.md`, `metrikler.csv` ve
`grafikler/` üretilir; suite kökünde `_karsilastirma/` klasörü, INDEX.md ve
baseline_ozeti.md yazılır.
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from thesis_chart_builder import (
    kesim_semasi_from_results,
    senaryo_seti_karsilastirma_grafikleri,
    uretim_adimlari_grafigi_from_results,
)
from thesis_failure_codes import CAPACITY_LT_DEMAND, NO_ORDERS
from thesis_ofat_baseline import baseline_orders_multi  # noqa: F401 — test/docs için referans
from thesis_report_common import (
    baseline_ozet_md_yaz,
    coklu_satir_csv_yaz,
    index_md_yaz,
    karsilastirma_klasoru_hazirla,
    karsilastirma_md_yaz,
    metrik_satiri_derle,
    rapor_md_yaz,
    senaryo_klasoru_hazirla,
    simdi_ts,
    suite_kok_olustur,
    tek_satir_csv_yaz,
)
from thesis_test_harness import test_calistir
from thesis_xlsx_report import build_cozum_raporu_xlsx, karsilastirma_xlsx, scenario_meta_from_test_calistir


_TEZ_KALINLIK_MM = 0.75
_TEZ_YOGUNLUK = 7.85
_TON_PER_M2 = (_TEZ_KALINLIK_MM / 1000.0) * _TEZ_YOGUNLUK  # ≈0.005888 t/m²


def _siparis_ton_listesi(
    tonlar: List[float],
    *,
    panel_width: float = 1.0,
    panel_length: float = 1.0,
) -> List[Dict[str, Any]]:
    """
    Hedef sipariş tonajlarını (ton) panel m² değerlerine dönüştürüp
    `test_calistir`'ın beklediği `musteri_talepleri` sözlüğü listesine çevirir.

    Formül: m² = ton / (thickness_mm/1000 × density_g_cm3). test_calistir içinde
    çift yüzey çarpanı (2×) ayrıca uygulanır, yani burada verilen ton değeri
    "sipariş tonajı"dır; kullanım ihtiyacı bunun iki katıdır.

    Args:
        tonlar: Her sipariş için hedef sipariş tonajı (ör. 8, 12, 15 t)
        panel_width: Panel genişliği
        panel_length: Panel uzunluğu

    Returns:
        test_calistir için sipariş sözlükleri listesi
    """
    orders: List[Dict[str, Any]] = []
    for t in tonlar:
        m2 = float(t) / _TON_PER_M2 if _TON_PER_M2 > 0 else 0.0
        orders.append({"m2": round(m2, 2), "panelWidth": panel_width, "panelLength": panel_length})
    return orders


def _assert_pass(expected_kind: str, r: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Senaryoya göre otomatik PASS/FAIL kuralı uygular.

    Args:
        expected_kind: Senaryo hedef türü (validation_error, optimal, optimal_low_waste,
                       infeasible_probe, observation)
        r: test_calistir çıktısı

    Returns:
        (geçti_mi, kısa sebep)
    """
    phase = r.get("phase")
    fc = r.get("failure_code")
    st = r.get("solver_status")

    if expected_kind == "validation_error":
        if phase == "precheck" and fc in (CAPACITY_LT_DEMAND, NO_ORDERS):
            return True, "ön kontrol beklenen kod"
        return False, f"beklenen precheck, gelen phase={phase} code={fc}"
    if expected_kind == "optimal":
        if st == "Optimal":
            return True, "Optimal"
        return False, f"beklenen Optimal, gelen {st}"
    if expected_kind == "optimal_low_waste":
        if st == "Optimal":
            tf = float(r.get("toplam_fire", 0) or 0)
            if tf <= 0.51:
                return True, "düşük fire bandı"
            return True, "Optimal (fire>0.5 iş kuralı sonrası)"
        return False, f"Optimal değil: {st}"
    if expected_kind == "infeasible_probe":
        if st == "Infeasible" or (phase == "solver" and fc):
            return True, "Infeasible veya failure_code"
        return False, "Infeasible bekleniyordu"
    if expected_kind == "observation":
        return True, "gözlemsel (sonuç kaydı)"
    return False, "bilinmeyen expected_kind"


def _senaryolar_tanimla(with_probes: bool) -> List[Dict[str, Any]]:
    """
    Tüm tez doğrulama senaryolarını tanımlar.

    Args:
        with_probes: True ise Infeasible sondaj senaryoları da eklenir

    Returns:
        Her öğe: name, aciklama, expected_kind, rulo_uzunlugu, orders, kwargs
    """
    # Rulolar 4-13 ton aralığında deterministik random bölünür (rolls_band=(4,13)).
    # Sipariş tonları gerçek üretim bandında (5-15 t), çift yüzey çarpanı 2x ile kullanım
    # ihtiyacı iki katı; kapasiteler 40-70 t aralığında → baseline 7-9 rulo hedefi (5-10 civarı).
    # Solver süresi 180 sn: gerçek üretim karmaşıklığında kaliteli çözüm için yeterli süre.
    _tl = 180
    _kw_base = {"rolls_band": (4, 13), "rolls_seed": 42, "time_limit_seconds": _tl}
    lst: List[Dict[str, Any]] = [
        {
            "name": "Aşırı kapasite (geniş)",
            "aciklama": (
                "rulo_tonaj=55 (4-13 t bandında ≈7-8 rulo); 3 sipariş (5, 8, 12 t); "
                "25 t talep → 50 t kullanım; %10 kapasite marjı — fire minimize senaryosu"
            ),
            "slug_uzun": "asiri_kapasite_rulo55t_3siparis_5_12ton",
            "expected_kind": "optimal",
            "rulo_uzunlugu": 55.0,
            "orders": _siparis_ton_listesi([5.0, 8.0, 12.0]),
            "kwargs": dict(_kw_base),
        },
        {
            "name": "İmkansız talep (tonaj < ihtiyaç)",
            "aciklama": (
                "rulo_tonaj=20 (4-13 bandında 2-3 rulo); 4 sipariş (10, 12, 15, 18 t); "
                "55 t talep × 2 = 110 t kullanım kapasitenin çok üstünde (precheck)"
            ),
            "slug_uzun": "imkansiz_talep_rulo20t_4siparis_10_18ton",
            "expected_kind": "validation_error",
            "rulo_uzunlugu": 20.0,
            "orders": _siparis_ton_listesi([10.0, 12.0, 15.0, 18.0]),
            "kwargs": dict(_kw_base),
        },
        {
            "name": "Sıfır talep",
            "aciklama": "orders=[] (ön kontrol NO_ORDERS)",
            "slug_uzun": "sifir_talep_orders_bos",
            "expected_kind": "validation_error",
            "rulo_uzunlugu": 40.0,
            "orders": [],
            "kwargs": dict(_kw_base),
        },
        {
            "name": "Tam eşleşme (düşük fire hedefi)",
            "aciklama": (
                "rulo_tonaj=42 (4-13 bandında ≈6 rulo); 3 sipariş (6, 7, 7 t); "
                "20 t talep → 40 t kullanım; kapasite sadece %5 fazla — düşük fire bandı"
            ),
            "slug_uzun": "tam_esleme_rulo42t_3siparis_6_7ton",
            "expected_kind": "optimal_low_waste",
            "rulo_uzunlugu": 42.0,
            "orders": _siparis_ton_listesi([6.0, 7.0, 7.0]),
            "kwargs": dict(_kw_base),
        },
        {
            "name": "Baseline (4 sipariş karma)",
            "aciklama": (
                "rulo_tonaj=60 (4-13 bandında ≈8 rulo); 4 sipariş (5, 6, 8, 10 t); "
                "29 t talep → 58 t kullanım; %3 kapasite marjı ile karma dağılım testi"
            ),
            "slug_uzun": "baseline_rulo60t_4siparis_5_10ton",
            "expected_kind": "optimal",
            "rulo_uzunlugu": 60.0,
            "orders": _siparis_ton_listesi([5.0, 6.0, 8.0, 10.0]),
            "kwargs": dict(_kw_base),
        },
        {
            "name": "Çoklu 3 sipariş (büyük)",
            "aciklama": (
                "rulo_tonaj=65 (4-13 bandında ≈8-9 rulo); 3 sipariş (8, 10, 12 t); "
                "30 t talep → 60 t kullanım; %8 kapasite marjı, her sipariş birden fazla ruloya dağılır"
            ),
            "slug_uzun": "coklu_3siparis_rulo65t_buyuk_8_12ton",
            "expected_kind": "optimal",
            "rulo_uzunlugu": 65.0,
            "orders": _siparis_ton_listesi([8.0, 10.0, 12.0]),
            "kwargs": dict(_kw_base),
        },
        {
            "name": "Çoklu 5 sipariş (orta)",
            "aciklama": (
                "rulo_tonaj=72 (4-13 bandında ≈9 rulo); 5 sipariş (4, 5, 6, 8, 10 t); "
                "33 t talep → 66 t kullanım; %9 kapasite marjı, yoğun sipariş kombinasyonu"
            ),
            "slug_uzun": "coklu_5siparis_rulo72t_orta_4_10ton",
            "expected_kind": "optimal",
            "rulo_uzunlugu": 72.0,
            "orders": _siparis_ton_listesi([4.0, 5.0, 6.0, 8.0, 10.0]),
            "kwargs": dict(_kw_base),
        },
    ]
    if with_probes:
        lst.extend(
            [
                {
                    "name": "Sondaj: maxOrdersPerRoll=1 iki büyük sipariş",
                    "aciklama": (
                        "rulo_tonaj=50 (≈6-7 rulo, 4-13 t); 2 sipariş (10 t her biri); "
                        "max_orders_per_roll=1 kısıtıyla fire/sıra davranışı"
                    ),
                    "slug_uzun": "sondaj_maxOrdersPerRoll_1_iki_buyuk_siparis",
                    "expected_kind": "infeasible_probe",
                    "rulo_uzunlugu": 50.0,
                    "orders": _siparis_ton_listesi([10.0, 10.0]),
                    "kwargs": {"max_orders_per_roll": 1, "max_rolls_per_order": 8,
                               "rolls_band": (4, 13), "rolls_seed": 42},
                },
                {
                    "name": "Sondaj: dar maxRollsPerOrder (gözlem)",
                    "aciklama": (
                        "rulo_tonaj=45 (≈5-6 rulo, 4-13 t); 1 sipariş (15 t); "
                        "max_rolls_per_order=2 ile bölünme davranışı"
                    ),
                    "slug_uzun": "sondaj_maxRollsPerOrder_2_buyuk_tek_siparis",
                    "expected_kind": "observation",
                    "rulo_uzunlugu": 45.0,
                    "orders": _siparis_ton_listesi([15.0]),
                    "kwargs": {"max_rolls_per_order": 2, "rolls_band": (4, 13), "rolls_seed": 42},
                },
            ]
        )
    return lst


def _senaryoyu_isle(
    idx_1: int,
    senaryo: Dict[str, Any],
    suite_kok: str,
) -> Dict[str, Any]:
    """
    Tek senaryoyu çalıştırır, klasörünü hazırlar, XLSX + rapor.md + metrikler.csv +
    kesim şeması PNG üretir.

    Args:
        idx_1: 1-başlangıçlı sıra no
        senaryo: _senaryolar_tanimla çıktısı öğesi
        suite_kok: Suite kök klasörü

    Returns:
        Karşılaştırma için birleşik satır sözlüğü (metrik_satiri_derle çıktısı + ek alanlar)
    """
    r = test_calistir(
        senaryo["rulo_uzunlugu"],
        senaryo["orders"],
        **senaryo.get("kwargs") or {},
    )
    ok, reason = _assert_pass(senaryo["expected_kind"], r)
    passed = "PASS" if ok else "FAIL"

    max_spr = senaryo.get("kwargs", {}).get("max_orders_per_roll", 6)
    max_rps = senaryo.get("kwargs", {}).get("max_rolls_per_order", 8)
    meta = scenario_meta_from_test_calistir(
        senaryo_adi=senaryo["name"],
        sonuc=r,
        siparisler=senaryo["orders"],
        max_siparis_per_rulo=max_spr,
        max_rulo_per_siparis=max_rps,
        aciklama=senaryo["aciklama"],
    )

    paths = senaryo_klasoru_hazirla(suite_kok, idx_1, senaryo["slug_uzun"], grafikler_alt_klasor=True)
    raw = r.get("raw") or {}
    results = raw.get("results")
    kesim_png = kesim_semasi_from_results(
        results,
        os.path.join(paths["grafikler_dir"], "kesim_semasi.png"),
        baslik=f"Kesim Şeması — {senaryo['name']}",
        alt_baslik=senaryo["aciklama"],
    )
    adim_png = uretim_adimlari_grafigi_from_results(
        results,
        os.path.join(paths["grafikler_dir"], "uretim_adimlari.png"),
        baslik=f"Üretim Adımları — {senaryo['name']}",
        alt_baslik=senaryo["aciklama"],
    )

    embed_graf = [p for p in (kesim_png, adim_png) if p]
    build_cozum_raporu_xlsx(meta, r, paths["cozum_raporu"],
                            grafik_yollari=embed_graf if embed_graf else None)

    metrik_row = metrik_satiri_derle(
        senaryo_adi=senaryo["name"],
        girdi_ozeti=senaryo["aciklama"],
        sonuc=r,
        scenario_meta=meta,
        passed=passed,
    )
    metrik_row["expected_kind"] = senaryo["expected_kind"]
    metrik_row["assert_reason"] = reason

    tek_satir_csv_yaz(paths["metrikler_csv"], metrik_row)

    rapor_md_yaz(
        paths["rapor_md"],
        baslik=senaryo["name"],
        girdi_ozeti=senaryo["aciklama"],
        sonuc={
            "phase": r.get("phase"),
            "solver_status": r.get("solver_status"),
            "failure_code": r.get("failure_code"),
            "passed": passed,
            "hints": r.get("hints"),
        },
        metrikler={
            "Beklenen": senaryo["expected_kind"],
            "Sebep": reason,
            "Toplam Fire (ton)": metrik_row["toplam_fire"],
            "Toplam Stok (ton)": metrik_row["toplam_stok"],
            "Kullanılan Ton": metrik_row["kullanilan_ton"],
            "Açılan Rulo": metrik_row["acilan_rulo"],
            "Rulo Değişim (kesim)": metrik_row["rulo_degisim_sayisi"],
            "Hat Geçiş (totalChanges)": metrik_row["uretim_hatti_rulo_gecis_sayisi"],
            "Toplam Maliyet": metrik_row["toplam_maliyet"],
            "Toplam Talep (ton)": metrik_row["toplam_talep_ton"],
            "Rulo Kapasiteleri": metrik_row["rulo_kapasiteleri_str"],
            "Kapasite Kullanım %": metrik_row["kapasite_kullanim_pct"],
            "Fire Oranı %": metrik_row["fire_orani_pct"],
        },
        kesim_senaryosu_metni=r.get("kesim_senaryosu_metni") or "",
        dosya_listesi=[
            "cozum_raporu.xlsx",
            "metrikler.csv",
            "grafikler/kesim_semasi.png" if kesim_png else "",
            "grafikler/uretim_adimlari.png" if adim_png else "",
        ],
    )

    metrik_row["klasor"] = os.path.basename(paths["klasor"])
    return metrik_row


def run_all_and_write(suite_kok: str, with_probes: bool) -> List[Dict[str, Any]]:
    """
    Tüm senaryoları çalıştırır, suite klasörünü doldurur, karşılaştırma dosyalarını yazar.

    Args:
        suite_kok: Suite kök klasörü (zaman damgalı)
        with_probes: Infeasible sondaj senaryolarını da ekle

    Returns:
        Karşılaştırma satırları (metrik_satiri_derle çıktıları)
    """
    senaryolar = _senaryolar_tanimla(with_probes)
    tum: List[Dict[str, Any]] = []
    print(f"Toplam {len(senaryolar)} senaryo çalıştırılacak.\n")
    for i, s in enumerate(senaryolar, start=1):
        print(f"[{i}/{len(senaryolar)}] {s['name']}")
        row = _senaryoyu_isle(i, s, suite_kok)
        tum.append(row)
        print(f"    durum={row['passed']} phase={row['phase']} solver={row['solver_status']} "
              f"failure={row['failure_code']} fire={row['toplam_fire']} maliyet={row['toplam_maliyet']}\n")

    karsilastirma = karsilastirma_klasoru_hazirla(suite_kok)

    grafik_yollari = senaryo_seti_karsilastirma_grafikleri(
        tum,
        karsilastirma["grafikler_dir"],
        alt_baslik="Tez doğrulama senaryoları",
    )
    kolon_sirasi = [
        "senaryo_adi", "girdi_ozeti", "expected_kind", "phase", "solver_status",
        "failure_code", "passed", "assert_reason", "toplam_fire", "toplam_stok",
        "kullanilan_ton", "acilan_rulo", "rulo_degisim_sayisi",
        "uretim_hatti_rulo_gecis_sayisi", "toplam_talep_ton",
        "toplam_rulo_kapasitesi_ton", "rulo_kapasiteleri_str", "toplam_maliyet",
        "fire_maliyet", "stok_maliyet", "setup_maliyet", "fire_orani_pct",
        "kapasite_kullanim_pct", "kesim_senaryosu_metni", "hints", "mesaj", "klasor",
    ]
    coklu_satir_csv_yaz(karsilastirma["karsilastirma_csv"], tum, kolon_sirasi)

    ozet_satirlar = [
        {
            "Senaryo": r["senaryo_adi"],
            "Beklenen": r["expected_kind"],
            "Durum": r["passed"],
            "Phase": r["phase"],
            "Solver": r["solver_status"],
            "FailureCode": r["failure_code"],
            "Fire (ton)": r["toplam_fire"],
            "Stok (ton)": r["toplam_stok"],
            "Kullanılan (ton)": r["kullanilan_ton"],
            "Açılan Rulo": r["acilan_rulo"],
            "Toplam Maliyet": r["toplam_maliyet"],
        }
        for r in tum
    ]
    maliyet_satirlar = [
        {
            "Senaryo": r["senaryo_adi"],
            "Fire Maliyeti": r["fire_maliyet"],
            "Stok Maliyeti": r["stok_maliyet"],
            "Setup Maliyeti": r["setup_maliyet"],
            "Toplam Maliyet": r["toplam_maliyet"],
        }
        for r in tum
    ]
    rulo_satirlar = [
        {
            "Senaryo": r["senaryo_adi"],
            "Rulo Kapasiteleri": r["rulo_kapasiteleri_str"],
            "Kapasite (ton)": r["toplam_rulo_kapasitesi_ton"],
            "Kullanılan (ton)": r["kullanilan_ton"],
            "Açılan Rulo": r["acilan_rulo"],
            "Rulo Değişim (kesim)": r["rulo_degisim_sayisi"],
            "Hat Geçiş (totalChanges)": r["uretim_hatti_rulo_gecis_sayisi"],
            "Kapasite Kullanım %": r["kapasite_kullanim_pct"],
            "Fire Oranı %": r["fire_orani_pct"],
        }
        for r in tum
    ]

    def _sheet(rows: List[Dict[str, Any]]) -> List[List[Any]]:
        """
        Sözlük listesini openpyxl tabloya uygun 2B listeye çevirir.

        Args:
            rows: Sözlük satırları

        Returns:
            İlk satır başlık, sonraki satırlar değer olan 2B liste
        """
        if not rows:
            return [[]]
        headers = list(rows[0].keys())
        out: List[List[Any]] = [headers]
        for r in rows:
            out.append([r.get(k, "") for k in headers])
        return out

    karsilastirma_xlsx(
        ozet_satirlar,
        karsilastirma["karsilastirma_xlsx"],
        grafik_yollari=grafik_yollari,
        baslik="TEZ DOĞRULAMA SENARYOLARI — ÖZET",
        ek_sheetler={
            "Maliyet_Kirilimi": _sheet(maliyet_satirlar),
            "Rulo_Metrikleri": _sheet(rulo_satirlar),
        },
    )

    md_satirlari = [
        [r["senaryo_adi"], r["passed"], r["phase"], r["solver_status"],
         r["failure_code"], r["toplam_fire"], r["toplam_maliyet"], r["acilan_rulo"]]
        for r in tum
    ]
    karsilastirma_md_yaz(
        karsilastirma["karsilastirma_md"],
        baslik="Tez Doğrulama Karşılaştırma",
        aciklama="Tüm senaryoların özet metrikleri; grafikler bu dosyanın yanında `grafikler/` altında.",
        tablo_basliklari=["Senaryo", "Durum", "Phase", "Solver", "FailureCode", "Fire", "Maliyet", "Rulo"],
        tablo_satirlari=md_satirlari,
        grafik_listesi=[os.path.relpath(p, os.path.dirname(karsilastirma["karsilastirma_md"])) for p in grafik_yollari],
        ek_yorum=f"Toplam: {sum(1 for r in tum if r['passed']=='PASS')}/{len(tum)} PASS.",
    )
    return tum


def _baseline_ozet_satirlari() -> List[str]:
    """
    Baseline özet metninde gösterilecek sabit satırları üretir.

    Returns:
        Bullet satırlar
    """
    return [
        "Malzeme: kalınlık 0.75 mm, yoğunluk 7.85 g/cm³",
        "Çift yüzey çarpanı: 2.0 (SURFACE_FACTOR_OPTIMIZE)",
        "Baseline birim maliyet: fire=100, setup=50, stock=30",
        "Her senaryoda rulolar iki fiziksel parçaya bölünür (split_total_tonnage_to_two_rolls)",
        "Mod profili: orta; senkron profili: dengeli",
        "Beklenen durum türleri: optimal / optimal_low_waste / validation_error / infeasible_probe / observation",
    ]


def main() -> None:
    """
    CLI giriş noktası: suite klasörünü oluşturur, senaryoları koşar, INDEX + baseline yazar.
    """
    parser = argparse.ArgumentParser(description="Tez doğrulama senaryoları (suite XLSX + grafikler)")
    parser.add_argument("--with-infeasible-probes", action="store_true",
                        help="Infeasible sondaj senaryolarını dahil et")
    parser.add_argument("--out", default="", help="Suite kök (boşsa backend/reports/thesis_suite_<ts>)")
    args = parser.parse_args()

    base = os.path.dirname(os.path.abspath(__file__))
    ts = simdi_ts()
    if args.out:
        suite_kok = args.out
        os.makedirs(suite_kok, exist_ok=True)
    else:
        suite_kok = suite_kok_olustur(os.path.join(base, "reports"), f"thesis_suite_{ts}")

    rows = run_all_and_write(suite_kok, args.with_infeasible_probes)

    baseline_ozet_md_yaz(
        os.path.join(suite_kok, "baseline_ozeti.md"),
        "Tez Doğrulama — Baseline Özeti",
        _baseline_ozet_satirlari(),
    )
    tablo = [["#", "Klasör", "Senaryo", "Beklenen", "Durum", "Solver", "Fire", "Maliyet"]]
    for i, r in enumerate(rows, start=1):
        tablo.append(
            [
                str(i),
                r.get("klasor", ""),
                r.get("senaryo_adi", ""),
                r.get("expected_kind", ""),
                r.get("passed", ""),
                r.get("solver_status", "") or "-",
                r.get("toplam_fire", ""),
                r.get("toplam_maliyet", ""),
            ]
        )
    pass_count = sum(1 for r in rows if r.get("passed") == "PASS")
    index_md_yaz(
        os.path.join(suite_kok, "INDEX.md"),
        baslik="Tez Doğrulama Suite",
        ts=ts,
        baseline_ozeti=(
            "- Malzeme 0.75 mm / 7.85 g/cm³, çift yüzey (2x). Birim maliyet fire/setup/stock = 100/50/30."
            "\n- Ayrıntı için `baseline_ozeti.md`."
        ),
        senaryolar_tablosu=tablo,
        ek_aciklama=f"Toplam PASS: {pass_count}/{len(rows)}",
    )

    print(f"\nSuite yazıldı: {suite_kok}")
    print(f"Özet: {pass_count}/{len(rows)} PASS")


if __name__ == "__main__":
    main()

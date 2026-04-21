"""
Rapor / veri karşılaştırma senaryoları.

Sabit rulo envanteri [6, 6, 6.4, 6.4] ile fire maliyeti cf=100 ve cf=150 karşılaştırılır.
Her cf için: serbest optimizasyon; yalnızca 6 t ruloların açılmasına izin verilen;
yalnızca 6,4 t ruloların açılmasına izin verilen senaryoların toplam maliyetleri raporlanır.
"""

from __future__ import annotations

import os
import sys
import unittest
from typing import Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from thesis_chart_builder import grouped_bar_gruplar, stacked_bar_kirilim

from optimizer import solve_optimization


def _siparis_1000m2_panel1() -> list:
    """Tek sipariş: 1000 m², 1×1 m panel (çift yüzey senaryoları için)."""
    return [{"m2": 1000, "panelWidth": 1.0, "panelLength": 1.0}]


def _sabit_rulolar() -> list[float]:
    """Karşılaştırma testinde değiştirilmeyen rulo tonaj listesini döndürür."""
    return [6.0, 6.0, 6.4, 6.4]


def _mask_sadece_6t() -> list[bool]:
    """İlk iki rulo (6 t) açılabilir; 6,4 t rulolar kapalı."""
    return [True, True, False, False]


def _mask_sadece_64t() -> list[bool]:
    """Son iki rulo (6,4 t) açılabilir; 6 t rulolar kapalı."""
    return [False, False, True, True]


def _coz_fire_cf(
    fire_cost: float,
    roll_open_mask: Optional[Sequence[bool]] = None,
) -> tuple[str, dict | None]:
    """
    Sabit rulolar ve verilen fire maliyeti (ve isteğe bağlı açılır rulo maskesi) ile çözüm üretir.

    Args:
        fire_cost: Birim fire maliyeti katsayısı.
        roll_open_mask: ``None`` ise tüm rulolar açılabilir; aksi halde ``False`` indeksler kilitlenir.

    Returns:
        ``solve_optimization`` çıktısı ``(status, results)``.
    """
    rolls = _sabit_rulolar()
    ortak = dict(
        thickness=0.75,
        density=7.85,
        orders=_siparis_1000m2_panel1(),
        panel_widths=[1.0],
        max_orders_per_roll=5,
        max_rolls_per_order=8,
        fire_cost=float(fire_cost),
        setup_cost=100.0,
        stock_cost=100.0,
        time_limit_seconds=120,
        surface_factor=2,
        roll_open_mask=roll_open_mask,
    )
    return solve_optimization(rolls=rolls, **ortak)


def _ozet_satir(results: dict) -> dict:
    """Çözüm sonucundan rapor satırı için özet alanları çıkarır."""
    sm = results.get("summary") or {}
    return {
        "totalCost": float(sm.get("totalCost", 0) or 0),
        "totalFire": float(sm.get("totalFire", 0) or 0),
        "totalStock": float(sm.get("totalStock", 0) or 0),
        "totalUnused": float(sm.get("totalUnusedInventoryTon", 0) or 0),
        "totalStockHoldingTon": float(sm.get("totalStockHoldingTon", 0) or 0),
        "openedRolls": int(sm.get("openedRolls", 0) or 0),
        "costFireLira": float(sm.get("costFireLira", 0) or 0),
        "costStockLira": float(sm.get("costStockLira", 0) or 0),
        "costStockProductionLira": float(sm.get("costStockProductionLira", 0) or 0),
        "costStockShelfLira": float(sm.get("costStockShelfLira", 0) or 0),
        "costSetupLira": float(sm.get("costSetupLira", 0) or 0),
        "costSequencePenaltyLira": float(sm.get("costSequencePenaltyLira", 0) or 0),
    }


def _maliyet_kirilimi_toplami(o: dict) -> float:
    """Özet satırındaki TL kırılımının toplamını döndürür (totalCost ile karşılaştırma için)."""
    return (
        o["costFireLira"]
        + o["costStockLira"]
        + o["costSetupLira"]
        + o["costSequencePenaltyLira"]
    )


def _senaryo_satirlari_hesapla(
    fire_cost: float,
) -> tuple[dict, dict, dict, str, str, str]:
    """
    Tek bir fire maliyeti için serbest, yalnız 6 t ve yalnız 6,4 t maskeli üç çözümü üretir.

    Args:
        fire_cost: Fire maliyeti katsayısı (100 veya 150).

    Returns:
        ``(ozet_serbest, ozet_s6, ozet_s64, st_s, st_6, st_64)`` — özet dict'ler ve durum dizgileri.
    """
    st0, r0 = _coz_fire_cf(fire_cost, None)
    st6, r6 = _coz_fire_cf(fire_cost, _mask_sadece_6t())
    st64, r64 = _coz_fire_cf(fire_cost, _mask_sadece_64t())
    o0 = _ozet_satir(r0) if r0 else {}
    o6 = _ozet_satir(r6) if r6 else {}
    o64 = _ozet_satir(r64) if r64 else {}
    return o0, o6, o64, st0, st6, st64


def _tablo_cf_alti_senaryo(
    paket_100: tuple[dict, dict, dict, str, str, str],
    paket_150: tuple[dict, dict, dict, str, str, str],
) -> None:
    """
    Önceden hesaplanmış iki cf paketiyle serbest / sadece 6t / sadece 6,4 maliyet tablosunu yazar.

    Args:
        paket_100: ``_senaryo_satirlari_hesapla(100)`` çıktısı.
        paket_150: ``_senaryo_satirlari_hesapla(150)`` çıktısı.
    """
    o0_100, o6_100, o64_100, s0_100, s6_100, s64_100 = paket_100
    o0_150, o6_150, o64_150, s0_150, s6_150, s64_150 = paket_150
    w = 36
    print("\n" + "=" * 88)
    print(
        "MALİYET ÖZETİ (TL) — rulolar 6, 6, 6.4, 6.4 · stok=100, açılış=100 · çift yüzey"
    )
    print("=" * 88)
    hdr = f"{'Senaryo':<{w}}{'cf=100':>14}{'cf=150':>14}{'st100':>8}{'st150':>8}"
    print(hdr)
    print("-" * 88)
    satirlar = [
        ("Serbest (hangi rulo optimal ise)", o0_100, o0_150, s0_100, s0_150),
        ("Yalnız 6 t rulolar açılabilir", o6_100, o6_150, s6_100, s6_150),
        ("Yalnız 6,4 t rulolar açılabilir", o64_100, o64_150, s64_100, s64_150),
    ]
    for ad, a100, a150, t100, t150 in satirlar:
        c100 = a100.get("totalCost", float("nan")) if t100 == "Optimal" else float("nan")
        c150 = a150.get("totalCost", float("nan")) if t150 == "Optimal" else float("nan")
        v100 = f"{c100:.2f}" if t100 == "Optimal" else "—"
        v150 = f"{c150:.2f}" if t150 == "Optimal" else "—"
        print(f"{ad:<{w}}{v100:>14}{v150:>14}{t100:>8}{t150:>8}")
    print("=" * 88 + "\n")


def rapor_karsilastirma_grafikleri_uret(grafikler_dir: str) -> list[str]:
    """
    cf=100 / cf=150 için serbest ve maskeli senaryoların toplam maliyet ve kırılım grafiklerini üretir.

    Birim test raporu (`run_unit_tests_report`) bu fonksiyonu çağırarak XLSX/MD içine
    gömülecek PNG yollarını alır.

    Args:
        grafikler_dir: Çıktı klasörü (örn. ``.../unit_tests_*/grafikler``).

    Returns:
        Oluşturulan PNG dosyalarının tam yol listesi.
    """
    o0_100, o6_100, o64_100, s0_100, s6_100, s64_100 = _senaryo_satirlari_hesapla(100.0)
    o0_150, o6_150, o64_150, s0_150, s6_150, s64_150 = _senaryo_satirlari_hesapla(150.0)
    if not all(x == "Optimal" for x in (s0_100, s6_100, s64_100, s0_150, s6_150, s64_150)):
        return []

    mat = [
        [o0_100["totalCost"], o6_100["totalCost"], o64_100["totalCost"]],
        [o0_150["totalCost"], o6_150["totalCost"], o64_150["totalCost"]],
    ]
    yollar: list[str] = []
    p_grup = os.path.join(grafikler_dir, "karsilastirma_maliyet_toplam_grup.png")
    grouped_bar_gruplar(
        ["cf=100", "cf=150"],
        ["Serbest", "Yalniz 6t", "Yalniz 6.4t"],
        mat,
        p_grup,
        baslik="Toplam maliyet (TL) — serbest vs rulo ailesi kisiti",
        y_label="TL",
        alt_baslik="Rulolar 6, 6, 6.4, 6.4 · stok=100, rulo acilis=100",
    )
    yollar.append(p_grup)

    def _kirilim_png(dosya_ad: str, cf_etik: str, o0: dict, o6: dict, o64: dict) -> str:
        """Verilen cf için üç senaryonun fire/stok/setup kırılım grafiğini yazar."""

        def _setup(o: dict) -> float:
            """Setup ve sıra cezası TL toplamını döndürür."""
            return o["costSetupLira"] + o["costSequencePenaltyLira"]

        pth = os.path.join(grafikler_dir, dosya_ad)
        stacked_bar_kirilim(
            ["Serbest", "Yalniz 6t", "Yalniz 6.4t"],
            {
                "fire": [o0["costFireLira"], o6["costFireLira"], o64["costFireLira"]],
                "stok": [o0["costStockLira"], o6["costStockLira"], o64["costStockLira"]],
                "setup": [_setup(o0), _setup(o6), _setup(o64)],
            },
            pth,
            baslik=f"Maliyet kirilimi (TL) — {cf_etik}",
            y_label="TL",
            alt_baslik="Fire / Stok / Setup (sira cezasi setup ile)",
        )
        return pth

    p100 = _kirilim_png("karsilastirma_cf100_maliyet_kirilim.png", "cf=100", o0_100, o6_100, o64_100)
    p150 = _kirilim_png("karsilastirma_cf150_maliyet_kirilim.png", "cf=150", o0_150, o6_150, o64_150)
    yollar.extend([p100, p150])
    return yollar


class TestRaporVeriFireMaliyetiKarsilastirma(unittest.TestCase):
    """
    Sabit rulolar (6, 6, 6.4, 6.4) ile cf=100 / cf=150 ve 6 t vs 6,4 t rulo ailesi maliyetleri.
    """

    def test_ayni_rulolar_farkli_fire_ve_rulo_ailesi_karsilastirmasi(self):
        """
        Her cf için serbest, yalnız 6 t ve yalnız 6,4 t açılabilir senaryolar Optimal olmalı;
        her çözümde TL kırılımı totalCost ile tutarlı olmalıdır.
        """
        paket_100 = _senaryo_satirlari_hesapla(100.0)
        paket_150 = _senaryo_satirlari_hesapla(150.0)
        for cf, paket in ((100.0, paket_100), (150.0, paket_150)):
            o0, o6, o64, s0, s6, s64 = paket
            with self.subTest(cf=cf):
                self.assertEqual(s0, "Optimal", msg="Serbest senaryo çözülemeli")
                self.assertEqual(s6, "Optimal", msg="Yalnız 6 t maskesi çözülemeli")
                self.assertEqual(s64, "Optimal", msg="Yalnız 6,4 t maskesi çözülemeli")
                for etiket, o in (("serbest", o0), ("sadece6t", o6), ("sadece64t", o64)):
                    with self.subTest(cf=cf, senaryo=etiket):
                        self.assertAlmostEqual(
                            o["totalCost"],
                            _maliyet_kirilimi_toplami(o),
                            places=1,
                            msg=f"cf={cf} {etiket}: kırılım totalCost ile uyumlu olmalı",
                        )

        _tablo_cf_alti_senaryo(paket_100, paket_150)

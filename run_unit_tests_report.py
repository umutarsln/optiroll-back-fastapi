"""
Birim test runner ve XLSX raporu üreticisi.

test_optimization_consistency, test_stock_orders_process, test_supabase_analytics_persistence,
test_rapor_veri_karsilastirma
unittest modüllerini çalıştırır; sonuçları `backend/reports/unit_tests_<ts>/` klasörüne
`ozet.xlsx`, `rapor.md`, ve pie/bar grafikleri olarak yazar.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import unittest
from typing import Any, Dict, List, Tuple

from thesis_chart_builder import pass_fail_pie, test_suresi_bar
from thesis_report_common import (
    coklu_satir_csv_yaz,
    karsilastirma_md_yaz,
    simdi_ts,
    suite_kok_olustur,
)
from thesis_xlsx_report import karsilastirma_xlsx


class _TimedResult(unittest.TextTestResult):
    """
    Test başı süre ölçümü yapan TextTestResult varyantı. Her test için başlangıç
    zamanını saklar; addSuccess / addFailure / addError üzerinde elapsed hesaplar.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """
        TextTestResult üzerine her test için başlangıç/bitiş zamanı sözlükleri ekler.

        Args:
            *args: TextTestResult argümanları
            **kwargs: TextTestResult argümanları
        """
        super().__init__(*args, **kwargs)
        self._start_times: Dict[str, float] = {}
        self.timings: Dict[str, float] = {}
        self.pass_set: List[str] = []
        self.fail_set: List[Tuple[str, str]] = []
        self.error_set: List[Tuple[str, str]] = []

    def startTest(self, test: unittest.TestCase) -> None:
        """
        Test başlamadan zaman damgası kaydeder.

        Args:
            test: Çalışan TestCase
        """
        super().startTest(test)
        self._start_times[str(test)] = time.perf_counter()

    def stopTest(self, test: unittest.TestCase) -> None:
        """
        Test bitiminde elapsed kaydeder.

        Args:
            test: Biten TestCase
        """
        super().stopTest(test)
        key = str(test)
        if key in self._start_times:
            self.timings[key] = time.perf_counter() - self._start_times.pop(key)

    def addSuccess(self, test: unittest.TestCase) -> None:
        """
        PASS listesini tutar.

        Args:
            test: Geçen TestCase
        """
        super().addSuccess(test)
        self.pass_set.append(str(test))

    def addFailure(self, test: unittest.TestCase, err: Any) -> None:
        """
        FAIL listesini tutar.

        Args:
            test: Kalan TestCase
            err: Hata demeti
        """
        super().addFailure(test, err)
        self.fail_set.append((str(test), self._exc_info_to_string(err, test)))

    def addError(self, test: unittest.TestCase, err: Any) -> None:
        """
        ERROR listesini tutar.

        Args:
            test: Hata atan TestCase
            err: Hata demeti
        """
        super().addError(test, err)
        self.error_set.append((str(test), self._exc_info_to_string(err, test)))


def _modul_tara(modul_adlari: List[str]) -> unittest.TestSuite:
    """
    Verilen modül adlarından toplu TestSuite üretir.

    Args:
        modul_adlari: Modül dotted-path listesi (backend dizininde)

    Returns:
        Toplu TestSuite
    """
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for ad in modul_adlari:
        try:
            suite.addTests(loader.loadTestsFromName(ad))
        except Exception as exc:  # noqa: BLE001
            print(f"UYARI: {ad} yüklenemedi: {exc}")
    return suite


def _satirlar_derle(result: _TimedResult) -> List[Dict[str, Any]]:
    """
    Detay sheet için test başına satırlar üretir.

    Args:
        result: Süre takipli test sonucu

    Returns:
        Her test için dict (Test, Durum, Süre (sn), Detay)
    """
    satirlar: List[Dict[str, Any]] = []
    for name in result.pass_set:
        satirlar.append({"Test": name, "Durum": "PASS", "Süre (sn)": round(result.timings.get(name, 0), 4), "Detay": ""})
    for name, tb in result.fail_set:
        satirlar.append({"Test": name, "Durum": "FAIL", "Süre (sn)": round(result.timings.get(name, 0), 4), "Detay": tb.splitlines()[-1] if tb else ""})
    for name, tb in result.error_set:
        satirlar.append({"Test": name, "Durum": "ERROR", "Süre (sn)": round(result.timings.get(name, 0), 4), "Detay": tb.splitlines()[-1] if tb else ""})
    satirlar.sort(key=lambda r: r["Test"])
    return satirlar


def run(suite_kok: str, modul_adlari: List[str]) -> Dict[str, Any]:
    """
    Tüm unittest modüllerini çalıştırır, XLSX/MD/CSV ve grafikleri yazar.

    Args:
        suite_kok: Çıktı klasörü
        modul_adlari: Yüklenecek test modülleri

    Returns:
        Özet sözlüğü: toplam, passed, failed, errors, süre
    """
    os.makedirs(suite_kok, exist_ok=True)
    grafikler_dir = os.path.join(suite_kok, "grafikler")
    os.makedirs(grafikler_dir, exist_ok=True)

    suite = _modul_tara(modul_adlari)
    start = time.perf_counter()
    runner = unittest.TextTestRunner(
        resultclass=_TimedResult,
        verbosity=2,
        stream=sys.stdout,
    )
    result = runner.run(suite)
    elapsed = time.perf_counter() - start

    total = result.testsRun
    failed = len(result.fail_set) if isinstance(result, _TimedResult) else len(result.failures)
    errors = len(result.error_set) if isinstance(result, _TimedResult) else len(result.errors)
    passed = total - failed - errors

    pie_path = pass_fail_pie(
        passed, failed, errors,
        os.path.join(grafikler_dir, "pass_fail_pie.png"),
        baslik=f"Birim Test Sonuçları ({total} test)",
    )
    if isinstance(result, _TimedResult):
        satirlar = _satirlar_derle(result)
        bar_path = test_suresi_bar(
            [r["Test"] for r in satirlar],
            [r["Süre (sn)"] for r in satirlar],
            os.path.join(grafikler_dir, "test_suresi_bar.png"),
        )
    else:
        satirlar = []
        bar_path = ""

    karsilastirma_maliyet_grafikleri: List[str] = []
    if "test_rapor_veri_karsilastirma" in modul_adlari:
        try:
            from test_rapor_veri_karsilastirma import rapor_karsilastirma_grafikleri_uret

            karsilastirma_maliyet_grafikleri = rapor_karsilastirma_grafikleri_uret(grafikler_dir)
        except Exception as exc:  # noqa: BLE001
            print(f"UYARI: Karşılaştırma maliyet grafikleri üretilemedi: {exc}")

    ozet_satirlar = [
        {"Metrik": "Toplam Test", "Değer": total},
        {"Metrik": "PASS", "Değer": passed},
        {"Metrik": "FAIL", "Değer": failed},
        {"Metrik": "ERROR", "Değer": errors},
        {"Metrik": "Toplam Süre (sn)", "Değer": round(elapsed, 3)},
    ]
    detay_rows = [[r["Test"], r["Durum"], r["Süre (sn)"], r["Detay"]] for r in satirlar]

    xlsx_path = os.path.join(suite_kok, "ozet.xlsx")
    tum_grafikler = [p for p in [pie_path, bar_path, *karsilastirma_maliyet_grafikleri] if p]
    karsilastirma_xlsx(
        ozet_satirlar, xlsx_path,
        grafik_yollari=tum_grafikler,
        baslik="BİRİM TEST — ÖZET",
        ek_sheetler={
            "Detay": [["Test", "Durum", "Süre (sn)", "Detay"]] + detay_rows
        },
    )

    csv_path = os.path.join(suite_kok, "metrikler.csv")
    coklu_satir_csv_yaz(csv_path, satirlar, ["Test", "Durum", "Süre (sn)", "Detay"])

    md_path = os.path.join(suite_kok, "rapor.md")
    md_basliklari = ["Test", "Durum", "Süre (sn)", "Detay"]
    md_satirlar = [[r["Test"], r["Durum"], r["Süre (sn)"], (r["Detay"] or "")[:80]] for r in satirlar]
    md_grafikler = ["grafikler/pass_fail_pie.png"]
    if bar_path:
        md_grafikler.append("grafikler/test_suresi_bar.png")
    for _p in karsilastirma_maliyet_grafikleri:
        md_grafikler.append(os.path.relpath(_p, suite_kok))
    karsilastirma_md_yaz(
        md_path,
        baslik=f"Birim Test Raporu — {total} test, {passed} PASS / {failed} FAIL / {errors} ERROR",
        aciklama=f"Toplam süre: {elapsed:.3f} sn · Modüller: {', '.join(modul_adlari)}",
        tablo_basliklari=md_basliklari,
        tablo_satirlari=md_satirlar,
        grafik_listesi=md_grafikler,
        ek_yorum="PNG grafikleri bu MD dosyasının yanındaki `grafikler/` altında.",
    )

    return {"total": total, "passed": passed, "failed": failed, "errors": errors, "elapsed": elapsed}


def main() -> None:
    """
    CLI giriş noktası.
    """
    parser = argparse.ArgumentParser(description="Birim test runner + XLSX raporu")
    parser.add_argument("--out", default="", help="Çıktı klasörü (boşsa backend/reports/unit_tests_<ts>)")
    parser.add_argument(
        "--modules",
        nargs="+",
        default=[
            "test_optimization_consistency",
            "test_stock_orders_process",
            "test_supabase_analytics_persistence",
            "test_rapor_veri_karsilastirma",
        ],
        help="Çalıştırılacak unittest modülleri (dotted path)",
    )
    args = parser.parse_args()

    base = os.path.dirname(os.path.abspath(__file__))
    if base not in sys.path:
        sys.path.insert(0, base)
    ts = simdi_ts()
    suite_kok = args.out or suite_kok_olustur(os.path.join(base, "reports"), f"unit_tests_{ts}")
    os.makedirs(suite_kok, exist_ok=True)

    summary = run(suite_kok, list(args.modules))
    print(f"\nBirim test özeti: {summary['passed']}/{summary['total']} PASS "
          f"(fail={summary['failed']}, error={summary['errors']}, {summary['elapsed']:.2f}s)")
    print(f"Rapor: {suite_kok}")


if __name__ == "__main__":
    main()

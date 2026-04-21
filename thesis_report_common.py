"""
Tez rapor paketleri için ortak yardımcılar.

- Suite klasörü ve alt klasör iskeletini kurar.
- Senaryo slug üretici, axis_value → klasör ismi dönüştürücü.
- rapor.md, INDEX.md, baseline_ozeti.md yazıcıları.
- Tek satır CSV ve csv.DictWriter tabanlı karşılaştırma CSV yazıcıları.
"""

from __future__ import annotations

import csv
import os
import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence


def simdi_ts() -> str:
    """
    Suite kök klasörü için standart YYYYMMDD_HHMMSS damgası üretir.

    Returns:
        Zaman damgası dizgisi
    """
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def safe_slug(s: str, maks: int = 80) -> str:
    """
    Metni dosya sistemi uyumlu ASCII slug'a dönüştürür.

    Args:
        s: Kaynak metin
        maks: Maksimum uzunluk

    Returns:
        Küçük harfli, tire/alt çizgi/rakam içeren slug
    """
    tr_map = str.maketrans(
        {"ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g",
         "ü": "u", "Ü": "u", "ö": "o", "Ö": "o", "ç": "c", "Ç": "c"}
    )
    s2 = s.translate(tr_map)
    s2 = s2.lower()
    s2 = re.sub(r"[^a-z0-9]+", "_", s2)
    s2 = re.sub(r"_+", "_", s2).strip("_")
    if not s2:
        s2 = "x"
    return s2[:maks]


def senaryo_klasoru_adi(index_1_based: int, aciklama: str) -> str:
    """
    `test_<N>_<slug>` biçiminde senaryo klasör adı üretir.

    Args:
        index_1_based: 1'den başlayan sıra numarası
        aciklama: Slug'a dönüştürülecek açıklama

    Returns:
        Klasör adı
    """
    return f"test_{int(index_1_based)}_{safe_slug(aciklama)}"


def axis_deger_klasoru(axis_value: Any) -> str:
    """
    OFAT axis_value için klasör adı üretir (ör. 0.5 → carpan_0p5, 10 → deger_10).

    Args:
        axis_value: int/float/str

    Returns:
        Klasör adı
    """
    try:
        f = float(axis_value)
        if f.is_integer():
            return f"deger_{int(f)}"
        return "carpan_" + f"{f:.3f}".rstrip("0").rstrip(".").replace(".", "p")
    except (TypeError, ValueError):
        return "deger_" + safe_slug(str(axis_value), 40)


def suite_kok_olustur(kok: str, ad: str) -> str:
    """
    Suite kök klasörünü oluşturur.

    Args:
        kok: Ana reports dizini
        ad: Suite klasör adı (ör. thesis_suite_20260420_2030)

    Returns:
        Suite kök tam yolu
    """
    path = os.path.join(kok, ad)
    os.makedirs(path, exist_ok=True)
    return path


def senaryo_klasoru_hazirla(
    suite_kok: str,
    index_1_based: int,
    aciklama: str,
    *,
    grafikler_alt_klasor: bool = True,
) -> Dict[str, str]:
    """
    Bir senaryo için alt klasörü ve gereken alt dizinleri hazırlar.

    Args:
        suite_kok: Suite kök klasörü
        index_1_based: Sıra numarası
        aciklama: Slug
        grafikler_alt_klasor: True ise grafikler/ alt klasörünü da oluşturur

    Returns:
        {"klasor", "cozum_raporu", "rapor_md", "metrikler_csv", "grafikler_dir"}
    """
    klasor = os.path.join(suite_kok, senaryo_klasoru_adi(index_1_based, aciklama))
    os.makedirs(klasor, exist_ok=True)
    out = {
        "klasor": klasor,
        "cozum_raporu": os.path.join(klasor, "cozum_raporu.xlsx"),
        "rapor_md": os.path.join(klasor, "rapor.md"),
        "metrikler_csv": os.path.join(klasor, "metrikler.csv"),
    }
    if grafikler_alt_klasor:
        g = os.path.join(klasor, "grafikler")
        os.makedirs(g, exist_ok=True)
        out["grafikler_dir"] = g
    return out


def karsilastirma_klasoru_hazirla(suite_kok: str) -> Dict[str, str]:
    """
    `_karsilastirma/` alt klasörünü ve grafikler/ dizinini oluşturur.

    Args:
        suite_kok: Suite kök klasörü

    Returns:
        {"klasor", "grafikler_dir", "karsilastirma_xlsx", "karsilastirma_md", "karsilastirma_csv"}
    """
    klasor = os.path.join(suite_kok, "_karsilastirma")
    os.makedirs(klasor, exist_ok=True)
    g = os.path.join(klasor, "grafikler")
    os.makedirs(g, exist_ok=True)
    return {
        "klasor": klasor,
        "grafikler_dir": g,
        "karsilastirma_xlsx": os.path.join(klasor, "karsilastirma.xlsx"),
        "karsilastirma_md": os.path.join(klasor, "karsilastirma.md"),
        "karsilastirma_csv": os.path.join(klasor, "karsilastirma.csv"),
    }


def tek_satir_csv_yaz(path: str, row: Dict[str, Any], baslik_sirasi: Optional[Sequence[str]] = None) -> None:
    """
    Tek satırlık metrik CSV'si yazar.

    Args:
        path: CSV yolu
        row: Sütun adı → değer
        baslik_sirasi: İstenen sütun sırası (None ise row.keys)
    """
    keys = list(baslik_sirasi) if baslik_sirasi else list(row.keys())
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerow({k: row.get(k, "") for k in keys})


def coklu_satir_csv_yaz(path: str, rows: Sequence[Dict[str, Any]],
                        baslik_sirasi: Optional[Sequence[str]] = None) -> None:
    """
    Birden fazla satırı CSV'ye yazar.

    Args:
        path: CSV yolu
        rows: Satır sözlükleri listesi
        baslik_sirasi: Sütun sırası (None ise ilk satırın anahtarları)
    """
    if not rows:
        return
    keys = list(baslik_sirasi) if baslik_sirasi else list(rows[0].keys())
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in keys})


def rapor_md_yaz(
    path: str,
    *,
    baslik: str,
    girdi_ozeti: str,
    sonuc: Dict[str, Any],
    metrikler: Dict[str, Any],
    kesim_senaryosu_metni: str = "",
    dosya_listesi: Optional[Sequence[str]] = None,
    ek_bolumler: Optional[Sequence[Dict[str, str]]] = None,
) -> None:
    """
    Bir senaryoya ait rapor.md dosyasını yazar.

    Args:
        path: rapor.md yolu
        baslik: Ana başlık
        girdi_ozeti: Girdi özet cümlesi
        sonuc: phase / solver_status / failure_code / passed / hints
        metrikler: metrik adı → değer
        kesim_senaryosu_metni: build_kesim_snapshot metni
        dosya_listesi: Bu klasördeki referans dosyalar
        ek_bolumler: [{baslik, icerik}, ...] serbest ek bölümler
    """
    satirlar: List[str] = [f"# {baslik}", ""]
    satirlar.append(f"**Girdi özeti:** {girdi_ozeti}")
    satirlar.append("")
    satirlar.append(
        f"**Sonuç:** phase=`{sonuc.get('phase')}` · solver=`{sonuc.get('solver_status')}`"
        f" · failure_code=`{sonuc.get('failure_code')}` · durum=`{sonuc.get('passed', '')}`"
    )
    satirlar.append("")
    hints = sonuc.get("hints") or []
    if hints:
        satirlar.append("**İpuçları:**")
        for h in hints:
            satirlar.append(f"- {h}")
        satirlar.append("")
    satirlar.append("## Metrikler")
    satirlar.append("")
    satirlar.append("| Metrik | Değer |")
    satirlar.append("|---|---:|")
    for k, v in metrikler.items():
        satirlar.append(f"| {k} | {v} |")
    satirlar.append("")
    if kesim_senaryosu_metni:
        satirlar.extend(["## Kesim Senaryosu", "", "```", kesim_senaryosu_metni, "```", ""])
    if ek_bolumler:
        for b in ek_bolumler:
            satirlar.append(f"## {b.get('baslik', '')}")
            satirlar.append("")
            satirlar.append(b.get("icerik", ""))
            satirlar.append("")
    if dosya_listesi:
        satirlar.extend(["## Dosyalar", ""])
        for d in dosya_listesi:
            satirlar.append(f"- `{d}`")
        satirlar.append("")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(satirlar))


def index_md_yaz(
    path: str,
    *,
    baslik: str,
    ts: str,
    baseline_ozeti: str,
    senaryolar_tablosu: Sequence[Sequence[str]],
    ek_aciklama: str = "",
) -> None:
    """
    Suite kök INDEX.md dosyasını yazar.

    Args:
        path: INDEX.md yolu
        baslik: Ana başlık
        ts: Zaman damgası
        baseline_ozeti: Baseline özet metni
        senaryolar_tablosu: Tablo verisi; ilk satır başlık
        ek_aciklama: Uç uca metin
    """
    satirlar = [f"# {baslik}", "", f"**Koşum zamanı:** {ts}", ""]
    if baseline_ozeti:
        satirlar.extend(["## Baseline", "", baseline_ozeti, ""])
    if senaryolar_tablosu:
        basliklar = senaryolar_tablosu[0]
        satirlar.append("| " + " | ".join(basliklar) + " |")
        satirlar.append("|" + "|".join(["---"] * len(basliklar)) + "|")
        for row in senaryolar_tablosu[1:]:
            satirlar.append("| " + " | ".join(str(c) for c in row) + " |")
        satirlar.append("")
    satirlar.extend(
        [
            "## Karşılaştırma",
            "",
            "- `_karsilastirma/karsilastirma.xlsx` — tüm senaryolar tek tabloda",
            "- `_karsilastirma/grafikler/` — bar / stacked / line / trend grafikleri",
            "",
        ]
    )
    if ek_aciklama:
        satirlar.extend([ek_aciklama, ""])
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(satirlar))


def baseline_ozet_md_yaz(path: str, baslik: str, icerik_satirlari: Iterable[str]) -> None:
    """
    Suite kökünde baseline_ozeti.md dosyasını yazar.

    Args:
        path: Dosya yolu
        baslik: Ana başlık
        icerik_satirlari: Madde madde satırlar
    """
    satirlar = [f"# {baslik}", ""]
    for s in icerik_satirlari:
        satirlar.append(f"- {s}")
    satirlar.append("")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(satirlar))


def karsilastirma_md_yaz(
    path: str,
    *,
    baslik: str,
    aciklama: str,
    tablo_basliklari: Sequence[str],
    tablo_satirlari: Sequence[Sequence[Any]],
    grafik_listesi: Optional[Sequence[str]] = None,
    ek_yorum: str = "",
) -> None:
    """
    _karsilastirma/karsilastirma.md dosyasını yazar.

    Args:
        path: Yazılacak yol
        baslik: Başlık
        aciklama: Kısa açıklama
        tablo_basliklari: Tablo başlıkları
        tablo_satirlari: Tablo satırları
        grafik_listesi: Göreli grafik yolu listesi
        ek_yorum: Sonuç yorumu
    """
    satirlar = [f"# {baslik}", ""]
    if aciklama:
        satirlar.extend([aciklama, ""])
    if tablo_basliklari and tablo_satirlari:
        satirlar.append("| " + " | ".join(tablo_basliklari) + " |")
        satirlar.append("|" + "|".join(["---"] * len(tablo_basliklari)) + "|")
        for row in tablo_satirlari:
            satirlar.append("| " + " | ".join(str(c) for c in row) + " |")
        satirlar.append("")
    if grafik_listesi:
        satirlar.extend(["## Grafikler", ""])
        for g in grafik_listesi:
            if not g:
                continue
            name = os.path.basename(g)
            satirlar.append(f"- ![{name}]({g})")
        satirlar.append("")
    if ek_yorum:
        satirlar.extend(["## Yorum", "", ek_yorum, ""])
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(satirlar))


def metrik_satiri_derle(
    *,
    senaryo_adi: str,
    girdi_ozeti: str,
    sonuc: Dict[str, Any],
    scenario_meta: Dict[str, Any],
    passed: str = "",
) -> Dict[str, Any]:
    """
    test_calistir çıktısı + meta bilgisinden karşılaştırma satırı (CSV/tablolar) üretir.

    Args:
        senaryo_adi: Senaryo adı
        girdi_ozeti: Girdi açıklaması
        sonuc: test_calistir çıktısı
        scenario_meta: scenario_meta sözlüğü (maliyetler, toplam_rulo_kapasitesi_ton, ...)
        passed: "PASS" / "FAIL" (opsiyonel)

    Returns:
        Satır sözlüğü (birleşik alan seti)
    """
    maliyetler = scenario_meta.get("maliyetler") or {}
    fire = float(sonuc.get("toplam_fire") or 0.0)
    stok = float(sonuc.get("toplam_stok") or 0.0)
    acilan = int(sonuc.get("kullanilan_rulo_sayisi") or 0)
    tc = float(sonuc.get("toplam_maliyet") or 0.0)
    fc = float(maliyetler.get("fire_cost") or 0.0)
    sc = float(maliyetler.get("stock_cost") or 0.0)
    sup = float(maliyetler.get("setup_cost") or 0.0)
    kapasite = float(scenario_meta.get("toplam_rulo_kapasitesi_ton") or 0.0)
    talep = float(scenario_meta.get("toplam_talep_ton") or 0.0)
    kullanilan_ton = 0.0
    raw = sonuc.get("raw") or {}
    res = raw.get("results") or {}
    if res:
        kullanilan_ton = sum(float(r.get("used", 0) or 0) for r in (res.get("rollStatus") or []))
    return {
        "senaryo_adi": senaryo_adi,
        "girdi_ozeti": girdi_ozeti,
        "phase": sonuc.get("phase"),
        "solver_status": sonuc.get("solver_status"),
        "failure_code": sonuc.get("failure_code") or "",
        "passed": passed,
        "toplam_fire": round(fire, 4),
        "toplam_stok": round(stok, 4),
        "kullanilan_ton": round(kullanilan_ton, 4),
        "acilan_rulo": acilan,
        "rulo_degisim_sayisi": int(sonuc.get("rulo_degisim_sayisi") or 0),
        "uretim_hatti_rulo_gecis_sayisi": int(sonuc.get("uretim_hatti_rulo_gecis_sayisi") or 0),
        "toplam_talep_ton": round(talep, 4),
        "toplam_rulo_kapasitesi_ton": round(kapasite, 2),
        "rulo_kapasiteleri_str": sonuc.get("rulo_kapasiteleri_str") or "",
        "toplam_maliyet": round(tc, 2),
        "fire_maliyet": round(fire * fc, 2),
        "stok_maliyet": round(stok * sc, 2),
        "setup_maliyet": round(acilan * sup, 2),
        "fire_orani_pct": round(100.0 * fire / kapasite, 3) if kapasite > 0 else 0.0,
        "kapasite_kullanim_pct": round(100.0 * kullanilan_ton / kapasite, 3) if kapasite > 0 else 0.0,
        "kesim_senaryosu_metni": (sonuc.get("kesim_senaryosu_metni") or "")[:500],
        "hints": "; ".join(sonuc.get("hints") or []),
        "mesaj": sonuc.get("mesaj") or "",
    }

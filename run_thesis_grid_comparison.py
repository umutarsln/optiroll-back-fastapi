"""
Genişletilmiş tez grid karşılaştırması: 4–13 t toplam kapasite, 4–5 sipariş (800–3500 m² profili),
5–10 fiziksel rulo bölmesi (her kapasite×sipariş için birden çok rulo sayısı); senaryolar sırayla
çalıştırılır, CSV + konsol kıyas özeti üretilir.
"""

from __future__ import annotations

import argparse
import csv
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Sequence, Tuple

from main import SURFACE_FACTOR_OPTIMIZE
from optimizer import calculate_demand
from thesis_ofat_baseline import baseline_orders_multi, multi_order_m2_values
from thesis_test_harness import test_calistir

# Malzeme: test_calistir ile aynı (mm, g/cm³)
_DEFAULT_THICKNESS_MM = 0.75
_DEFAULT_DENSITY = 7.85

# Her (kapasite, sipariş) için 5–10 bandında alt / orta / üst üç rulo sayısı (CLI ile değiştirilebilir)
_DEFAULT_RULO_VARYANTLARI: Tuple[int, ...] = (5, 7, 10)


def rulo_varyantlari_parse_et(virgullu: str) -> Tuple[int, ...]:
    """
    --rulo-varyantlari argümanından tamsayı listesi üretir; değerleri 5–10 aralığına sıkıştırır.

    Args:
        virgullu: Örn. "5,7,10"

    Returns:
        En az bir elemanlı rulo sayıları demeti
    """
    raw = [p.strip() for p in virgullu.split(",") if p.strip()]
    out: List[int] = []
    for p in raw:
        v = int(p)
        v = max(5, min(10, v))
        out.append(v)
    return tuple(out) if out else _DEFAULT_RULO_VARYANTLARI


def talep_ton_hesapla(n_orders: int, scale_m2: float) -> float:
    """
    Verilen sipariş sayanı ve m² ölçeği için toplam talep tonunu hesaplar (çift yüzey çarpanı ile).

    Args:
        n_orders: Sipariş adedi (4 veya 5)
        scale_m2: 800–3500 profiline uygulanan çarpan

    Returns:
        Toplam talep tonu
    """
    orders = baseline_orders_multi(n_orders, scale_m2)
    orders_list = [
        {
            "m2": float(o["m2"]),
            "panelWidth": float(o["panelWidth"]),
            "panelLength": float(o.get("panelLength", 1.0) or 1.0),
        }
        for o in orders
    ]
    pw = [o["panelWidth"] for o in orders_list]
    pl = [o["panelLength"] for o in orders_list]
    _, total = calculate_demand(
        orders_list,
        _DEFAULT_THICKNESS_MM,
        _DEFAULT_DENSITY,
        pw,
        pl,
        SURFACE_FACTOR_OPTIMIZE,
    )
    return float(total)


def olcek_bul_kapasite_icin(toplam_kapasite_ton: float, n_orders: int, hedef_kullanim_orani: float) -> float:
    """
    İkili arama ile talep tonunu kapasitenin belirli bir oranına sığdıracak m² ölçeğini bulur.

    Args:
        toplam_kapasite_ton: Toplam rulo kapasitesi (ton)
        n_orders: Sipariş sayısı
        hedef_kullanim_orani: Talep / kapasite hedef üst sınırı (ör. 0.88)

    Returns:
        baseline_orders_multi için scale_m2 çarpanı
    """
    hedef_talep = float(toplam_kapasite_ton) * hedef_kullanim_orani
    lo, hi = 1e-10, 4.0
    for _ in range(55):
        mid = (lo + hi) / 2.0
        if talep_ton_hesapla(n_orders, mid) <= hedef_talep:
            lo = mid
        else:
            hi = mid
    return float(lo * 0.997)


def senaryo_listesi_uret(rulo_varyantlari: Sequence[int] | None = None) -> List[Dict[str, Any]]:
    """
    4–13 t kapasite × 4/5 sipariş × her çift için birden fazla fiziksel rulo sayısı senaryoları üretir.

    Args:
        rulo_varyantlari: 5–10 arası istenen fiziksel rulo sayıları (ör. 5, 7, 10); None ise varsayılan demet

    Returns:
        Her öğe: toplam_ton, n_orders, physical_roll_count, senaryo_no, rulo_varyant_etiketi
    """
    varyant = tuple(rulo_varyantlari) if rulo_varyantlari is not None else _DEFAULT_RULO_VARYANTLARI
    kapasiteler = [4, 5, 6, 7, 8, 9, 10, 11, 12, 13]
    siparis_adetleri = [4, 5]
    out: List[Dict[str, Any]] = []
    idx = 0
    etiketler = ("dusuk", "orta", "yuksek", "v4", "v5", "v6", "v7", "v8", "v9", "v10")
    for cap in kapasiteler:
        for n_ord in siparis_adetleri:
            for j, n_phys in enumerate(varyant):
                etiket = etiketler[j] if j < len(etiketler) else f"v{j + 1}"
                out.append(
                    {
                        "toplam_ton": float(cap),
                        "n_orders": n_ord,
                        "physical_roll_count": int(n_phys),
                        "senaryo_no": idx + 1,
                        "rulo_varyant_etiketi": etiket,
                    }
                )
                idx += 1
    return out


def satir_olustur(
    senaryo: Dict[str, Any],
    scale: float,
    r: Dict[str, Any],
) -> Dict[str, Any]:
    """
    CSV için tek senaryo satırını düzleştirir.

    Args:
        senaryo: Senaryo tanımı
        scale: Kullanılan m² ölçeği
        r: test_calistir çıktısı

    Returns:
        Tablo satırı sözlüğü
    """
    m2_ornek = multi_order_m2_values(int(senaryo["n_orders"]))
    m2_min = round(m2_ornek[0] * scale, 2)
    m2_max = round(m2_ornek[-1] * scale, 2)
    raw = r.get("raw") or {}
    res = raw.get("results") or {}
    sm = res.get("summary") or {}
    lts = res.get("lineTransitionsSummary") or {}
    return {
        "senaryo_no": senaryo["senaryo_no"],
        "toplam_kapasite_ton": senaryo["toplam_ton"],
        "siparis_sayisi": senaryo["n_orders"],
        "rulo_varyant": senaryo.get("rulo_varyant_etiketi", ""),
        "fiziksel_rulo_sayisi_istek": senaryo["physical_roll_count"],
        "m2_olcegi": round(scale, 8),
        "siparis_m2_araligi": f"{m2_min}-{m2_max}",
        "talep_ton": r.get("toplam_talep_ton"),
        "phase": r.get("phase"),
        "solver_status": r.get("solver_status"),
        "failure_code": r.get("failure_code"),
        "toplam_maliyet": r.get("toplam_maliyet"),
        "toplam_fire": r.get("toplam_fire"),
        "toplam_stok": r.get("toplam_stok"),
        "acilan_rulo": r.get("kullanilan_rulo_sayisi"),
        "kesim_rulo_degisim": r.get("rulo_degisim_sayisi"),
        "hat_rulo_gecis": r.get("uretim_hatti_rulo_gecis_sayisi"),
        "hat_es_zaman": lts.get("synchronousChanges"),
        "hat_bagimsiz": lts.get("independentChanges"),
        "adim_sayisi": lts.get("stepCount"),
        "rollChangeCount_summary": sm.get("rollChangeCount"),
        "mesaj": (r.get("mesaj") or "")[:200],
    }


def kiyas_ozeti_yaz(rows: List[Dict[str, Any]]) -> None:
    """
    Optimal satırlar için maliyet, fire ve hat geçişine göre sıralı kısa konsol özetleri yazar.

    Args:
        rows: satir_olustur çıktıları
    """
    opt = [x for x in rows if x.get("solver_status") == "Optimal"]
    if not opt:
        print("\n[Özet] Optimal senaryo yok; kıyas yapılamadı.")
        return
    print("\n=== Karşılaştırma (yalnızca Optimal) ===")
    by_cost = sorted(opt, key=lambda x: float(x.get("toplam_maliyet") or 1e18))
    by_fire = sorted(opt, key=lambda x: float(x.get("toplam_fire") or 1e18))
    by_hat = sorted(opt, key=lambda x: int(x.get("hat_rulo_gecis") or 9999))

    def line(prefix: str, lst: List[Dict[str, Any]], k: int = 5) -> None:
        print(f"\n{prefix} (ilk {min(k, len(lst))}):")
        for x in lst[:k]:
            print(
                f"  #{x['senaryo_no']} | {x['toplam_kapasite_ton']}t | {x['siparis_sayisi']} sip | "
                f"n_rulo={x['fiziksel_rulo_sayisi_istek']} ({x.get('rulo_varyant', '')}) | "
                f"maliyet={x['toplam_maliyet']} | fire={x['toplam_fire']} | hat_gecis={x['hat_rulo_gecis']}"
            )

    line("En düşük toplam maliyet", by_cost)
    line("En düşük fire (ton)", by_fire)
    line("En az hat rulo geçişi", by_hat)
    print(
        "\nNot: Tek bir 'en optimal' tanımı yok; maliyet / fire / operasyon yükünü (hat geçişi) "
        "tabloda birlikte değerlendirin."
    )


def main() -> None:
    """
    Grid senaryolarını sırayla çalıştırır, CSV yazar ve konsola kıyas özeti basar.
    """
    parser = argparse.ArgumentParser(
        description="Tez grid karşılaştırma (4–13 t, 4–5 sipariş; her çift için çoklu 5–10 rulo)"
    )
    parser.add_argument(
        "--sleep-ms",
        type=int,
        default=0,
        help="Her senaryo sonrası bekleme (ms); GC/IO için isteğe bağlı",
    )
    parser.add_argument(
        "--hedef-kullanim",
        type=float,
        default=0.88,
        help="Talep tonunun toplam kapasiteye oranı üst hedefi (ön kontrol için)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Yalnızca ilk N senaryoyu çalıştır (hızlı doğrulama için)",
    )
    parser.add_argument(
        "--rulo-varyantlari",
        type=str,
        default="5,7,10",
        help="Her kapasite×sipariş için denenecek virgülle ayrılmış rulo sayıları (5–10, örn. 5,7,10)",
    )
    args = parser.parse_args()

    rv = rulo_varyantlari_parse_et(args.rulo_varyantlari)
    senaryolar = senaryo_listesi_uret(rv)
    if args.limit is not None:
        senaryolar = senaryolar[: max(0, int(args.limit))]
    if not senaryolar:
        print("Senaryo yok (--limit 0 veya boş liste). Çıkılıyor.")
        return
    print(f"Toplam {len(senaryolar)} senaryo sırayla çalıştırılacak.\n")

    tum_satirlar: List[Dict[str, Any]] = []

    for s in senaryolar:
        no = s["senaryo_no"]
        cap = s["toplam_ton"]
        n_ord = s["n_orders"]
        n_phys = s["physical_roll_count"]
        scale = olcek_bul_kapasite_icin(cap, n_ord, args.hedef_kullanim)
        talep_hesap = talep_ton_hesapla(n_ord, scale)

        rv_etik = s.get("rulo_varyant_etiketi", "")
        print(
            f"[{no}/{len(senaryolar)}] {cap}t | {n_ord} sipariş | {n_phys} rulo ({rv_etik}) | "
            f"m² ölçek={scale:.6f} | ön talep≈{talep_hesap:.4f}t"
        )

        orders = baseline_orders_multi(n_ord, scale)
        r = test_calistir(
            cap,
            orders,
            physical_roll_count=n_phys,
        )
        row = satir_olustur(s, scale, r)
        tum_satirlar.append(row)

        st = r.get("solver_status")
        print(
            f"    → {st} | maliyet={r.get('toplam_maliyet')} | fire={r.get('toplam_fire')} | "
            f"hat_gecis={r.get('uretim_hatti_rulo_gecis_sayisi')}\n"
        )

        if args.sleep_ms > 0:
            time.sleep(args.sleep_ms / 1000.0)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "reports",
        f"thesis_grid_compare_{ts}.csv",
    )
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    keys = list(tum_satirlar[0].keys()) if tum_satirlar else []
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for row in tum_satirlar:
            w.writerow(row)

    print(f"CSV yazıldı: {out_path}")
    kiyas_ozeti_yaz(tum_satirlar)


if __name__ == "__main__":
    main()

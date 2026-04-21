"""
Tez raporları: talep tonu, rulo kapasiteleri ve kesim planı özetleri (karşılaştırma için).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


def build_kesim_snapshot(
    demand_by_order: Dict[int, float],
    rolls_int: List[int],
    results: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Talep tonu, rulo listesi ve varsa optimal kesim planından okunabilir özet üretir.

    Args:
        demand_by_order: Sipariş indeksi -> talep tonu (calculate_demand çıktısı)
        rolls_int: Kullanılan rulo kapasiteleri (ton, tam sayı)
        results: solve_optimization sonuç sözlüğü veya None

    Returns:
        Rulo satırları, kesim planı roll bazlı gruplama ve kısa Türkçe metin içeren sözlük
    """
    tot_d = round(float(sum(demand_by_order.values())), 4)
    cap = int(sum(rolls_int))
    out: Dict[str, Any] = {
        "toplam_talep_ton": tot_d,
        "toplam_rulo_kapasitesi_ton": cap,
        "rulo_kapasiteleri_listesi": list(rolls_int),
        "rulo_kapasiteleri_str": "+".join(str(x) for x in rolls_int),
        "kapasite_ustluk_ton": round(cap - tot_d, 4),
    }
    if not results:
        out["kesim_durumu"] = "çözüm yok (ön kontrol veya Infeasible)"
        out["kesim_senaryosu_metni"] = out["kesim_durumu"]
        return out

    roll_status = results.get("rollStatus") or []
    rulo_satirlari: List[Dict[str, Any]] = []
    for rs in roll_status:
        rulo_satirlari.append(
            {
                "rulo_no": rs.get("rollId"),
                "kapasite_ton": rs.get("totalTonnage"),
                "kullanilan_ton": rs.get("used"),
                "fire_ton": rs.get("fire"),
                "stok_ton": rs.get("stock"),
                "rulodaki_farkli_siparis_sayisi": rs.get("ordersUsed"),
            }
        )
    out["rulo_satirlari"] = rulo_satirlari

    cutting_plan = results.get("cuttingPlan") or []
    by_roll: Dict[int, List[Dict[str, Any]]] = {}
    for row in cutting_plan:
        rid = int(row.get("rollId") or 0)
        by_roll.setdefault(rid, []).append(
            {
                "siparis_no": row.get("orderId"),
                "tonaj": row.get("tonnage"),
                "panel_sayisi": row.get("panelCount"),
                "m2": row.get("m2"),
            }
        )
    out["kesim_plani_roll_bazli"] = {
        str(k): v for k, v in sorted(by_roll.items(), key=lambda x: x[0])
    }
    out["kesim_plani_satir_sayisi"] = len(cutting_plan)
    out["kesim_senaryosu_metni"] = _kesim_metni_uret(rulo_satirlari, by_roll)
    out["kesim_durumu"] = "Optimal" if results.get("summary") else "bilinmiyor"
    return out


def _kesim_metni_uret(
    rulo_satirlari: List[Dict[str, Any]],
    by_roll: Dict[int, List[Dict[str, Any]]],
) -> str:
    """
    Rulo ve kesim planından kısa Türkçe özet paragraf üretir.

    Args:
        rulo_satirlari: Her rulo için kullanım özeti
        by_roll: Rulo no -> kesim planı satırları

    Returns:
        Birkaç cümlelik metin
    """
    parcalar: List[str] = []
    for rs in rulo_satirlari:
        rid = rs.get("rulo_no")
        if rid is None:
            continue
        det = by_roll.get(int(rid), [])
        sip_str = ", ".join(
            f"S{int(x.get('siparis_no', 0))}:{float(x.get('tonaj', 0) or 0):.3f}t"
            for x in det[:6]
        )
        if len(det) > 6:
            sip_str += f" …(+{len(det) - 6} satır)"
        parcalar.append(
            f"Rulo {rid}: kapasite {rs.get('kapasite_ton')}t, kullanılan {rs.get('kullanilan_ton')}t, "
            f"fire {rs.get('fire_ton')}t, stok {rs.get('stok_ton')}t; kesim: {sip_str or '—'}"
        )
    return " | ".join(parcalar) if parcalar else "Kesim detayı yok."


def kesim_json_kisa(snapshot: Dict[str, Any], max_len: int = 2000) -> str:
    """
    CSV hücresi için kesim özetini JSON string (kısaltılmış) olarak döner.

    Args:
        snapshot: build_kesim_snapshot çıktısı
        max_len: Maksimum karakter (fazlası kesilir)

    Returns:
        JSON metni
    """
    slim = {
        "talep_ton": snapshot.get("toplam_talep_ton"),
        "kapasite_ton": snapshot.get("toplam_rulo_kapasitesi_ton"),
        "rulolar": snapshot.get("rulo_kapasiteleri_listesi"),
        "rulo_satirlari": snapshot.get("rulo_satirlari"),
        "kesim_roll_bazli": snapshot.get("kesim_plani_roll_bazli"),
    }
    s = json.dumps(slim, ensure_ascii=False)
    if len(s) > max_len:
        return s[: max_len - 3] + "..."
    return s


def ofat_ne_degisti_aciklamasi(
    axis: str,
    axis_value: Any,
    baseline_meta: str,
) -> str:
    """
    OFAT satırında 'yalnızca şu değişti' açıklaması üretir.

    Args:
        axis: Değişen parametre adı
        axis_value: Yeni değer
        baseline_meta: Baseline sabit özeti (thesis_ofat_baseline)

    Returns:
        Tek satırlık Türkçe açıklama
    """
    return (
        f"OFAT: diğer tüm girdiler baseline ile aynı; bu satırda yalnızca `{axis}` = {axis_value}. "
        f"Baseline referansı: {baseline_meta}"
    )


def ofat_delta_vs_referans_satir(
    row: Dict[str, Any],
    referans: Dict[str, Any],
) -> str:
    """
    Aynı eksen grubundaki referans satıra göre fire, açılan rulo, kesim planı rulo değişimi ve stok farkını metinler.

    Args:
        row: Mevcut OFAT sonuç satırı
        referans: Aynı dalgada seçilen referans (ör. çarpan 1.0)

    Returns:
        Kısa fark özeti veya boş string
    """
    if not referans or row is referans:
        return ""
    try:
        df = float(row.get("totalFire") or 0) - float(referans.get("totalFire") or 0)
        dr = int(row.get("openedRolls") or 0) - int(referans.get("openedRolls") or 0)
        ds = float(row.get("totalStock") or 0) - float(referans.get("totalStock") or 0)
        ddeg = int(row.get("rollChangeCount") or 0) - int(referans.get("rollChangeCount") or 0)
        dh = int(row.get("lineRollTransitionCount") or 0) - int(
            referans.get("lineRollTransitionCount") or 0
        )
        return (
            f"ref’e göre: Δfire={df:+.4f}t, Δacilan_rulo={dr:+d}, Δkesim_deg={ddeg:+d}, "
            f"Δhat_gecis={dh:+d}, Δstok={ds:+.4f}t"
        )
    except (TypeError, ValueError):
        return ""

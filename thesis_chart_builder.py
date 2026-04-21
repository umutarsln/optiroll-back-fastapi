"""
Grafik üretici modül.

Senaryo kümeleri (tez doğrulama, OFAT, main batch) ve tek senaryolar için bar / line /
stacked bar / kesim şeması grafiklerini matplotlib ile üretir; çıktılar PNG dosyalarıdır.
Dönen yol listeleri thesis_xlsx_report.karsilastirma_xlsx'e veya build_cozum_raporu_xlsx'e
'Grafikler' sayfası olarak gömülebilir.

Bu modülü kullanırken `plt.rcParams['font.family']` DejaVu Sans olarak ayarlanır; böylece
Türkçe karakter sorunu yaşanmaz.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

import matplotlib

matplotlib.use("Agg")  # noqa: E402 — headless ortamda GUI backend gerekmez
import matplotlib.pyplot as plt  # noqa: E402

plt.rcParams["font.family"] = "DejaVu Sans"
plt.rcParams["axes.unicode_minus"] = False

SENARYO_RENKLERI = (
    "#4C78A8",
    "#F58518",
    "#54A24B",
    "#E45756",
    "#72B7B2",
    "#B279A2",
    "#EECA3B",
    "#9D755D",
    "#BAB0AC",
)
REFERANS_RENGI = "#888888"
FIRE_RENGI = "#E45756"
STOK_RENGI = "#4C78A8"
KULLANILAN_RENGI = "#54A24B"
SETUP_RENGI = "#72B7B2"


def _ensure_dir(path: str) -> None:
    """
    Klasör yoksa oluşturur.

    Args:
        path: Hedef klasör yolu
    """
    os.makedirs(path, exist_ok=True)


def _save_fig(fig, output_path: str) -> str:
    """
    Figürü 150 dpi, tight bbox ile kaydeder ve kapatır.

    Args:
        fig: matplotlib Figure
        output_path: .png tam yolu

    Returns:
        Kaydedilen dosya yolu
    """
    _ensure_dir(os.path.dirname(output_path) or ".")
    fig.tight_layout()
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return output_path


def _senaryo_ad_kisalt(ad: str, maks: int = 18) -> str:
    """
    Uzun senaryo adlarını bar etiketlerinde kısaltır.

    Args:
        ad: Senaryo adı
        maks: Maksimum karakter

    Returns:
        Kısa etiket
    """
    if len(ad) <= maks:
        return ad
    return ad[: maks - 1] + "…"


def _bar_degerleri_yaz(ax, bars, fmt: str = "{:.2f}") -> None:
    """
    Bar üstüne değer etiketi yazar.

    Args:
        ax: matplotlib axes
        bars: Bar container
        fmt: Sayı biçimi
    """
    for b in bars:
        h = b.get_height()
        if h is None:
            continue
        try:
            v = float(h)
        except (TypeError, ValueError):
            continue
        ax.annotate(
            fmt.format(v),
            xy=(b.get_x() + b.get_width() / 2.0, v),
            xytext=(0, 3),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontsize=8,
        )


def bar_karsilastirma(
    labels: Sequence[str],
    values: Sequence[float],
    output_path: str,
    *,
    baslik: str,
    y_label: str,
    alt_baslik: str = "",
    sayi_formati: str = "{:.2f}",
    renkler: Optional[Sequence[str]] = None,
) -> str:
    """
    Tek metrik için senaryolar arası bar karşılaştırma grafiği.

    Args:
        labels: Senaryo isimleri (x ekseni)
        values: Her senaryo için değer
        output_path: PNG yolu
        baslik: Ana başlık
        y_label: Y ekseni etiketi
        alt_baslik: Suptitle
        sayi_formati: Bar üstü değer formatı
        renkler: Opsiyonel sabit renk listesi

    Returns:
        Kaydedilen PNG yolu
    """
    fig, ax = plt.subplots(figsize=(10, 6))
    etiketler = [_senaryo_ad_kisalt(s) for s in labels]
    colors = list(renkler) if renkler else [SENARYO_RENKLERI[i % len(SENARYO_RENKLERI)] for i in range(len(labels))]
    bars = ax.bar(etiketler, list(values), color=colors)
    ax.set_ylabel(y_label)
    ax.set_title(baslik)
    if alt_baslik:
        fig.suptitle(alt_baslik, fontsize=10, color="#555")
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    plt.setp(ax.get_xticklabels(), rotation=25, ha="right")
    _bar_degerleri_yaz(ax, bars, fmt=sayi_formati)
    return _save_fig(fig, output_path)


def grouped_bar_gruplar(
    grup_etiketleri: Sequence[str],
    seri_etiketleri: Sequence[str],
    deger_satirlari: Sequence[Sequence[float]],
    output_path: str,
    *,
    baslik: str,
    y_label: str,
    alt_baslik: str = "",
    sayi_formati: str = "{:.1f}",
    renkler: Optional[Sequence[str]] = None,
) -> str:
    """
    X ekseninde gruplar (ör. cf değeri); her grupta yan yana seriler (ör. serbest / kısıtlı senaryolar).

    Args:
        grup_etiketleri: Grup adları (x ekseni konumları).
        seri_etiketleri: Lejant seri adları.
        deger_satirlari: ``deger_satirlari[g][s]`` = g. grup, s. seri değeri; ``len == len(grup_etiketleri)``.
        output_path: PNG yolu.
        baslik: Ana başlık.
        y_label: Y ekseni etiketi.
        alt_baslik: Suptitle.
        sayi_formati: Sütun üstü etiket biçimi.
        renkler: Seri başına renk (opsiyonel).

    Returns:
        Kaydedilen PNG yolu.
    """
    n_g = len(grup_etiketleri)
    n_s = len(seri_etiketleri)
    if n_g == 0 or n_s == 0:
        raise ValueError("grup_etiketleri ve seri_etiketleri boş olamaz.")
    if len(deger_satirlari) != n_g:
        raise ValueError("deger_satirlari satır sayısı grup_etiketleri ile aynı olmalıdır.")
    for g, row in enumerate(deger_satirlari):
        if len(row) != n_s:
            raise ValueError(f"Grup {g}: her satırda {n_s} değer olmalıdır.")

    fig, ax = plt.subplots(figsize=(11, 6))
    grup_x = list(range(n_g))
    bar_w = min(0.85 / max(n_s, 1), 0.28)
    colors = list(renkler) if renkler else [SENARYO_RENKLERI[i % len(SENARYO_RENKLERI)] for i in range(n_s)]

    for s in range(n_s):
        offset = (s - (n_s - 1) / 2.0) * bar_w
        xs = [xg + offset for xg in grup_x]
        vals = [float(deger_satirlari[g][s]) for g in range(n_g)]
        bars = ax.bar(xs, vals, width=bar_w, label=seri_etiketleri[s], color=colors[s])
        for b in bars:
            h = b.get_height()
            if h is None:
                continue
            ax.annotate(
                sayi_formati.format(float(h)),
                xy=(b.get_x() + b.get_width() / 2.0, float(h)),
                xytext=(0, 2),
                textcoords="offset points",
                ha="center",
                va="bottom",
                fontsize=7,
            )

    ax.set_xticks(grup_x)
    ax.set_xticklabels([_senaryo_ad_kisalt(str(g), maks=14) for g in grup_etiketleri])
    ax.set_ylabel(y_label)
    ax.set_title(baslik)
    if alt_baslik:
        fig.suptitle(alt_baslik, fontsize=10, color="#555")
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    ax.legend(loc="upper left")
    return _save_fig(fig, output_path)


def stacked_bar_kirilim(
    labels: Sequence[str],
    bilesenler: Dict[str, Sequence[float]],
    output_path: str,
    *,
    baslik: str,
    y_label: str,
    alt_baslik: str = "",
    renkler: Optional[Dict[str, str]] = None,
) -> str:
    """
    Senaryolar için çok bileşenli stacked bar (ör. fire/stok/setup maliyet kırılımı veya
    kullanılan/stok/fire ton kırılımı).

    Args:
        labels: Senaryo isimleri
        bilesenler: Bileşen adı → değer listesi
        output_path: PNG yolu
        baslik: Ana başlık
        y_label: Y ekseni etiketi
        alt_baslik: Suptitle
        renkler: Bileşen → renk haritası (opsiyonel)

    Returns:
        PNG yolu
    """
    fig, ax = plt.subplots(figsize=(10, 6))
    etiketler = [_senaryo_ad_kisalt(s) for s in labels]
    n = len(labels)
    alt = [0.0] * n
    varsayilan_renk = {
        "fire": FIRE_RENGI,
        "stok": STOK_RENGI,
        "kullanilan": KULLANILAN_RENGI,
        "setup": SETUP_RENGI,
    }
    rlookup = dict(varsayilan_renk)
    if renkler:
        rlookup.update(renkler)
    for ad, vals in bilesenler.items():
        renk = rlookup.get(ad.lower(), None)
        vs = [float(v or 0.0) for v in vals]
        ax.bar(etiketler, vs, bottom=alt, label=ad, color=renk)
        alt = [a + v for a, v in zip(alt, vs)]
    ax.set_ylabel(y_label)
    ax.set_title(baslik)
    if alt_baslik:
        fig.suptitle(alt_baslik, fontsize=10, color="#555")
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    plt.setp(ax.get_xticklabels(), rotation=25, ha="right")
    ax.legend(loc="upper right")
    return _save_fig(fig, output_path)


def line_karsilastirma(
    x_values: Sequence[Any],
    serileri: Dict[str, Sequence[float]],
    output_path: str,
    *,
    baslik: str,
    x_label: str,
    y_label: str,
    alt_baslik: str = "",
    referans_x: Optional[Any] = None,
) -> str:
    """
    Çoklu çizgi grafiği. OFAT eksen değeri veya senaryo sırası boyunca metrik trendi.

    Args:
        x_values: X ekseni değerleri
        serileri: Seri adı → değer listesi (x_values ile aynı uzunlukta)
        output_path: PNG yolu
        baslik: Ana başlık
        x_label: X ekseni etiketi
        y_label: Y ekseni etiketi
        alt_baslik: Suptitle
        referans_x: Varsa dikey referans çizgisi konumu

    Returns:
        PNG yolu
    """
    fig, ax = plt.subplots(figsize=(10, 6))
    xs_str = [str(x) for x in x_values]
    for idx, (ad, vals) in enumerate(serileri.items()):
        color = SENARYO_RENKLERI[idx % len(SENARYO_RENKLERI)]
        ax.plot(xs_str, [float(v or 0.0) for v in vals], marker="o", label=ad, color=color)
    ax.set_xlabel(x_label)
    ax.set_ylabel(y_label)
    ax.set_title(baslik)
    if alt_baslik:
        fig.suptitle(alt_baslik, fontsize=10, color="#555")
    ax.grid(True, linestyle="--", alpha=0.4)
    if referans_x is not None:
        try:
            ref_lbl = str(referans_x)
            if ref_lbl in xs_str:
                ax.axvline(xs_str.index(ref_lbl), color=REFERANS_RENGI, linestyle="--", alpha=0.8, label="Referans")
        except (TypeError, ValueError):
            pass
    ax.legend(loc="best")
    plt.setp(ax.get_xticklabels(), rotation=20, ha="right")
    return _save_fig(fig, output_path)


def trend_line_normalize(
    labels: Sequence[str],
    metrikler: Dict[str, Sequence[float]],
    output_path: str,
    *,
    baslik: str,
    alt_baslik: str = "",
    baseline_idx: int = 0,
) -> str:
    """
    Senaryolar arası çok metrik normalize trendi (baseline = 100).

    Args:
        labels: Senaryo adları (sıralı)
        metrikler: Metrik adı → değer listesi
        output_path: PNG yolu
        baslik: Ana başlık
        alt_baslik: Suptitle
        baseline_idx: Normalizasyon referansı olan senaryo indeksi

    Returns:
        PNG yolu
    """
    fig, ax = plt.subplots(figsize=(10, 6))
    etiketler = [_senaryo_ad_kisalt(s) for s in labels]
    for idx, (ad, vals) in enumerate(metrikler.items()):
        vs = [float(v or 0.0) for v in vals]
        base = vs[baseline_idx] if 0 <= baseline_idx < len(vs) else 0.0
        if abs(base) < 1e-12:
            norm = vs
            etiket = f"{ad} (ham)"
        else:
            norm = [100.0 * v / base for v in vs]
            etiket = f"{ad} (baseline=100)"
        color = SENARYO_RENKLERI[idx % len(SENARYO_RENKLERI)]
        ax.plot(etiketler, norm, marker="o", label=etiket, color=color)
    ax.set_ylabel("Normalize değer")
    ax.set_title(baslik)
    if alt_baslik:
        fig.suptitle(alt_baslik, fontsize=10, color="#555")
    ax.grid(True, linestyle="--", alpha=0.4)
    ax.axhline(100, color=REFERANS_RENGI, linestyle=":", alpha=0.7)
    ax.legend(loc="best")
    plt.setp(ax.get_xticklabels(), rotation=20, ha="right")
    return _save_fig(fig, output_path)


def kesim_semasi(
    rolls: Sequence[Dict[str, Any]],
    output_path: str,
    *,
    baslik: str,
    alt_baslik: str = "",
) -> str:
    """
    Her rulo için ton ekseninde yatay stacked bar (sipariş parçaları renklendirilmiş,
    ardından stok ve fire).

    Args:
        rolls: Her rulo için sözlük: {rollId, totalTonnage, used, stock, fire,
               segments=[{orderId, tonnage, m2?}]}
        output_path: PNG yolu
        baslik: Başlık
        alt_baslik: Alt başlık

    Returns:
        PNG yolu
    """
    fig, ax = plt.subplots(figsize=(11, 1.0 + 0.7 * max(1, len(rolls))))
    y_pos = list(range(len(rolls)))
    y_labels = [f"Rulo {r.get('rollId', i + 1)} ({float(r.get('totalTonnage', 0) or 0):.1f} t)" for i, r in enumerate(rolls)]

    order_colors: Dict[int, str] = {}

    def _order_color(oid: int) -> str:
        """
        Sipariş id'sine stabil renk atar.

        Args:
            oid: Sipariş id

        Returns:
            Hex renk
        """
        if oid not in order_colors:
            order_colors[oid] = SENARYO_RENKLERI[oid % len(SENARYO_RENKLERI)]
        return order_colors[oid]

    for i, r in enumerate(rolls):
        sol = 0.0
        for seg in r.get("segments") or []:
            oid = int(seg.get("orderId", 0))
            ton = float(seg.get("tonnage", 0) or 0)
            if ton <= 0:
                continue
            ax.barh(i, ton, left=sol, color=_order_color(oid), edgecolor="white")
            if ton >= 0.3:
                ax.text(
                    sol + ton / 2.0,
                    i,
                    f"S{oid}\n{ton:.2f}t",
                    ha="center",
                    va="center",
                    fontsize=7,
                    color="white",
                )
            sol += ton
        stok = float(r.get("stock", 0) or 0)
        fire = float(r.get("fire", 0) or 0)
        if stok > 0:
            ax.barh(i, stok, left=sol, color=STOK_RENGI, alpha=0.45, edgecolor="white", hatch="//")
            ax.text(sol + stok / 2.0, i, f"Stok {stok:.2f}t", ha="center", va="center", fontsize=7, color="#333")
            sol += stok
        if fire > 0:
            ax.barh(i, fire, left=sol, color=FIRE_RENGI, alpha=0.55, edgecolor="white", hatch="xx")
            ax.text(sol + fire / 2.0, i, f"Fire {fire:.2f}t", ha="center", va="center", fontsize=7, color="#333")

    ax.set_yticks(y_pos)
    ax.set_yticklabels(y_labels)
    ax.invert_yaxis()
    ax.set_xlabel("Ton")
    ax.set_title(baslik)
    if alt_baslik:
        fig.suptitle(alt_baslik, fontsize=10, color="#555")
    ax.grid(axis="x", linestyle="--", alpha=0.4)

    from matplotlib.patches import Patch

    legend_items = [Patch(facecolor=c, label=f"Sipariş {oid}") for oid, c in sorted(order_colors.items())]
    legend_items.append(Patch(facecolor=STOK_RENGI, alpha=0.45, hatch="//", label="Stok"))
    legend_items.append(Patch(facecolor=FIRE_RENGI, alpha=0.55, hatch="xx", label="Fire"))
    ax.legend(handles=legend_items, loc="upper right", fontsize=8)

    return _save_fig(fig, output_path)


def uretim_adimlari_grafigi(
    line_schedule: Optional[List[Dict[str, Any]]],
    output_path: str,
    *,
    baslik: str = "Üretim Adımları (Eş Zamanlı Üst + Alt Yüzey)",
    alt_baslik: str = "",
) -> Optional[str]:
    """
    Her adımda üst ve alt rulonun eş zamanlı çalıştığı çift yüzey üretim çizelgesini görselleştirir.

    Görsel:
      - Y ekseni: Adım numarası (1'den başlar; yukarıdan aşağıya sıralı)
      - X ekseni tek bir panel genişliği; ama iki sütun var: soldaki "Üst yüzey",
        sağdaki "Alt yüzey"
      - Her hücre: rulo kutucuğu (renk = rulo id) + kesilen ton etiketi + sipariş no
      - Rulo renkleri tutarlı (aynı rulo her adımda aynı renk)
      - Aksiyon özeti her satırın sağ marjında metin olarak

    Args:
        line_schedule: solve_optimization çıktısındaki `lineSchedule` listesi (None ise None döner)
        output_path: PNG yolu
        baslik: Ana başlık
        alt_baslik: Suptitle

    Returns:
        PNG yolu veya None (veri yoksa)
    """
    if not line_schedule:
        return None
    steps = list(line_schedule)
    n = len(steps)
    fig_h = max(3.5, 0.6 * n + 1.5)
    fig, ax = plt.subplots(figsize=(11, fig_h))
    ax.set_xlim(0, 10)
    ax.set_ylim(0, n + 1)
    ax.invert_yaxis()
    ax.set_yticks([i + 1 for i in range(n)])
    ax.set_yticklabels([f"Adım {s.get('step', i + 1)}" for i, s in enumerate(steps)])
    ax.set_xticks([2.0, 6.0])
    ax.set_xticklabels(["Üst yüzey", "Alt yüzey"])
    ax.set_title(baslik)
    if alt_baslik:
        fig.suptitle(alt_baslik, fontsize=10, color="#555")
    ax.grid(axis="y", linestyle="--", alpha=0.3)

    roll_colors: Dict[int, str] = {}

    def _rcol(rid: Optional[int]) -> str:
        """
        Rulo id'sine stabil renk ataması.

        Args:
            rid: rulo id ya da None

        Returns:
            Hex renk
        """
        if rid is None:
            return "#DDDDDD"
        if rid not in roll_colors:
            roll_colors[rid] = SENARYO_RENKLERI[rid % len(SENARYO_RENKLERI)]
        return roll_colors[rid]

    from matplotlib.patches import FancyBboxPatch

    for i, s in enumerate(steps):
        y = i + 1
        oid = s.get("orderId")
        upper = s.get("upperRollId")
        lower = s.get("lowerRollId")
        upper_ton = 0.0
        lower_ton = 0.0
        for c in s.get("cuts") or []:
            rid = c.get("rollId")
            if rid is None:
                continue
            if upper is not None and int(rid) == int(upper):
                upper_ton += float(c.get("tonnage") or 0.0)
            if lower is not None and int(rid) == int(lower):
                lower_ton += float(c.get("tonnage") or 0.0)

        for cx, rid, ton, action in [
            (2.0, upper, upper_ton, s.get("upperAction", "")),
            (6.0, lower, lower_ton, s.get("lowerAction", "")),
        ]:
            box = FancyBboxPatch(
                (cx - 1.4, y - 0.38),
                2.8,
                0.76,
                boxstyle="round,pad=0.02,rounding_size=0.1",
                linewidth=1.2,
                edgecolor="#333" if action == "takildi" else "#888",
                facecolor=_rcol(rid),
                alpha=0.85 if rid is not None else 0.3,
            )
            ax.add_patch(box)
            if rid is not None:
                ax.text(cx, y - 0.1, f"Rulo {rid}", ha="center", va="center", fontsize=9, color="white", weight="bold")
                ax.text(
                    cx,
                    y + 0.18,
                    f"S{oid}: {ton:.2f} t",
                    ha="center",
                    va="center",
                    fontsize=8,
                    color="white",
                )
            else:
                ax.text(cx, y, "—", ha="center", va="center", fontsize=10, color="#666")

        aksiyon = s.get("actionSummary", "")
        if aksiyon:
            ax.text(10.2, y, aksiyon, ha="left", va="center", fontsize=7, color="#333")

    ax.set_xlim(0, 15.5)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    from matplotlib.patches import Patch

    legend_items = [Patch(facecolor=c, label=f"Rulo {rid}") for rid, c in sorted(roll_colors.items())]
    if legend_items:
        ax.legend(handles=legend_items, loc="lower right", fontsize=8, ncol=min(len(legend_items), 4))

    return _save_fig(fig, output_path)


def uretim_adimlari_grafigi_from_results(
    results: Optional[Dict[str, Any]],
    output_path: str,
    *,
    baslik: str = "Üretim Adımları",
    alt_baslik: str = "",
) -> Optional[str]:
    """
    `solve_optimization` çıktısından üretim adımları grafiği üretir.

    Args:
        results: Çözüm sözlüğü (lineSchedule içermeli) veya None
        output_path: PNG yolu
        baslik: Başlık
        alt_baslik: Suptitle

    Returns:
        PNG yolu veya None
    """
    if not results:
        return None
    return uretim_adimlari_grafigi(results.get("lineSchedule"), output_path, baslik=baslik, alt_baslik=alt_baslik)


def kesim_semasi_from_results(
    results: Optional[Dict[str, Any]],
    output_path: str,
    *,
    baslik: str,
    alt_baslik: str = "",
) -> Optional[str]:
    """
    solve_optimization çıktısından kesim şeması PNG üretir.

    Args:
        results: solve_optimization dönüşü veya None
        output_path: PNG yolu
        baslik: Başlık
        alt_baslik: Alt başlık

    Returns:
        PNG yolu veya None (results yoksa)
    """
    if not results or not results.get("rollStatus"):
        return None
    by_roll: Dict[int, List[Dict[str, Any]]] = {}
    for item in results.get("cuttingPlan") or []:
        rid = int(item.get("rollId", 0))
        by_roll.setdefault(rid, []).append(
            {
                "orderId": int(item.get("orderId", 0)),
                "tonnage": float(item.get("tonnage", 0) or 0),
                "m2": float(item.get("m2", 0) or 0),
            }
        )
    rolls: List[Dict[str, Any]] = []
    for rs in results.get("rollStatus") or []:
        rid = int(rs.get("rollId", 0))
        rolls.append(
            {
                "rollId": rid,
                "totalTonnage": float(rs.get("totalTonnage", 0) or 0),
                "used": float(rs.get("used", 0) or 0),
                "stock": float(rs.get("stock", 0) or 0),
                "fire": float(rs.get("fire", 0) or 0),
                "segments": by_roll.get(rid, []),
            }
        )
    if not rolls:
        return None
    return kesim_semasi(rolls, output_path, baslik=baslik, alt_baslik=alt_baslik)


# ---- Senaryo kümesi seviye üst-düzey yardımcılar ----

# Tez / main karşılaştırma için tek satır özet yapısı
# row beklenen alanlar: senaryo_adi, toplam_fire, toplam_stok, kullanilan_ton, acilan_rulo,
# toplam_maliyet, fire_maliyet, stok_maliyet, setup_maliyet, rulo_degisim_sayisi,
# uretim_hatti_rulo_gecis_sayisi, toplam_rulo_kapasitesi_ton


def _f(v: Any) -> float:
    """
    None/str güvenli float dönüşüm.

    Args:
        v: Herhangi bir değer

    Returns:
        float (başarısızsa 0.0)
    """
    try:
        return float(v) if v is not None and v != "" else 0.0
    except (TypeError, ValueError):
        return 0.0


def _i(v: Any) -> int:
    """
    None/str güvenli int dönüşüm.

    Args:
        v: Herhangi bir değer

    Returns:
        int (başarısızsa 0)
    """
    try:
        return int(float(v)) if v is not None and v != "" else 0
    except (TypeError, ValueError):
        return 0


def senaryo_seti_karsilastirma_grafikleri(
    rows: Sequence[Dict[str, Any]],
    output_dir: str,
    *,
    alt_baslik: str = "",
) -> List[str]:
    """
    Birden fazla senaryo (tez doğrulama / main batch) için standart 12 grafik setini üretir.

    Args:
        rows: Her senaryonun metrik sözlüğü (senaryo_adi, toplam_fire, toplam_stok,
              kullanilan_ton, acilan_rulo, toplam_maliyet, fire_maliyet, stok_maliyet,
              setup_maliyet, rulo_degisim_sayisi, uretim_hatti_rulo_gecis_sayisi,
              toplam_rulo_kapasitesi_ton)
        output_dir: PNG'lerin yazılacağı klasör
        alt_baslik: Tüm grafiklerin altına yazılacak suptitle

    Returns:
        Üretilen PNG yollarının listesi (var olanlar)
    """
    _ensure_dir(output_dir)
    out: List[str] = []
    if not rows:
        return out
    labels = [str(r.get("senaryo_adi", f"S{i+1}")) for i, r in enumerate(rows)]
    fire = [_f(r.get("toplam_fire")) for r in rows]
    stok = [_f(r.get("toplam_stok")) for r in rows]
    kullanilan = [_f(r.get("kullanilan_ton")) for r in rows]
    acilan = [_i(r.get("acilan_rulo")) for r in rows]
    maliyet = [_f(r.get("toplam_maliyet")) for r in rows]
    fire_mal = [_f(r.get("fire_maliyet")) for r in rows]
    stok_mal = [_f(r.get("stok_maliyet")) for r in rows]
    setup_mal = [_f(r.get("setup_maliyet")) for r in rows]
    rulo_deg = [_i(r.get("rulo_degisim_sayisi")) for r in rows]
    hat_gecis = [_i(r.get("uretim_hatti_rulo_gecis_sayisi")) for r in rows]
    kapasite = [_f(r.get("toplam_rulo_kapasitesi_ton")) for r in rows]

    fire_orani = [100.0 * f / k if k > 0 else 0.0 for f, k in zip(fire, kapasite)]
    kap_kullanim = [100.0 * u / k if k > 0 else 0.0 for u, k in zip(kullanilan, kapasite)]

    p = os.path.join
    out.append(bar_karsilastirma(labels, fire, p(output_dir, "fire_bar.png"),
                                 baslik="Toplam Fire (Ton)", y_label="Fire (Ton)", alt_baslik=alt_baslik, sayi_formati="{:.3f}"))
    out.append(bar_karsilastirma(labels, stok, p(output_dir, "stok_bar.png"),
                                 baslik="Toplam Stok (Ton)", y_label="Stok (Ton)", alt_baslik=alt_baslik, sayi_formati="{:.3f}"))
    out.append(bar_karsilastirma(labels, kullanilan, p(output_dir, "kullanilan_ton_bar.png"),
                                 baslik="Kullanılan Ton", y_label="Kullanılan (Ton)", alt_baslik=alt_baslik, sayi_formati="{:.3f}"))
    out.append(bar_karsilastirma(labels, acilan, p(output_dir, "acilan_rulo_bar.png"),
                                 baslik="Açılan Rulo Sayısı", y_label="Adet", alt_baslik=alt_baslik, sayi_formati="{:.0f}"))
    out.append(bar_karsilastirma(labels, maliyet, p(output_dir, "maliyet_toplam_bar.png"),
                                 baslik="Toplam Maliyet", y_label="Maliyet", alt_baslik=alt_baslik, sayi_formati="{:.2f}"))
    out.append(stacked_bar_kirilim(labels,
                                   {"fire": fire_mal, "stok": stok_mal, "setup": setup_mal},
                                   p(output_dir, "maliyet_kirilim_stacked.png"),
                                   baslik="Maliyet Kırılımı (Fire + Stok + Setup)", y_label="Maliyet", alt_baslik=alt_baslik))
    out.append(stacked_bar_kirilim(labels,
                                   {"kullanilan": kullanilan, "stok": stok, "fire": fire},
                                   p(output_dir, "ton_kirilim_stacked.png"),
                                   baslik="Tonaj Kırılımı (Kullanılan + Stok + Fire)", y_label="Ton", alt_baslik=alt_baslik))
    out.append(bar_karsilastirma(labels, rulo_deg, p(output_dir, "rulo_degisim_bar.png"),
                                 baslik="Kesim Planı Rulo Değişim Sayısı", y_label="Adet", alt_baslik=alt_baslik, sayi_formati="{:.0f}"))
    out.append(bar_karsilastirma(labels, hat_gecis, p(output_dir, "hat_gecis_bar.png"),
                                 baslik="Üretim Hattı Rulo Geçiş Sayısı", y_label="Adet", alt_baslik=alt_baslik, sayi_formati="{:.0f}"))
    out.append(bar_karsilastirma(labels, fire_orani, p(output_dir, "fire_orani_bar.png"),
                                 baslik="Fire Oranı (% kapasite)", y_label="%", alt_baslik=alt_baslik, sayi_formati="{:.2f}"))
    out.append(bar_karsilastirma(labels, kap_kullanim, p(output_dir, "kapasite_kullanim_bar.png"),
                                 baslik="Kapasite Kullanım Oranı", y_label="%", alt_baslik=alt_baslik, sayi_formati="{:.2f}"))

    trend_metrikler = {
        "Fire (ton)": fire,
        "Stok (ton)": stok,
        "Toplam Maliyet": maliyet,
        "Açılan Rulo": [float(x) for x in acilan],
    }
    out.append(trend_line_normalize(labels, trend_metrikler, p(output_dir, "metrik_trend_line.png"),
                                    baslik="Metrik Trendi (Baseline=100)", alt_baslik=alt_baslik))

    return [o for o in out if o and os.path.exists(o)]


def ofat_eksen_line_grafikleri(
    axis_adi: str,
    axis_values: Sequence[Any],
    rows: Sequence[Dict[str, Any]],
    output_dir: str,
    *,
    referans_axis_value: Any = 1.0,
    alt_baslik: str = "",
) -> List[str]:
    """
    Tek OFAT ekseninin (örn fireCost çarpanı) noktaları için 5 line grafiği üretir.

    Args:
        axis_adi: Eksen adı (fireCost, stockCost, ...)
        axis_values: X ekseni değerleri
        rows: Her nokta için sözlük (totalFire, totalStock, totalCost, openedRolls,
              rollChangeCount, toplam_rulo_kapasitesi_ton)
        output_dir: Çıktı klasörü
        referans_axis_value: Dikey referans çizgisi
        alt_baslik: Grafik suptitle

    Returns:
        Üretilen PNG yollarının listesi
    """
    _ensure_dir(output_dir)
    if not rows:
        return []
    fire = [_f(r.get("totalFire") or r.get("toplam_fire")) for r in rows]
    stok = [_f(r.get("totalStock") or r.get("toplam_stok")) for r in rows]
    maliyet = [_f(r.get("totalCost") or r.get("toplam_maliyet")) for r in rows]
    opened = [_f(r.get("openedRolls") or r.get("acilan_rulo")) for r in rows]
    rcc = [_f(r.get("rollChangeCount") or r.get("rulo_degisim_sayisi")) for r in rows]
    kapasite = [_f(r.get("toplam_rulo_kapasitesi_ton")) for r in rows]
    kullanim = [
        100.0 * _f(r.get("kullanilan_ton") or r.get("toplam_talep_ton")) / k if k > 0 else 0.0
        for r, k in zip(rows, kapasite)
    ]

    p = os.path.join
    out: List[str] = []
    out.append(line_karsilastirma(axis_values, {"Fire (ton)": fire},
                                  p(output_dir, f"ofat_{axis_adi}_fire_line.png"),
                                  baslik=f"OFAT {axis_adi} → Fire", x_label=axis_adi, y_label="Fire (Ton)",
                                  alt_baslik=alt_baslik, referans_x=referans_axis_value))
    out.append(line_karsilastirma(axis_values, {"Stok (ton)": stok},
                                  p(output_dir, f"ofat_{axis_adi}_stok_line.png"),
                                  baslik=f"OFAT {axis_adi} → Stok", x_label=axis_adi, y_label="Stok (Ton)",
                                  alt_baslik=alt_baslik, referans_x=referans_axis_value))
    out.append(line_karsilastirma(axis_values, {"Toplam Maliyet": maliyet},
                                  p(output_dir, f"ofat_{axis_adi}_maliyet_line.png"),
                                  baslik=f"OFAT {axis_adi} → Toplam Maliyet", x_label=axis_adi, y_label="Maliyet",
                                  alt_baslik=alt_baslik, referans_x=referans_axis_value))
    out.append(line_karsilastirma(axis_values, {"Açılan Rulo": opened, "Rulo Değişim": rcc},
                                  p(output_dir, f"ofat_{axis_adi}_rulo_line.png"),
                                  baslik=f"OFAT {axis_adi} → Rulo Metrikleri", x_label=axis_adi, y_label="Adet",
                                  alt_baslik=alt_baslik, referans_x=referans_axis_value))
    out.append(line_karsilastirma(axis_values, {"Kapasite Kullanımı (%)": kullanim},
                                  p(output_dir, f"ofat_{axis_adi}_kapasite_line.png"),
                                  baslik=f"OFAT {axis_adi} → Kapasite Kullanımı", x_label=axis_adi, y_label="%",
                                  alt_baslik=alt_baslik, referans_x=referans_axis_value))
    return [o for o in out if o and os.path.exists(o)]


def ofat_eksenler_normalize(
    eksen_adlari: Sequence[str],
    eksen_row_gruplari: Sequence[Sequence[Dict[str, Any]]],
    output_dir: str,
) -> List[str]:
    """
    Tüm OFAT eksenlerinin fire ve maliyet değerlerini birlikte normalize eden karşılaştırma.

    Args:
        eksen_adlari: Eksen adları
        eksen_row_gruplari: Her eksen için nokta listeleri
        output_dir: Çıktı klasörü

    Returns:
        İki PNG yolu (fire ve maliyet)
    """
    _ensure_dir(output_dir)
    out: List[str] = []
    if not eksen_adlari:
        return out
    fire_series: Dict[str, List[float]] = {}
    maliyet_series: Dict[str, List[float]] = {}
    max_len = 0
    for ad, rows in zip(eksen_adlari, eksen_row_gruplari):
        fs = [_f(r.get("totalFire") or r.get("toplam_fire")) for r in rows]
        ms = [_f(r.get("totalCost") or r.get("toplam_maliyet")) for r in rows]
        fire_series[ad] = fs
        maliyet_series[ad] = ms
        max_len = max(max_len, len(fs))
    xs = list(range(1, max_len + 1))

    def _pad(vals: List[float]) -> List[float]:
        """
        Seri eksenleri eşit uzunluğa pad'ler.

        Args:
            vals: Değer listesi

        Returns:
            Pad'lenmiş liste
        """
        if len(vals) >= max_len:
            return vals
        return vals + [float("nan")] * (max_len - len(vals))

    for ad, vals in fire_series.items():
        fire_series[ad] = _pad(vals)
    for ad, vals in maliyet_series.items():
        maliyet_series[ad] = _pad(vals)

    out.append(line_karsilastirma(xs, fire_series, os.path.join(output_dir, "eksenler_fire_normalize.png"),
                                  baslik="OFAT Eksenleri — Fire Trendi", x_label="Nokta sırası", y_label="Fire (Ton)"))
    out.append(line_karsilastirma(xs, maliyet_series, os.path.join(output_dir, "eksenler_maliyet_normalize.png"),
                                  baslik="OFAT Eksenleri — Toplam Maliyet Trendi", x_label="Nokta sırası", y_label="Maliyet"))
    return [o for o in out if o and os.path.exists(o)]


def pass_fail_pie(
    passed: int,
    failed: int,
    errors: int,
    output_path: str,
    *,
    baslik: str = "Birim Test Sonuçları",
) -> str:
    """
    Birim test özetine pass/fail pie grafiği.

    Args:
        passed: Geçen test sayısı
        failed: Kalan test sayısı
        errors: Hata atan test sayısı
        output_path: PNG yolu
        baslik: Grafik başlığı

    Returns:
        PNG yolu
    """
    fig, ax = plt.subplots(figsize=(7, 6))
    values = [max(0, passed), max(0, failed), max(0, errors)]
    labels = ["PASS", "FAIL", "ERROR"]
    colors = ["#54A24B", "#E45756", "#EECA3B"]
    nonzero = [(v, l, c) for v, l, c in zip(values, labels, colors) if v > 0]
    if not nonzero:
        ax.text(0.5, 0.5, "Veri yok", ha="center", va="center")
    else:
        vals = [x[0] for x in nonzero]
        lbls = [x[1] for x in nonzero]
        cols = [x[2] for x in nonzero]
        ax.pie(vals, labels=lbls, colors=cols, autopct="%1.0f%%", startangle=90)
        ax.axis("equal")
    ax.set_title(baslik)
    return _save_fig(fig, output_path)


def test_suresi_bar(
    test_adlari: Sequence[str],
    sureler_sn: Sequence[float],
    output_path: str,
) -> str:
    """
    Birim test süreleri için yatay bar grafiği.

    Args:
        test_adlari: Test fonksiyon / sınıf adları
        sureler_sn: Saniye cinsinden süreler
        output_path: PNG yolu

    Returns:
        PNG yolu
    """
    fig, ax = plt.subplots(figsize=(10, max(4, 0.35 * len(test_adlari))))
    y = list(range(len(test_adlari)))
    bars = ax.barh(y, list(sureler_sn), color=SENARYO_RENKLERI[0])
    ax.set_yticks(y)
    ax.set_yticklabels([_senaryo_ad_kisalt(t, 40) for t in test_adlari])
    ax.invert_yaxis()
    ax.set_xlabel("Süre (sn)")
    ax.set_title("Birim Test Süreleri")
    ax.grid(axis="x", linestyle="--", alpha=0.4)
    for b in bars:
        w = b.get_width()
        ax.annotate(f"{float(w):.2f}s", xy=(w, b.get_y() + b.get_height() / 2.0),
                    xytext=(3, 0), textcoords="offset points", ha="left", va="center", fontsize=8)
    return _save_fig(fig, output_path)

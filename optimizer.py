"""
Optimizasyon mantığı - Modüler fonksiyonlar
"""
import pulp
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill
from datetime import datetime
import os
import random
from typing import Dict, List, Tuple, Optional
from pydantic import BaseModel


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
        m2_eff = panel_count * pw * pl
        demand[j] = round(m2_eff * (thickness / 1000) * density, 4)
    
    total_tonnage = sum(demand.values())
    return demand, total_tonnage


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
) -> Tuple[str, Optional[Dict]]:
    """
    Optimizasyon modelini çöz. Panel uzunluğu ile kesim: uzunluk ve katları (örn. 3m → 3*33+1 fire).
    
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
        talep_m2[j] = panel_count * pw * pl
    D = {j: round(talep_m2[j] * (thickness / 1000) * density, 4) for j in J}
    
    # Model oluştur
    model = pulp.LpProblem("Kesme_Stoku_Optimizasyonu", pulp.LpMinimize)
    
    # Değişkenler (main.py ile aynı - n değişkeni kaldırıldı, daha hızlı)
    x = pulp.LpVariable.dicts("x", [(i, j) for i in I for j in J], lowBound=0, cat='Continuous')
    u = pulp.LpVariable.dicts("u", I, lowBound=0, cat='Continuous')
    R = pulp.LpVariable.dicts("R", I, lowBound=0, cat='Continuous')
    F = pulp.LpVariable.dicts("F", I, lowBound=0, cat='Continuous')
    b = pulp.LpVariable.dicts("b", I, cat='Binary')
    y = pulp.LpVariable.dicts("y", I, cat='Binary')
    w = pulp.LpVariable.dicts("w", [(i, j) for i in I for j in J], cat='Binary')
    
    # Amaç fonksiyonu
    model += (pulp.lpSum([F[i] * fire_cost for i in I]) +
              pulp.lpSum([R[i] * stock_cost for i in I]) +
              pulp.lpSum([y[i] * setup_cost for i in I]), "Toplam_Maliyet")
    
    # Talep kısıtı
    for j in J:
        model += pulp.lpSum([x[(i, j)] for i in I]) == D[j], f"Talep_{j}"
    
    # Kapasite kısıtı
    for i in I:
        model += u[i] <= S[i], f"Kapasite_{i}"
    
    # Kullanım tanımı
    for i in I:
        model += u[i] == pulp.lpSum([x[(i, j)] for j in J]), f"Kullanim_{i}"
    
    # Rulo kullanım tetikleyici
    for i in I:
        model += u[i] <= S[i] * y[i], f"Rulo_Kullanim_{i}"
    
    # Setup mantığı (Big-M): x_ij <= M * w_ij
    for i in I:
        for j in J:
            model += x[(i, j)] <= S[i] * w[(i, j)], f"Setup_{i}_{j}"
    
    # Minimum lot size kısıtı: x_ij >= min_lot * w_ij
    # Sipariş bazlı min lot - en küçük talebin yarısı veya 0.05 ton
    min_demand = min(D.values()) if D else 0.5
    min_lot = max(0.01, min(0.5, min_demand / 2))
    for i in I:
        for j in J:
            model += x[(i, j)] >= min_lot * w[(i, j)], f"Min_Lot_Size_{i}_{j}"
    
    # w_ij ve y_i bağlantısı: w_ij <= y_i
    for i in I:
        for j in J:
            model += w[(i, j)] <= y[i], f"W_Y_Link_{i}_{j}"
    
    # Rulo başına maksimum sipariş kısıtı (y_i ile çarpım - main.py'deki gibi)
    for i in I:
        model += pulp.lpSum([w[(i, j)] for j in J]) <= max_orders_per_roll * y[i], f"Max_Siparis_Per_Rulo_{i}"
    
    # Sipariş başına maksimum rulo kısıtı
    for j in J:
        model += pulp.lpSum([w[(i, j)] for i in I]) <= max_rolls_per_order, f"Max_Rulo_Per_Siparis_{j}"
    
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
    for i in I:
        for j in J:
            if pulp.value(x[(i, j)]) > 0.0001:
                miktar_ton = pulp.value(x[(i, j)])
                miktar_m2 = miktar_ton / ((thickness / 1000) * density)
                panel_count = int(round(miktar_ton / birim_tonaj[j])) if birim_tonaj[j] > 0 else 0
                pl = panel_lengths[j] if j < len(panel_lengths) else 1.0
                cutting_plan.append({
                    "rollId": i + 1,
                    "orderId": j + 1,
                    "panelCount": panel_count,
                    "panelWidth": panel_widths[j],
                    "panelLength": pl,
                    "tonnage": round(miktar_ton, 4),
                    "m2": round(miktar_m2, 2)
                })
    
    roll_status = []
    for i in I:
        kullanilan = pulp.value(u[i])
        fire = pulp.value(F[i])
        stok = pulp.value(R[i])
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
    
    # Özet
    toplam_fire = sum([pulp.value(F[i]) for i in I])
    toplam_stok = sum([pulp.value(R[i]) for i in I])
    acilan_rulo = sum([1 for i in I if pulp.value(y[i]) > 0.5])
    
    summary = {
        "totalCost": round(pulp.value(model.objective), 2),
        "totalFire": round(toplam_fire, 4),
        "totalStock": round(toplam_stok, 4),
        "openedRolls": acilan_rulo
    }
    
    results = {
        "status": status,
        "objective": round(pulp.value(model.objective), 2),
        "summary": summary,
        "cuttingPlan": cutting_plan,
        "rollStatus": roll_status,
        "model_vars": {
            "x": x,
            "u": u,
            "R": R,
            "F": F,
            "y": y,
            "w": w
        },
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
    model_vars = results['model_vars']
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
    
    x = model_vars['x']
    u = model_vars['u']
    R = model_vars['R']
    F = model_vars['F']
    y = model_vars['y']
    w = model_vars['w']
    
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
    
    ozet_data = [
        ["Metrik", "Değer"],
        ["Toplam Maliyet", f"{results['summary']['totalCost']:.2f}"],
        ["Toplam Fire (Ton)", f"{results['summary']['totalFire']:.4f}"],
        ["Toplam Stok (Ton)", f"{results['summary']['totalStock']:.4f}"],
        ["Açılan Rulo Sayısı", f"{results['summary']['openedRolls']}"],
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


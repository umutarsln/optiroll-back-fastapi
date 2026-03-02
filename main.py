"""
FastAPI Backend - Optimizasyon API
"""
from dotenv import load_dotenv
load_dotenv()

import logging
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List, Dict, Optional
import uuid
import os
from optimizer import (
    generate_rolls,
    calculate_demand,
    solve_optimization,
    create_excel_report,
)
from supabase_client import (
    save_optimization_result,
    save_configuration,
    get_configuration_by_id,
    update_run_configuration_id,
    delete_run_by_file_id,
    delete_report_from_storage,
    list_order_sets,
    save_order_set,
    delete_order_set,
    list_stock_sets,
    save_stock_set,
    delete_stock_set,
    upload_report_to_storage,
    update_report_url,
    list_runs,
    get_run_by_file_id,
)

app = FastAPI(title="Kesme Stoku Optimizasyon API")

# CORS ayarları: lokal ve production frontend origin'lerini izinli yap
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "https://www.optiroll.pro",
        "https://optiroll.pro",
        "https://3d-web-iduh-4os6uvukk-umuts-projects-ef16418a.vercel.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class MaterialInput(BaseModel):
    thickness: float  # mm
    density: float  # g/cm³


class OrderInput(BaseModel):
    orderId: Optional[str] = None
    m2: float
    panelWidth: float  # metre
    panelLength: Optional[float] = 1.0  # panel kesim uzunluğu (metre); bu uzunluk ve katları kesilir


class RollSettingsInput(BaseModel):
    """Rulo ayarları: manuel liste veya otomatik bölme"""
    rolls: Optional[List[float]] = None  # Manuel rulo tonajları
    totalTonnage: Optional[float] = None  # Otomatik modda toplam tonaj
    minRollTon: int = 4
    maxRollTon: int = 10
    maxOrdersPerRoll: int
    maxRollsPerOrder: int = 999


class CostsInput(BaseModel):
    fireCost: float
    setupCost: float
    stockCost: float


class OptimizeRequest(BaseModel):
    material: MaterialInput
    orders: List[OrderInput]
    rollSettings: RollSettingsInput
    costs: CostsInput
    safetyStock: Optional[float] = 0
    configurationId: Optional[str] = None


class ConfigurationSaveRequest(BaseModel):
    """
    Konfigürasyon kayıt/güncelleme isteği.
    """
    configurationId: Optional[str] = None
    name: Optional[str] = None
    material: MaterialInput
    safetyStock: float = 0
    orders: List[OrderInput]
    rollSettings: RollSettingsInput
    costs: CostsInput


class OrderSetRequest(BaseModel):
    """
    Sipariş seti oluşturma/güncelleme isteği.
    """
    id: Optional[str] = None
    name: str
    orders: List[OrderInput]


class StockSetRequest(BaseModel):
    """
    Stok/rulo seti oluşturma/güncelleme isteği.
    """
    id: Optional[str] = None
    name: str
    rolls: List[float]


class SummaryResponse(BaseModel):
    totalCost: float
    totalFire: float
    totalStock: float
    openedRolls: int


class CuttingPlanItem(BaseModel):
    rollId: int
    orderId: int
    panelCount: int
    panelWidth: float
    panelLength: Optional[float] = 1.0
    tonnage: float
    m2: float


class RollStatusItem(BaseModel):
    rollId: int
    totalTonnage: float
    used: float
    remaining: float
    fire: float
    stock: float
    ordersUsed: int


class OptimizeResponse(BaseModel):
    status: str
    objective: float
    summary: SummaryResponse
    cuttingPlan: List[CuttingPlanItem]
    rollStatus: List[RollStatusItem]
    fileId: str


@app.get("/")
async def root():
    """API root endpoint"""
    return {"message": "Kesme Stoku Optimizasyon API", "version": "1.0.0"}


@app.post("/api/optimize", response_model=OptimizeResponse)
async def optimize(request: OptimizeRequest):
    """
    Optimizasyon çalıştır
    
    Request body:
    - material: {thickness, density}
    - orders: [{m2, panelWidth}, ...]
    - rollSettings: {totalTonnage, maxOrdersPerRoll}
    - costs: {fireCost, setupCost, stockCost}
    """
    try:
        # Input validation
        if request.material.thickness <= 0:
            logger.warning("[400] Kalınlık 0'dan büyük olmalıdır (thickness=%s)", request.material.thickness)
            raise HTTPException(status_code=400, detail="Kalınlık 0'dan büyük olmalıdır")
        
        if request.material.density <= 0:
            logger.warning("[400] Yoğunluk 0'dan büyük olmalıdır (density=%s)", request.material.density)
            raise HTTPException(status_code=400, detail="Yoğunluk 0'dan büyük olmalıdır")
        
        if len(request.orders) == 0:
            logger.warning("[400] En az bir sipariş gerekli")
            raise HTTPException(status_code=400, detail="En az bir sipariş gerekli")
        
        rs = request.rollSettings
        if rs.rolls and len(rs.rolls) > 0:
            rolls = [int(round(r)) for r in rs.rolls if r > 0]
            if len(rolls) == 0:
                logger.warning("[400] Manuel rulo listesi boş veya geçersiz")
                raise HTTPException(status_code=400, detail="En az bir geçerli rulo tonajı girin")
            total_roll_tonnage = sum(rolls)
        else:
            if not rs.totalTonnage or rs.totalTonnage <= 0:
                logger.warning("[400] Toplam rulo tonajı 0'dan büyük olmalı (totalTonnage=%s)", rs.totalTonnage)
                raise HTTPException(status_code=400, detail="Toplam rulo tonajı 0'dan büyük olmalıdır")
            total_roll_tonnage = int(rs.totalTonnage)
            rolls = generate_rolls(total_roll_tonnage, rs.minRollTon, rs.maxRollTon)
        
        if request.rollSettings.maxOrdersPerRoll < 1:
            logger.warning("[400] Maksimum sipariş/rulo en az 1 (maxOrdersPerRoll=%s)", request.rollSettings.maxOrdersPerRoll)
            raise HTTPException(status_code=400, detail="Maksimum sipariş/rulo en az 1 olmalıdır")
        
        # Panel genişlik ve uzunluklarını kontrol et
        panel_widths = [order.panelWidth for order in request.orders]
        panel_lengths = [float(order.panelLength or 1.0) for order in request.orders]
        for j, order in enumerate(request.orders):
            if order.panelWidth <= 0:
                detail = f"Sipariş {j+1} için panel genişliği 0'dan büyük olmalıdır"
                logger.warning("[400] %s (panelWidth=%s)", detail, order.panelWidth)
                raise HTTPException(status_code=400, detail=detail)
            if panel_lengths[j] <= 0:
                detail = f"Sipariş {j+1} için panel uzunluğu 0'dan büyük olmalıdır"
                raise HTTPException(status_code=400, detail=detail)
        
        # Talep hesaplama (panel uzunluğu ile: m² / (genişlik * uzunluk) = tam sayı panel)
        orders_list = [{"m2": o.m2, "panelWidth": o.panelWidth, "panelLength": panel_lengths[i]} for i, o in enumerate(request.orders)]
        D, total_tonnage_needed = calculate_demand(
            orders_list,
            request.material.thickness,
            request.material.density,
            panel_widths=panel_widths,
            panel_lengths=panel_lengths,
        )
        
        # Rulo tonajı kontrolü (küçük yuvarlama farklarına tolerans)
        if total_roll_tonnage < total_tonnage_needed - 0.01:
            detail = f"Toplam rulo tonajı ({total_roll_tonnage:.2f}) ihtiyaçtan ({total_tonnage_needed:.2f}) az olamaz"
            logger.warning("[400] %s", detail)
            raise HTTPException(status_code=400, detail=detail)
        
        # Optimizasyonu çöz (2 dk timeout); panel uzunluğu ile kesim: uzunluk ve katları
        status, results = solve_optimization(
            thickness=request.material.thickness,
            density=request.material.density,
            orders=orders_list,
            panel_widths=panel_widths,
            panel_lengths=panel_lengths,
            rolls=rolls,
            max_orders_per_roll=request.rollSettings.maxOrdersPerRoll,
            max_rolls_per_order=request.rollSettings.maxRollsPerOrder,
            fire_cost=request.costs.fireCost,
            setup_cost=request.costs.setupCost,
            stock_cost=request.costs.stockCost,
            time_limit_seconds=120,
        )
        
        if status != 'Optimal' or results is None:
            detail_msg = f"Optimizasyon çözülemedi. Durum: {status}"
            if status == 'Infeasible':
                detail_msg += ". Parametreleri kontrol edin: tonaj yeterli mi, max sipariş/rulo ve max rulo/sipariş kısıtları uyumlu mu?"
            logger.warning(
                "[400] Optimizasyon hatası: status=%s | tonaj=%s, rulo_sayisi=%s, ihtiyaç=%s, maxOrdersPerRoll=%s, maxRollsPerOrder=%s",
                status, total_roll_tonnage, len(rolls), total_tonnage_needed,
                request.rollSettings.maxOrdersPerRoll, request.rollSettings.maxRollsPerOrder
            )
            raise HTTPException(status_code=400, detail=detail_msg)
        
        # Excel rapor dosyasını oluştur
        file_id = uuid.uuid4().hex[:16]
        excel_path = create_excel_report(results, file_id)

        # Supabase'e kaydet (kesim sonuçları ve rapor)
        input_data = {
            "material": request.material.model_dump(),
            "safetyStock": request.safetyStock,
            "configurationId": request.configurationId,
            "orders": [o.model_dump() for o in request.orders],
            "rollSettings": request.rollSettings.model_dump(),
            "costs": request.costs.model_dump(),
        }
        save_optimization_result(
            file_id=file_id,
            input_data=input_data,
            summary=results["summary"],
            cutting_plan=results["cuttingPlan"],
            roll_status=results["rollStatus"],
            configuration_id=request.configurationId,
        )
        report_url = upload_report_to_storage(excel_path, file_id)
        if report_url:
            update_report_url(file_id, report_url)

        # Response oluştur
        response = OptimizeResponse(
            status=results['status'],
            objective=results['objective'],
            summary=SummaryResponse(**results['summary']),
            cuttingPlan=[CuttingPlanItem(**item) for item in results['cuttingPlan']],
            rollStatus=[RollStatusItem(**item) for item in results['rollStatus']],
            fileId=file_id
        )
        
        return response
        
    except HTTPException as he:
        if he.status_code >= 400:
            logger.warning("[%s] %s", he.status_code, he.detail)
        raise
    except Exception as e:
        logger.exception(
            "Optimize sunucu hatası: %s | orders=%s, rolls=%s",
            str(e), len(request.orders), len(request.rollSettings.rolls or []),
        )
        raise HTTPException(status_code=500, detail=f"Sunucu hatası: {str(e)}")


@app.post("/api/configurations")
async def save_configuration_endpoint(request: ConfigurationSaveRequest):
    """
    Konfigürasyonu kaydeder veya mevcut bir konfigürasyonu günceller.
    """
    try:
        if request.material.thickness <= 0:
            raise HTTPException(status_code=400, detail="Kalınlık 0'dan büyük olmalıdır")
        if request.material.density <= 0:
            raise HTTPException(status_code=400, detail="Yoğunluk 0'dan büyük olmalıdır")
        if len(request.orders) == 0:
            raise HTTPException(status_code=400, detail="En az bir sipariş gerekli")
        if request.rollSettings.maxOrdersPerRoll < 1:
            raise HTTPException(status_code=400, detail="Maksimum sipariş/rulo en az 1 olmalıdır")
        if request.rollSettings.maxRollsPerOrder < 1:
            raise HTTPException(status_code=400, detail="Maksimum rulo/sipariş en az 1 olmalıdır")

        row = save_configuration(
            config_id=request.configurationId,
            name=request.name,
            material=request.material.model_dump(),
            safety_stock=request.safetyStock,
            roll_settings=request.rollSettings.model_dump(),
            costs=request.costs.model_dump(),
            orders=[o.model_dump() for o in request.orders],
        )
        if not row:
            raise HTTPException(status_code=500, detail="Konfigürasyon kaydedilemedi")

        return {
            "configurationId": row.get("id"),
            "createdAt": row.get("created_at"),
            "updatedAt": row.get("updated_at"),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Konfigürasyon kayıt endpoint hatası: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Sunucu hatası: {str(e)}")


@app.post("/api/runs/{file_id}/save-configuration")
async def save_run_configuration_endpoint(file_id: str):
    """
    Sonuç çalıştırmasının input_data alanından konfigürasyon kaydı üretir/günceller.
    """
    try:
        run = get_run_by_file_id(file_id)
        if not run:
            raise HTTPException(status_code=404, detail="Çalıştırma bulunamadı veya Supabase kaydı yok")

        input_data = run.get("input_data") or {}
        material = input_data.get("material") or {}
        orders = input_data.get("orders") or []
        roll_settings = input_data.get("rollSettings") or {}
        costs = input_data.get("costs") or {}
        safety_stock = float(input_data.get("safetyStock", 0) or 0)

        if not material or not orders or not roll_settings or not costs:
            raise HTTPException(status_code=400, detail="Çalıştırma girdileri eksik, konfigürasyon üretilemedi")

        row = save_configuration(
            config_id=run.get("configuration_id"),
            name=f"Run {file_id}",
            material=material,
            safety_stock=safety_stock,
            roll_settings=roll_settings,
            costs=costs,
            orders=orders,
        )
        if not row:
            raise HTTPException(status_code=500, detail="Konfigürasyon kaydedilemedi")

        configuration_id = row.get("id")
        if configuration_id:
            update_run_configuration_id(file_id, configuration_id)

        return {
            "configurationId": configuration_id,
            "createdAt": row.get("created_at"),
            "updatedAt": row.get("updated_at"),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Run konfigürasyon kaydı hatası: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Sunucu hatası: {str(e)}")


@app.get("/api/configurations/{configuration_id}")
async def get_configuration_endpoint(configuration_id: str):
    """
    Konfigürasyon detayını döner.
    """
    row = get_configuration_by_id(configuration_id)
    if not row:
        raise HTTPException(status_code=404, detail="Konfigürasyon bulunamadı")
    return row


@app.get("/api/order-sets")
async def get_order_sets():
    """
    Kayıtlı sipariş setlerini listeler.
    """
    return {"orderSets": list_order_sets()}


@app.post("/api/order-sets")
async def upsert_order_set(request: OrderSetRequest):
    """
    Sipariş seti kaydeder veya günceller.
    """
    if not request.name.strip():
        raise HTTPException(status_code=400, detail="Set adı zorunludur")
    if len(request.orders) == 0:
        raise HTTPException(status_code=400, detail="En az bir sipariş gerekli")
    row = save_order_set(
        name=request.name.strip(),
        orders=[o.model_dump() for o in request.orders],
        set_id=request.id,
    )
    if not row:
        raise HTTPException(status_code=500, detail="Sipariş seti kaydedilemedi")
    return row


@app.delete("/api/order-sets/{set_id}")
async def remove_order_set(set_id: str):
    """
    Sipariş setini siler.
    """
    ok = delete_order_set(set_id)
    if not ok:
        raise HTTPException(status_code=500, detail="Sipariş seti silinemedi")
    return {"ok": True}


@app.get("/api/stock-sets")
async def get_stock_sets():
    """
    Kayıtlı stok/rulo setlerini listeler.
    """
    return {"stockSets": list_stock_sets()}


@app.post("/api/stock-sets")
async def upsert_stock_set(request: StockSetRequest):
    """
    Stok/rulo seti kaydeder veya günceller.
    """
    if not request.name.strip():
        raise HTTPException(status_code=400, detail="Set adı zorunludur")
    if len(request.rolls) == 0:
        raise HTTPException(status_code=400, detail="En az bir rulo gerekli")
    clean_rolls = [float(r) for r in request.rolls if float(r) > 0]
    if len(clean_rolls) == 0:
        raise HTTPException(status_code=400, detail="Geçerli rulo tonajı girin")
    row = save_stock_set(name=request.name.strip(), rolls=clean_rolls, set_id=request.id)
    if not row:
        raise HTTPException(status_code=500, detail="Stok seti kaydedilemedi")
    return row


@app.delete("/api/stock-sets/{set_id}")
async def remove_stock_set(set_id: str):
    """
    Stok/rulo setini siler.
    """
    ok = delete_stock_set(set_id)
    if not ok:
        raise HTTPException(status_code=500, detail="Stok seti silinemedi")
    return {"ok": True}


@app.get("/api/runs")
async def get_runs(limit: int = 50, offset: int = 0):
    """
    Geçmiş optimizasyon çalıştırmalarını yalnızca Supabase'den listeler.
    """
    runs = list_runs(limit=limit, offset=offset)
    return {"runs": runs}


@app.get("/api/runs/{file_id}")
async def get_run_detail(file_id: str):
    """
    Belirli bir optimizasyon çalıştırmasının detayını yalnızca Supabase'den döner.
    Frontend OptimizeResponse formatındadır.
    """
    run = get_run_by_file_id(file_id)
    if not run:
        raise HTTPException(status_code=404, detail="Çalıştırma bulunamadı")
    summary = run.get("summary") or {}
    return {
        "fileId": run.get("file_id", file_id),
        "status": run.get("status", "Optimal"),
        "objective": summary.get("totalCost", 0),
        "summary": summary,
        "cuttingPlan": run.get("cutting_plan") or [],
        "rollStatus": run.get("roll_status") or [],
        "configurationId": run.get("configuration_id"),
        "inputData": run.get("input_data"),
        "createdAt": run.get("created_at"),
        "reportUrl": run.get("report_url"),
    }


@app.delete("/api/runs/{file_id}")
async def delete_run(file_id: str):
    """
    Belirli bir optimizasyon çalıştırmasını siler.

    Args:
        file_id: Çalıştırma ID değeri
    """
    run = get_run_by_file_id(file_id)
    if not run:
        raise HTTPException(status_code=404, detail="Çalıştırma bulunamadı")

    storage_deleted = delete_report_from_storage(file_id)
    db_deleted = delete_run_by_file_id(file_id)
    if not db_deleted:
        raise HTTPException(status_code=500, detail="Çalıştırma silinemedi")

    # Yerelde varsa rapor dosyasını da sil (opsiyonel)
    local_excel = os.path.join("sonuclar", f"cozum_raporu_{file_id}.xlsx")
    if os.path.exists(local_excel):
        try:
            os.remove(local_excel)
        except Exception as remove_err:
            logger.warning("Yerel rapor silinemedi: %s", str(remove_err))

    return {
        "ok": True,
        "fileId": file_id,
        "storageDeleted": storage_deleted,
    }


@app.get("/api/results/{file_id}")
async def get_results(file_id: str):
    """
    Excel dosyasını indir
    
    Args:
        file_id: Dosya ID
    """
    file_path = os.path.join("sonuclar", f"cozum_raporu_{file_id}.xlsx")
    
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Dosya bulunamadı")
    
    return FileResponse(
        file_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"cozum_raporu_{file_id}.xlsx"
    )


@app.post("/api/validate")
async def validate_input(request: OptimizeRequest):
    """
    Input validasyonu
    
    Returns:
        Validation sonuçları
    """
    errors = []
    warnings = []
    
    # Material validation
    if request.material.thickness <= 0:
        errors.append("Kalınlık 0'dan büyük olmalıdır")
    
    if request.material.density <= 0:
        errors.append("Yoğunluk 0'dan büyük olmalıdır")
    
    # Orders validation
    if len(request.orders) == 0:
        errors.append("En az bir sipariş gerekli")
    
    for j, order in enumerate(request.orders):
        if order.m2 <= 0:
            errors.append(f"Sipariş {j+1}: m² değeri 0'dan büyük olmalıdır")
        
        if order.panelWidth <= 0:
            errors.append(f"Sipariş {j+1}: Panel genişliği 0'dan büyük olmalıdır")
        pl = float(order.panelLength or 1.0)
        if pl <= 0:
            errors.append(f"Sipariş {j+1}: Panel uzunluğu 0'dan büyük olmalıdır")
        
        # Panel sayısı kontrolü: m² / (genişlik * uzunluk) = tam sayı panel
        panel_count = order.m2 / (order.panelWidth * pl)
        if abs(panel_count - round(panel_count)) > 0.001:
            warnings.append(
                f"Sipariş {j+1}: Panel sayısı tam sayı değil ({panel_count:.2f} panel)"
            )
    
    # Roll settings validation
    rs = request.rollSettings
    total_roll = sum(rs.rolls) if rs.rolls and len(rs.rolls) > 0 else (rs.totalTonnage or 0)
    if total_roll <= 0:
        errors.append("Toplam rulo tonajı 0'dan büyük olmalıdır (manuel rulo listesi veya otomatik toplam tonaj)")
    
    if request.rollSettings.maxOrdersPerRoll < 1:
        errors.append("Maksimum sipariş/rulo en az 1 olmalıdır")
    
    if request.rollSettings.maxOrdersPerRoll > len(request.orders):
        errors.append(
            f"Maksimum sipariş/rulo ({request.rollSettings.maxOrdersPerRoll}) "
            f"toplam sipariş sayısından ({len(request.orders)}) fazla olamaz"
        )
    
    # Talep hesaplama ve kontrol (panel uzunluğu ile)
    panel_lengths = [float(o.panelLength or 1.0) for o in request.orders]
    orders_list = [{"m2": o.m2, "panelWidth": o.panelWidth, "panelLength": panel_lengths[i]} for i, o in enumerate(request.orders)]
    D, total_tonnage_needed = calculate_demand(
        orders_list,
        request.material.thickness,
        request.material.density,
        panel_widths=[o.panelWidth for o in request.orders],
        panel_lengths=panel_lengths,
    )
    
    if total_roll < total_tonnage_needed:
        warnings.append(
            f"Toplam rulo tonajı ({total_roll:.2f} ton) "
            f"ihtiyaçtan ({total_tonnage_needed:.2f} ton) az. Optimizasyon başarısız olabilir."
        )
    
    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "totalTonnageNeeded": total_tonnage_needed
    }


if __name__ == "__main__":
    import uvicorn
    # Railway (ve benzeri platformlar) PORT env değişkeni verir; yoksa local için 8000
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)


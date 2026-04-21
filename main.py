"""
FastAPI Backend - Optimizasyon API
"""
from dotenv import load_dotenv
load_dotenv()

import logging
import time
from collections import defaultdict
from threading import Lock
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Any, List, Dict, Optional, Literal, Tuple
import uuid
import os
from optimizer import (
    generate_rolls,
    calculate_demand,
    solve_optimization,
    create_excel_report,
)
from thesis_failure_codes import (
    CAPACITY_LT_DEMAND,
    INFEASIBLE_UNKNOWN,
    INVALID_MATERIAL,
    INVALID_ROLLS,
    MAX_ROLLS_PER_ORDER_RULE,
    NO_ORDERS,
    classify_infeasible_structure,
    hints_for_code,
)
from thesis_xlsx_report import scenario_meta_from_dashboard_inputs

from supabase_client import (
    SupabaseTransportError,
    SupabaseWriteError,
    save_optimization_result,
    save_configuration,
    get_configuration_by_id,
    update_run_configuration_id,
    delete_run_by_file_id,
    delete_report_from_storage,
    list_orders,
    save_order,
    delete_order,
    list_stock_rolls,
    add_stock_roll,
    update_stock_roll,
    delete_stock_roll,
    process_optimization_result,
    cancel_run,
    upload_report_to_storage,
    update_report_url,
    list_runs,
    get_run_by_file_id,
    insert_customer_request,
    list_customer_requests,
    get_customer_request,
    update_customer_request,
    set_customer_request_converted,
    delete_customer_request,
)

app = FastAPI(title="Kesme Stoku Optimizasyon API")

# Optimizasyon tek senaryo: sipariş m² tek yüzey, talep her zaman çift yüzey (2x) ile hesaplanır.
SURFACE_FACTOR_OPTIMIZE = 2.0

# POST /api/customer-requests için IP başına pencere içi istek sınırı (bellek içi; çok işçili ortamda paylaşılmaz).
_customer_request_rate_times: Dict[str, List[float]] = defaultdict(list)
_customer_request_rate_lock = Lock()
_CUSTOMER_REQUEST_RATE_WINDOW_SEC = 60.0
_CUSTOMER_REQUEST_RATE_MAX = 20


def _enforce_customer_request_post_rate_limit(request: Request) -> None:
    """
    Halka açık talep formunu basit IP bazlı rate limit ile korur.
    """
    if request.client is None:
        return
    ip = request.client.host or "unknown"
    now = time.monotonic()
    with _customer_request_rate_lock:
        times = _customer_request_rate_times[ip]
        times[:] = [t for t in times if now - t < _CUSTOMER_REQUEST_RATE_WINDOW_SEC]
        if len(times) >= _CUSTOMER_REQUEST_RATE_MAX:
            raise HTTPException(
                status_code=429,
                detail="Çok fazla istek. Lütfen bir süre sonra tekrar deneyin.",
            )
        times.append(now)


def _optimization_error_detail(
    message: str,
    failure_code: Optional[str] = None,
    extra_hints: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    /api/optimize JSON hata gövdesi: message, failureCode, hints (frontend toast için).

    Args:
        message: Kullanıcıya gösterilecek ana metin
        failure_code: Makine kodu (thesis_failure_codes)
        extra_hints: Koda özel ek ipuçları

    Returns:
        FastAPI detail sözlüğü
    """
    hints: List[str] = []
    if failure_code:
        hints.extend(hints_for_code(failure_code))
    if extra_hints:
        hints.extend(extra_hints)
    out: Dict[str, Any] = {"message": message, "hints": hints}
    if failure_code:
        out["failureCode"] = failure_code
    return out


def _get_run_row_or_http_exception(file_id: str) -> Dict:
    """
    optimization_runs satırını getirir. Supabase ulaşılamazsa 503, kayıt yoksa 404 döner.
    """
    try:
        run = get_run_by_file_id(file_id)
    except SupabaseTransportError as e:
        logger.warning("Supabase erişilemiyor (file_id=%s): %s", file_id, e)
        raise HTTPException(
            status_code=503,
            detail=(
                "Veritabanına (Supabase) bağlanılamadı. İnternet, VPN, firewall ve .env içindeki "
                "SUPABASE_URL (örn. https://xxxx.supabase.co) ile DNS çözümlemesini kontrol edin."
            ),
        ) from e
    if not run:
        raise HTTPException(status_code=404, detail="Çalıştırma bulunamadı")
    return run

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
    """Optimizasyon isteği; talep çarpanı sunucuda sabit çift yüzey (2x) olarak uygulanır."""
    material: MaterialInput
    orders: List[OrderInput]
    rollSettings: RollSettingsInput
    costs: CostsInput
    safetyStock: Optional[float] = 0
    surfaceFactor: Optional[float] = None  # İstemci alanı (geri uyumluluk); optimize yolunda yok sayılır, 2 kullanılır
    requireDualRollAllocation: Optional[bool] = None  # Geri uyumluluk; yok sayılır
    maxInterleavingOrders: Optional[int] = 2  # Araya max kaç farklı sipariş (soft ceza eşiği)
    interleavingPenaltyCost: Optional[float] = 0.0  # Fazla araya sipariş başına ceza (0 = ceza kapalı)
    configurationId: Optional[str] = None
    saveToDb: Optional[bool] = True  # False ise sadece hesaplama, DB'ye kaydetmez
    description: Optional[str] = None  # Kısa açıklama; sonuçlar tablosunda ID yerine gösterilir
    stock_roll_ids: Optional[List[str]] = Field(None, alias="stockRollIds")
    strategy_modes: Optional[List[Literal["az", "orta", "cok", "eszamanli"]]] = Field(
        None,
        alias="strategyModes",
    )
    sync_level: Optional[Literal["serbest", "dengeli", "siki"]] = Field(None, alias="syncLevel")
    sync_levels: Optional[List[Literal["serbest", "dengeli", "siki"]]] = Field(
        None,
        alias="syncLevels",
    )


class OrderCreateUpdate(BaseModel):
    """Sipariş oluşturma/güncelleme isteği."""
    id: Optional[str] = None
    order_id: Optional[str] = None
    m2: float
    panel_width: float
    panel_length: Optional[float] = 1.0
    il: Optional[str] = None
    bitis_tarihi: Optional[str] = None
    aciklama: Optional[str] = None
    status: Optional[str] = "Pending"


class CustomerRequestCreate(BaseModel):
    """Halka açık teklif talebi formu gövdesi."""

    firma_adi: str = Field(..., min_length=1, max_length=500)
    yetkili_adi: str = Field(..., min_length=1, max_length=200)
    email: str = Field(..., min_length=3, max_length=320)
    telefon: str = Field(..., min_length=6, max_length=50)
    m2: float
    panel_width: float
    panel_length: float = 1.0
    il: Optional[str] = Field(None, max_length=100)
    bitis_tarihi: Optional[str] = None
    musteri_notu: Optional[str] = Field(None, max_length=5000)

    @field_validator("email")
    @classmethod
    def validate_email_format(cls, v: str) -> str:
        """E-postayı trimler ve basit biçim doğrulaması yapar (email-validator bağımlılığı olmadan)."""
        s = (v or "").strip().lower()
        if "@" not in s or s.startswith("@") or s.endswith("@") or s.count("@") != 1:
            raise ValueError("Geçerli bir e-posta girin")
        local, _, domain = s.partition("@")
        if len(local) < 1 or len(domain) < 3 or "." not in domain:
            raise ValueError("Geçerli bir e-posta girin")
        return s

    @field_validator("telefon")
    @classmethod
    def validate_telefon_trim(cls, v: str) -> str:
        """Telefonu trimler; en az 6 karakter olmalıdır."""
        t = (v or "").strip()
        if len(t) < 6:
            raise ValueError("Telefon numarası en az 6 karakter olmalıdır")
        return t


class CustomerRequestPatch(BaseModel):
    """Admin: talep satırı kısmi güncelleme."""

    status: Optional[str] = None
    admin_notu: Optional[str] = None
    tahmini_teklif: Optional[str] = None

    @model_validator(mode="after")
    def at_least_one_field(self) -> "CustomerRequestPatch":
        """En az bir alan dolu olmalıdır."""
        if self.status is None and self.admin_notu is None and self.tahmini_teklif is None:
            raise ValueError("En az bir alan (status, admin_notu, tahmini_teklif) gönderilmelidir")
        return self


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
    surfaceFactor: Optional[float] = 1.0
    requireDualRollAllocation: Optional[bool] = False
    maxInterleavingOrders: Optional[int] = 2
    interleavingPenaltyCost: Optional[float] = 0.0


class StockRollCreate(BaseModel):
    """Rulo ekleme isteği."""
    tonnage: float


class SummaryResponse(BaseModel):
    totalCost: float
    totalFire: float
    totalStock: float
    totalUnusedInventoryTon: float = Field(
        0.0,
        description="Hiç açılmamış ruloların toplam tonu (rafta kalan bobin).",
    )
    openedRolls: int
    sequencePenalty: float = 0.0
    interleavingViolationCount: int = 0
    rollChangeCount: Optional[int] = None
    surfaceSyncViolations: Optional[int] = None
    costFireLira: float = Field(
        0.0,
        description="Rapor fire tonu × fire maliyeti (cf).",
    )
    costStockLira: float = Field(
        0.0,
        description="Stok tutma (h × ton): üretim stoğu + rafta elde bobin toplamı.",
    )
    totalStockHoldingTon: float = Field(
        0.0,
        description="h ile çarpılan toplam ton: totalStock + totalUnusedInventoryTon.",
    )
    costStockProductionLira: float = Field(
        0.0,
        description="Sadece üretim stoku tonu × h (maliyet kırılımı).",
    )
    costStockShelfLira: float = Field(
        0.0,
        description="Sadece rafta elde ton × h (maliyet kırılımı).",
    )
    costSetupLira: float = Field(
        0.0,
        description="Açılan rulo adedi × kurulum (rulo açma) maliyeti.",
    )
    costSequencePenaltyLira: float = Field(
        0.0,
        description="Sipariş sırası ihlali cezasının TL karşılığı (varsa).",
    )


class SequenceViolationItem(BaseModel):
    """Siparişe geç dönüş (araya fazla sipariş) ihlali kaydı."""

    rollId: int
    orderId: int
    distinctInterleavedOrders: int
    maxAllowed: int
    excess: int


class CuttingPlanItem(BaseModel):
    rollId: int
    orderId: int
    panelCount: int
    panelWidth: float
    panelLength: Optional[float] = 1.0
    tonnage: float
    m2: float


class RollStatusItem(BaseModel):
    """
    Rulo bazlı durum. Ton alanları optimizörde kg tam sayı defterine göre üretilir (her biri 0,001 t katı).
    """

    rollId: int
    totalTonnage: float
    used: float
    remaining: float
    fire: float
    stock: float
    ordersUsed: int
    unusedRollTonnage: float = Field(
        0.0,
        description="Açılmamış ruloda rafta kalan bobin tonu; açılmış ruloda 0.",
    )


class ModeComparisonItem(BaseModel):
    """Çok modlu çalıştırmada her strateji için özet karşılaştırma satırı."""

    mode: Literal["az", "orta", "cok", "eszamanli"]
    status: str
    objective: float
    totalCost: Optional[float] = None
    totalFire: Optional[float] = None
    totalStock: Optional[float] = None
    openedRolls: Optional[int] = None
    rollChangeCount: Optional[int] = None
    surfaceSyncViolations: Optional[int] = None


class SyncComparisonItem(BaseModel):
    """Senkron seviye bazlı kısa karşılaştırma satırı."""

    syncLevel: Literal["serbest", "dengeli", "siki"]
    status: str
    totalCost: Optional[float] = None
    totalFire: Optional[float] = None
    rollChangeCount: Optional[int] = None
    synchronousChanges: Optional[int] = None
    independentChanges: Optional[int] = None


class LineEventItem(BaseModel):
    """Üst/alt hat üzerinde tek bir tak-çıkar/devam olayı."""

    timestampStep: int
    line: Literal["ust", "alt"]
    action: Literal["tak", "cikar", "devam"]
    rollId: int
    orderIdFrom: Optional[int] = None
    orderIdTo: Optional[int] = None


class LineTransitionsSummary(BaseModel):
    """Hat değişim özet metrikleri."""

    totalChanges: int = 0
    synchronousChanges: int = 0
    independentChanges: int = 0
    stepCount: int = 0


class LineScheduleCutItem(BaseModel):
    """Tek adımda kesilen sipariş parçası."""

    orderId: int
    rollId: int
    tonnage: float
    m2: float
    upperTonnage: float = 0.0
    lowerTonnage: float = 0.0


class LineScheduleStepItem(BaseModel):
    """Tek hat adımı: üst/alt rulo durumu ve o adımda kesilen parçalar."""

    step: int
    orderId: int
    upperRollId: Optional[int] = None
    lowerRollId: Optional[int] = None
    upperAction: Optional[str] = None
    lowerAction: Optional[str] = None
    orderAction: Optional[str] = None
    actionSummary: Optional[str] = None
    prevUpperRollId: Optional[int] = None
    prevLowerRollId: Optional[int] = None
    cuts: List[LineScheduleCutItem] = Field(default_factory=list)


class OptimizeResponse(BaseModel):
    status: str
    objective: float
    summary: SummaryResponse
    cuttingPlan: List[CuttingPlanItem]
    rollStatus: List[RollStatusItem]
    fileId: str
    sequencePenalty: float = 0.0
    sequenceViolations: List[SequenceViolationItem] = Field(default_factory=list)
    rollOrderSequences: Dict[str, List[int]] = Field(default_factory=dict)
    selectedModes: List[Literal["az", "orta", "cok", "eszamanli"]] = Field(default_factory=list)
    selectedModesCount: int = 0
    comparisonEnabled: bool = False
    modeComparisons: List[ModeComparisonItem] = Field(default_factory=list)
    selectedSyncLevels: List[Literal["serbest", "dengeli", "siki"]] = Field(default_factory=list)
    selectedSyncLevelsCount: int = 0
    syncComparisons: List[SyncComparisonItem] = Field(default_factory=list)
    lineEvents: List[LineEventItem] = Field(default_factory=list)
    lineSchedule: List[LineScheduleStepItem] = Field(default_factory=list)
    lineTransitionsSummary: LineTransitionsSummary = Field(default_factory=LineTransitionsSummary)


@app.get("/")
async def root():
    """API root endpoint"""
    return {"message": "Kesme Stoku Optimizasyon API", "version": "1.0.0"}


def _resolve_strategy_modes(request: OptimizeRequest) -> List[Literal["az", "orta", "cok", "eszamanli"]]:
    """
    İstekten çalıştırılacak strateji modlarını normalize eder.

    Args:
        request: Optimize isteği

    Returns:
        Sırası korunmuş, tekrarsız mod listesi
    """
    requested = request.strategy_modes or []
    valid: List[Literal["az", "orta", "cok", "eszamanli"]] = []
    seen = set()
    for mode in requested:
        if mode in seen:
            continue
        seen.add(mode)
        valid.append(mode)
    return valid


def _resolve_sync_levels(request: OptimizeRequest) -> List[Literal["serbest", "dengeli", "siki"]]:
    """
    İstekten çalıştırılacak senkron seviyelerini normalize eder.
    """
    requested = request.sync_levels or ([request.sync_level] if request.sync_level else [])
    valid: List[Literal["serbest", "dengeli", "siki"]] = []
    seen = set()
    for level in requested:
        if level in seen:
            continue
        seen.add(level)
        valid.append(level)
    return valid


def _build_sync_profile(level: Literal["serbest", "dengeli", "siki"]) -> Dict[str, float | int | bool | str]:
    """
    Senkron seviyesine göre çözücü ayarlarını döner.
    """
    if level == "siki":
        return {
            "sync_level": "siki",
            "enforce_surface_sync": True,
            "sync_penalty_weight": 0.0,
        }
    if level == "dengeli":
        return {
            "sync_level": "dengeli",
            "enforce_surface_sync": False,
            "sync_penalty_weight": 45.0,
        }
    return {
        "sync_level": "serbest",
        "enforce_surface_sync": False,
        "sync_penalty_weight": 0.0,
    }


def _build_sync_comparison_item(
    level: Literal["serbest", "dengeli", "siki"],
    status: str,
    results: Optional[Dict],
) -> SyncComparisonItem:
    """
    Senkron seviye sonucu için kısa karşılaştırma satırı üretir.
    """
    if results is None:
        return SyncComparisonItem(syncLevel=level, status=status)
    summary = results.get("summary") or {}
    trans = results.get("lineTransitionsSummary") or {}
    return SyncComparisonItem(
        syncLevel=level,
        status=status,
        totalCost=float(summary.get("totalCost", 0.0) or 0.0),
        totalFire=float(summary.get("totalFire", 0.0) or 0.0),
        rollChangeCount=int(summary.get("rollChangeCount", 0) or 0),
        synchronousChanges=int(trans.get("synchronousChanges", 0) or 0),
        independentChanges=int(trans.get("independentChanges", 0) or 0),
    )


def _build_mode_profile(
    mode: Literal["az", "orta", "cok", "eszamanli"],
    request: OptimizeRequest,
) -> Dict[str, float | int | bool]:
    """
    Strateji adına göre çözücü parametre profilini üretir.

    Args:
        mode: Çalıştırılacak strateji modu
        request: Kullanıcı isteği

    Returns:
        solve_optimization için profil parametreleri
    """
    base_max_orders = int(request.rollSettings.maxOrdersPerRoll)
    base_max_rolls = int(request.rollSettings.maxRollsPerOrder)
    base_fire = float(request.costs.fireCost)
    base_setup = float(request.costs.setupCost)
    base_stock = float(request.costs.stockCost)
    base_max_interleaving = int(request.maxInterleavingOrders if request.maxInterleavingOrders is not None else 2)
    base_penalty = float(request.interleavingPenaltyCost or 0.0)

    if mode == "az":
        return {
            "max_orders_per_roll": max(1, min(base_max_orders, 2)),
            "max_rolls_per_order": max(2, min(base_max_rolls, 4)),
            "fire_cost": base_fire * 0.85,
            "setup_cost": max(1.0, base_setup * 1.8),
            "stock_cost": base_stock,
            "max_interleaving_orders": max(0, min(base_max_interleaving, 1)),
            "interleaving_penalty_cost": max(base_penalty, base_setup * 0.7),
            "enforce_surface_sync": False,
        }
    if mode == "cok":
        return {
            "max_orders_per_roll": max(base_max_orders, 4),
            "max_rolls_per_order": max(base_max_rolls, 8),
            "fire_cost": base_fire * 1.25,
            "setup_cost": max(0.1, base_setup * 0.65),
            "stock_cost": base_stock,
            "max_interleaving_orders": max(base_max_interleaving, 4),
            "interleaving_penalty_cost": max(0.0, base_penalty * 0.6),
            "enforce_surface_sync": False,
        }
    if mode == "eszamanli":
        return {
            "max_orders_per_roll": max(1, min(base_max_orders, 3)),
            "max_rolls_per_order": max(2, min(base_max_rolls, 6)),
            "fire_cost": base_fire,
            "setup_cost": max(1.0, base_setup * 1.2),
            "stock_cost": base_stock,
            "max_interleaving_orders": max(0, min(base_max_interleaving, 1)),
            "interleaving_penalty_cost": max(base_penalty, base_setup),
            "enforce_surface_sync": True,
        }
    return {
        "max_orders_per_roll": max(1, base_max_orders),
        "max_rolls_per_order": max(2, base_max_rolls),
        "fire_cost": base_fire,
        "setup_cost": base_setup,
        "stock_cost": base_stock,
        "max_interleaving_orders": max(0, base_max_interleaving),
        "interleaving_penalty_cost": max(0.0, base_penalty),
        "enforce_surface_sync": False,
    }


def _build_mode_comparison_item(
    mode: Literal["az", "orta", "cok", "eszamanli"],
    status: str,
    results: Optional[Dict],
) -> ModeComparisonItem:
    """
    Çözüm sonucunu frontend için sabit şemalı karşılaştırma satırına dönüştürür.

    Args:
        mode: Strateji modu
        status: Solver durumu
        results: Çözücü çıktı sözlüğü

    Returns:
        ModeComparisonItem
    """
    if results is None:
        return ModeComparisonItem(mode=mode, status=status, objective=0.0)
    summary = results.get("summary") or {}
    return ModeComparisonItem(
        mode=mode,
        status=status,
        objective=float(results.get("objective", 0.0) or 0.0),
        totalCost=float(summary.get("totalCost", 0.0) or 0.0),
        totalFire=float(summary.get("totalFire", 0.0) or 0.0),
        totalStock=float(summary.get("totalStock", 0.0) or 0.0),
        openedRolls=int(summary.get("openedRolls", 0) or 0),
        rollChangeCount=int(results.get("rollChangeCount", summary.get("rollChangeCount", 0)) or 0),
        surfaceSyncViolations=int(
            results.get("surfaceSyncViolations", summary.get("surfaceSyncViolations", 0)) or 0
        ),
    )


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
            raise HTTPException(
                status_code=400,
                detail=_optimization_error_detail(
                    "Kalınlık 0'dan büyük olmalıdır", failure_code=INVALID_MATERIAL
                ),
            )

        if request.material.density <= 0:
            logger.warning("[400] Yoğunluk 0'dan büyük olmalıdır (density=%s)", request.material.density)
            raise HTTPException(
                status_code=400,
                detail=_optimization_error_detail(
                    "Yoğunluk 0'dan büyük olmalıdır", failure_code=INVALID_MATERIAL
                ),
            )

        if len(request.orders) == 0:
            logger.warning("[400] En az bir sipariş gerekli")
            raise HTTPException(
                status_code=400,
                detail=_optimization_error_detail("En az bir sipariş gerekli", failure_code=NO_ORDERS),
            )
        
        rs = request.rollSettings
        if rs.rolls and len(rs.rolls) > 0:
            # Manuel tonajları tam sayıya yuvarlamayın (örn. 5,89 → 6); aksi halde farklı
            # kapasiteler eşitlenir ve çözücü eş maliyetli rulolar arasında keyfi seçim yapar.
            rolls = [round(float(r), 4) for r in rs.rolls if r > 0]
            if len(rolls) == 0:
                logger.warning("[400] Manuel rulo listesi boş veya geçersiz")
                raise HTTPException(
                    status_code=400,
                    detail=_optimization_error_detail(
                        "En az bir geçerli rulo tonajı girin", failure_code=INVALID_ROLLS
                    ),
                )
            total_roll_tonnage = sum(rolls)
        else:
            if not rs.totalTonnage or rs.totalTonnage <= 0:
                logger.warning("[400] Toplam rulo tonajı 0'dan büyük olmalı (totalTonnage=%s)", rs.totalTonnage)
                raise HTTPException(
                    status_code=400,
                    detail=_optimization_error_detail(
                        "Toplam rulo tonajı 0'dan büyük olmalıdır", failure_code=INVALID_ROLLS
                    ),
                )
            total_roll_tonnage = int(rs.totalTonnage)
            rolls = generate_rolls(total_roll_tonnage, rs.minRollTon, rs.maxRollTon)
        
        if request.rollSettings.maxOrdersPerRoll < 1:
            logger.warning("[400] Maksimum sipariş/rulo en az 1 (maxOrdersPerRoll=%s)", request.rollSettings.maxOrdersPerRoll)
            raise HTTPException(status_code=400, detail="Maksimum sipariş/rulo en az 1 olmalıdır")
        if request.rollSettings.maxRollsPerOrder < 2:
            logger.warning(
                "[400] Çift yüzey senaryosunda max rulo/sipariş en az 2 olmalı (maxRollsPerOrder=%s)",
                request.rollSettings.maxRollsPerOrder,
            )
            raise HTTPException(
                status_code=400,
                detail=_optimization_error_detail(
                    "Çift yüzey senaryosunda maksimum rulo/sipariş en az 2 olmalıdır",
                    failure_code=MAX_ROLLS_PER_ORDER_RULE,
                ),
            )
        
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
            surface_factor=SURFACE_FACTOR_OPTIMIZE,
        )
        
        # Rulo tonajı kontrolü (küçük yuvarlama farklarına tolerans)
        if total_roll_tonnage < total_tonnage_needed - 0.01:
            detail = f"Toplam rulo tonajı ({total_roll_tonnage:.2f}) ihtiyaçtan ({total_tonnage_needed:.2f}) az olamaz"
            logger.warning("[400] %s", detail)
            raise HTTPException(
                status_code=400,
                detail=_optimization_error_detail(detail, failure_code=CAPACITY_LT_DEMAND),
            )

        selected_modes = _resolve_strategy_modes(request)
        selected_sync_levels = _resolve_sync_levels(request)
        if len(selected_sync_levels) == 0:
            logger.warning("[400] Senkron seviye seçimi boş geldi")
            raise HTTPException(status_code=400, detail="En az bir senkron seviyesi seçin")

        # Senkron seviyeleri için karşılaştırma (mod seçiminden bağımsız baz profil: orta).
        anchor_profile = _build_mode_profile("orta", request)
        sync_status_results: List[Tuple[Literal["serbest", "dengeli", "siki"], str, Optional[Dict]]] = []
        sync_comparisons: List[SyncComparisonItem] = []
        for level in selected_sync_levels:
            sync_profile = _build_sync_profile(level)
            status, results = solve_optimization(
                thickness=request.material.thickness,
                density=request.material.density,
                orders=orders_list,
                panel_widths=panel_widths,
                panel_lengths=panel_lengths,
                rolls=rolls,
                max_orders_per_roll=int(anchor_profile["max_orders_per_roll"]),
                max_rolls_per_order=int(anchor_profile["max_rolls_per_order"]),
                fire_cost=float(anchor_profile["fire_cost"]),
                setup_cost=float(anchor_profile["setup_cost"]),
                stock_cost=float(anchor_profile["stock_cost"]),
                time_limit_seconds=120,
                surface_factor=SURFACE_FACTOR_OPTIMIZE,
                require_dual_roll_allocation=False,
                max_interleaving_orders=int(anchor_profile["max_interleaving_orders"]),
                interleaving_penalty_cost=float(anchor_profile["interleaving_penalty_cost"]),
                enforce_surface_sync=bool(sync_profile["enforce_surface_sync"]),
                sync_level=str(sync_profile["sync_level"]),
                sync_penalty_weight=float(sync_profile["sync_penalty_weight"]),
            )
            sync_status_results.append((level, status, results))
            sync_comparisons.append(_build_sync_comparison_item(level, status, results))

        selected_sync: Optional[Tuple[Literal["serbest", "dengeli", "siki"], str, Optional[Dict]]] = None
        for item in sync_status_results:
            if item[0] == "dengeli" and item[1] == "Optimal" and item[2] is not None:
                selected_sync = item
                break
        if selected_sync is None:
            for item in sync_status_results:
                if item[1] == "Optimal" and item[2] is not None:
                    selected_sync = item
                    break
        if selected_sync is None:
            status = sync_status_results[0][1] if sync_status_results else "Infeasible"
            fail_level = sync_status_results[0][0] if sync_status_results else "dengeli"
            sp = _build_sync_profile(fail_level)
            code, _ = classify_infeasible_structure(
                rolls=rolls,
                num_orders=len(orders_list),
                max_orders_per_roll=int(anchor_profile["max_orders_per_roll"]),
                max_rolls_per_order=int(anchor_profile["max_rolls_per_order"]),
                surface_factor=SURFACE_FACTOR_OPTIMIZE,
                enforce_surface_sync=bool(sp["enforce_surface_sync"]),
            )
            fc = code if status == "Infeasible" else INFEASIBLE_UNKNOWN
            msg = f"Senkron seviyesi için çözüm üretilemedi. Durum: {status}"
            raise HTTPException(
                status_code=400,
                detail=_optimization_error_detail(msg, failure_code=fc),
            )
        selected_sync_level, _, _ = selected_sync
        selected_sync_profile = _build_sync_profile(selected_sync_level)

        # Strateji modu seçiliyse karşılaştırma yap; seçilmediyse baz profil ile tek sonuç üret.
        mode_status_results: List[Tuple[Literal["az", "orta", "cok", "eszamanli"], str, Optional[Dict]]] = []
        mode_comparisons: List[ModeComparisonItem] = []
        selected_mode: Optional[str] = None
        if len(selected_modes) > 0:
            for mode in selected_modes:
                profile = _build_mode_profile(mode, request)
                status, results = solve_optimization(
                    thickness=request.material.thickness,
                    density=request.material.density,
                    orders=orders_list,
                    panel_widths=panel_widths,
                    panel_lengths=panel_lengths,
                    rolls=rolls,
                    max_orders_per_roll=int(profile["max_orders_per_roll"]),
                    max_rolls_per_order=int(profile["max_rolls_per_order"]),
                    fire_cost=float(profile["fire_cost"]),
                    setup_cost=float(profile["setup_cost"]),
                    stock_cost=float(profile["stock_cost"]),
                    time_limit_seconds=120,
                    surface_factor=SURFACE_FACTOR_OPTIMIZE,
                    require_dual_roll_allocation=False,
                    max_interleaving_orders=int(profile["max_interleaving_orders"]),
                    interleaving_penalty_cost=float(profile["interleaving_penalty_cost"]),
                    enforce_surface_sync=bool(selected_sync_profile["enforce_surface_sync"]),
                    sync_level=str(selected_sync_profile["sync_level"]),
                    sync_penalty_weight=float(selected_sync_profile["sync_penalty_weight"]),
                )
                mode_status_results.append((mode, status, results))
                mode_comparisons.append(_build_mode_comparison_item(mode, status, results))

            selected: Optional[Tuple[Literal["az", "orta", "cok", "eszamanli"], str, Optional[Dict]]] = None
            for item in mode_status_results:
                if item[0] == "orta" and item[1] == "Optimal" and item[2] is not None:
                    selected = item
                    break
            if selected is None:
                for item in mode_status_results:
                    if item[1] == "Optimal" and item[2] is not None:
                        selected = item
                        break
            if selected is None:
                status = mode_status_results[0][1] if mode_status_results else "Infeasible"
                detail_msg = f"Optimizasyon çözülemedi. Durum: {status}"
                if status == 'Infeasible':
                    detail_msg += (
                        " Olası nedenler: çift yüzeyde her sipariş için üst ve alt yüzey tonajı ayrı ayrı tam D/2 olmalı; "
                        "en az iki rulo; yeterli toplam kapasite; max sipariş/rulo veya max rulo/sipariş; min. lot / kurulum."
                    )
                logger.warning(
                    "[400] Çok modlu optimizasyon hatası | tonaj=%s, rulo_sayisi=%s, ihtiyaç=%s, maxOrdersPerRoll=%s, maxRollsPerOrder=%s",
                    total_roll_tonnage, len(rolls), total_tonnage_needed,
                    request.rollSettings.maxOrdersPerRoll, request.rollSettings.maxRollsPerOrder
                )
                prof = _build_mode_profile(mode_status_results[0][0], request) if mode_status_results else anchor_profile
                code, _ = classify_infeasible_structure(
                    rolls=rolls,
                    num_orders=len(orders_list),
                    max_orders_per_roll=int(prof["max_orders_per_roll"]),
                    max_rolls_per_order=int(prof["max_rolls_per_order"]),
                    surface_factor=SURFACE_FACTOR_OPTIMIZE,
                    enforce_surface_sync=bool(selected_sync_profile["enforce_surface_sync"]),
                )
                fc = code if status == "Infeasible" else INFEASIBLE_UNKNOWN
                raise HTTPException(
                    status_code=400,
                    detail=_optimization_error_detail(detail_msg, failure_code=fc),
                )
            selected_mode, status, results = selected
        else:
            status, results = solve_optimization(
                thickness=request.material.thickness,
                density=request.material.density,
                orders=orders_list,
                panel_widths=panel_widths,
                panel_lengths=panel_lengths,
                rolls=rolls,
                max_orders_per_roll=int(anchor_profile["max_orders_per_roll"]),
                max_rolls_per_order=int(anchor_profile["max_rolls_per_order"]),
                fire_cost=float(anchor_profile["fire_cost"]),
                setup_cost=float(anchor_profile["setup_cost"]),
                stock_cost=float(anchor_profile["stock_cost"]),
                time_limit_seconds=120,
                surface_factor=SURFACE_FACTOR_OPTIMIZE,
                require_dual_roll_allocation=False,
                max_interleaving_orders=int(anchor_profile["max_interleaving_orders"]),
                interleaving_penalty_cost=float(anchor_profile["interleaving_penalty_cost"]),
                enforce_surface_sync=bool(selected_sync_profile["enforce_surface_sync"]),
                sync_level=str(selected_sync_profile["sync_level"]),
                sync_penalty_weight=float(selected_sync_profile["sync_penalty_weight"]),
            )
            if status != "Optimal" or results is None:
                code, _ = classify_infeasible_structure(
                    rolls=rolls,
                    num_orders=len(orders_list),
                    max_orders_per_roll=int(anchor_profile["max_orders_per_roll"]),
                    max_rolls_per_order=int(anchor_profile["max_rolls_per_order"]),
                    surface_factor=SURFACE_FACTOR_OPTIMIZE,
                    enforce_surface_sync=bool(selected_sync_profile["enforce_surface_sync"]),
                )
                fc = code if status == "Infeasible" else INFEASIBLE_UNKNOWN
                msg = f"Optimizasyon çözülemedi. Durum: {status}"
                raise HTTPException(
                    status_code=400,
                    detail=_optimization_error_detail(msg, failure_code=fc),
                )
        
        file_id = uuid.uuid4().hex[:16]
        input_data = {
            "material": request.material.model_dump(),
            "safetyStock": request.safetyStock,
            "surfaceFactor": SURFACE_FACTOR_OPTIMIZE,
            "maxInterleavingOrders": int(
                request.maxInterleavingOrders
                if request.maxInterleavingOrders is not None
                else 2
            ),
            "interleavingPenaltyCost": float(request.interleavingPenaltyCost or 0.0),
            "configurationId": request.configurationId,
            "orders": [o.model_dump() for o in request.orders],
            "rollSettings": request.rollSettings.model_dump(),
            "costs": request.costs.model_dump(),
            "strategyModes": selected_modes,
            "selectedModes": selected_modes,
            "selectedModesCount": len(selected_modes),
            "comparisonEnabled": len(selected_modes) > 1,
            "syncLevels": selected_sync_levels,
            "selectedSyncLevels": selected_sync_levels,
            "selectedSyncLevelsCount": len(selected_sync_levels),
            "selectedSyncLevel": selected_sync_level,
            "selectedMode": selected_mode,
            "modeComparisons": [item.model_dump() for item in mode_comparisons],
            "syncComparisons": [item.model_dump() for item in sync_comparisons],
            "lineEvents": results.get("lineEvents", []),
            "lineSchedule": results.get("lineSchedule", []),
            "lineTransitionsSummary": results.get("lineTransitionsSummary", {}),
        }
        if getattr(request, "stock_roll_ids", None) and len(request.stock_roll_ids) > 0:
            input_data["stockRollIds"] = request.stock_roll_ids
        run_description = (request.description or "").strip()[:500] if getattr(request, "description", None) else None

        # saveToDb True ise Supabase'e kaydet ve Excel oluştur
        save_to_db = getattr(request, "saveToDb", True)
        if save_to_db is None:
            save_to_db = True
        if save_to_db:
            senaryo_baslik = (run_description or "Optimizasyon çalıştırması")[:500]
            aciklama_parcalari: List[str] = []
            if selected_sync_level:
                aciklama_parcalari.append(f"Senkron seviye: {selected_sync_level}")
            if selected_mode:
                aciklama_parcalari.append(f"Strateji modu: {selected_mode}")
            aciklama_dashboard = " | ".join(aciklama_parcalari) if aciklama_parcalari else None
            dashboard_meta = scenario_meta_from_dashboard_inputs(
                senaryo_adi=senaryo_baslik,
                kalinlik_mm=float(request.material.thickness),
                yogunluk_g_cm3=float(request.material.density),
                rulolar_ton=[float(x) for x in rolls],
                siparisler=orders_list,
                fire_cost=float(request.costs.fireCost),
                setup_cost=float(request.costs.setupCost),
                stock_cost=float(request.costs.stockCost),
                toplam_talep_ton=float(total_tonnage_needed),
                toplam_rulo_kapasitesi_ton=float(total_roll_tonnage),
                guvenlik_payi=float(request.safetyStock)
                if request.safetyStock is not None
                else None,
                max_siparis_per_rulo=int(request.rollSettings.maxOrdersPerRoll),
                max_rulo_per_siparis=int(request.rollSettings.maxRollsPerOrder),
                aciklama=aciklama_dashboard,
            )
            excel_path = create_excel_report(results, file_id, scenario_meta=dashboard_meta)
            save_optimization_result(
                file_id=file_id,
                input_data=input_data,
                summary=results["summary"],
                cutting_plan=results["cuttingPlan"],
                roll_status=results["rollStatus"],
                configuration_id=request.configurationId,
                description=run_description,
            )
            report_url = upload_report_to_storage(excel_path, file_id)
            if report_url:
                update_report_url(file_id, report_url)
        else:
            report_url = None
            # saveToDb False: Excel oluşturma atlanır (önizleme modu)

        # Response oluştur
        response = OptimizeResponse(
            status=results['status'],
            objective=results['objective'],
            summary=SummaryResponse(**results['summary']),
            cuttingPlan=[CuttingPlanItem(**item) for item in results['cuttingPlan']],
            rollStatus=[RollStatusItem(**item) for item in results['rollStatus']],
            fileId=file_id,
            sequencePenalty=float(results.get('sequencePenalty', 0)),
            sequenceViolations=[
                SequenceViolationItem(**v) for v in results.get('sequenceViolations', [])
            ],
            rollOrderSequences=dict(results.get('rollOrderSequences') or {}),
            selectedModes=selected_modes,
            selectedModesCount=len(selected_modes),
            comparisonEnabled=len(selected_modes) > 1,
            modeComparisons=mode_comparisons,
            selectedSyncLevels=selected_sync_levels,
            selectedSyncLevelsCount=len(selected_sync_levels),
            syncComparisons=sync_comparisons,
            lineEvents=[LineEventItem(**e) for e in results.get("lineEvents", [])],
            lineSchedule=[LineScheduleStepItem(**s) for s in results.get("lineSchedule", [])],
            lineTransitionsSummary=LineTransitionsSummary(**(results.get("lineTransitionsSummary") or {})),
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
        if request.rollSettings.maxRollsPerOrder < 2:
            raise HTTPException(
                status_code=400,
                detail="Çift yüzey senaryosunda maksimum rulo/sipariş en az 2 olmalıdır",
            )

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
        run = _get_run_row_or_http_exception(file_id)

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


@app.post("/api/customer-requests")
async def create_customer_request_endpoint(body: CustomerRequestCreate, request: Request):
    """
    Müşteri teklif talebini kaydeder (giriş gerektirmez; rate limit uygulanır).
    """
    _enforce_customer_request_post_rate_limit(request)
    if body.m2 <= 0:
        raise HTTPException(status_code=400, detail="m² 0'dan büyük olmalıdır")
    if body.panel_width <= 0:
        raise HTTPException(status_code=400, detail="Panel genişliği 0'dan büyük olmalıdır")
    if body.panel_length <= 0:
        raise HTTPException(status_code=400, detail="Panel uzunluğu 0'dan büyük olmalıdır")
    try:
        row = insert_customer_request(
            firma_adi=body.firma_adi,
            yetkili_adi=body.yetkili_adi,
            email=body.email,
            telefon=body.telefon,
            m2=body.m2,
            panel_width=body.panel_width,
            panel_length=body.panel_length,
            il=body.il,
            bitis_tarihi=body.bitis_tarihi,
            musteri_notu=body.musteri_notu,
            status="submitted",
        )
        return {"customerRequest": row}
    except SupabaseWriteError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e


@app.get("/api/customer-requests")
async def list_customer_requests_endpoint(status: Optional[str] = None):
    """
    Müşteri taleplerini listeler. Dashboard girişi istemci tarafında; API anahtarı şimdilik kullanılmıyor.
    """
    return {"customerRequests": list_customer_requests(status_filter=status)}


@app.patch("/api/customer-requests/{request_id}")
async def patch_customer_request_endpoint(request_id: str, body: CustomerRequestPatch):
    """
    Talep durumu veya admin alanlarını günceller.
    """
    existing = get_customer_request(request_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Talep bulunamadı")
    try:
        updated = update_customer_request(
            request_id,
            status=body.status,
            admin_notu=body.admin_notu,
            tahmini_teklif=body.tahmini_teklif,
        )
        return {"customerRequest": updated}
    except SupabaseWriteError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e


@app.post("/api/customer-requests/{request_id}/convert-to-order")
async def convert_customer_request_endpoint(request_id: str, body: OrderCreateUpdate):
    """
    Talebi onaylayıp sipariş satırı oluşturur ve talebi converted olarak işaretler.
    """
    existing = get_customer_request(request_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Talep bulunamadı")
    st = (existing.get("status") or "").lower()
    if st == "converted":
        raise HTTPException(status_code=400, detail="Bu talep zaten siparişe dönüştürülmüş")
    if st == "rejected":
        raise HTTPException(status_code=400, detail="Reddedilmiş talep siparişe dönüştürülemez")
    if not (body.order_id or "").strip():
        raise HTTPException(status_code=400, detail="Sipariş adı (order_id) zorunludur")
    if body.m2 <= 0:
        raise HTTPException(status_code=400, detail="m² 0'dan büyük olmalıdır")
    if body.panel_width <= 0:
        raise HTTPException(status_code=400, detail="Panel genişliği 0'dan büyük olmalıdır")
    if (body.panel_length or 1) <= 0:
        raise HTTPException(status_code=400, detail="Panel uzunluğu 0'dan büyük olmalıdır")
    try:
        order_row = save_order(
            order_id=body.order_id.strip(),
            m2=body.m2,
            panel_width=body.panel_width,
            panel_length=body.panel_length or 1.0,
            il=body.il,
            bitis_tarihi=body.bitis_tarihi,
            aciklama=body.aciklama,
            status=body.status or "Pending",
            id=None,
        )
        oid = order_row.get("id")
        if not oid:
            raise HTTPException(status_code=500, detail="Sipariş oluşturuldu ancak id alınamadı")
        req_row = set_customer_request_converted(request_id, str(oid))
        return {"order": order_row, "customerRequest": req_row}
    except SupabaseWriteError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e


@app.delete("/api/customer-requests/{request_id}")
async def delete_customer_request_endpoint(request_id: str):
    """
    Yalnızca durumu 'rejected' olan müşteri talebini kalıcı olarak siler.
    """
    existing = get_customer_request(request_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Talep bulunamadı")
    if (existing.get("status") or "").lower() != "rejected":
        raise HTTPException(status_code=400, detail="Sadece reddedilmiş talepler silinebilir")
    try:
        delete_customer_request(request_id)
        return {"ok": True}
    except SupabaseWriteError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e


@app.get("/api/orders")
async def get_orders(status: Optional[str] = None):
    """
    Kayıtlı siparişleri listeler. status=Pending ile filtrelenebilir.
    """
    return {"orders": list_orders(status_filter=status)}


@app.post("/api/orders")
async def upsert_order(request: OrderCreateUpdate):
    """
    Sipariş kaydeder veya günceller.
    """
    if request.m2 <= 0:
        raise HTTPException(status_code=400, detail="m² 0'dan büyük olmalıdır")
    if request.panel_width <= 0:
        raise HTTPException(status_code=400, detail="Panel genişliği 0'dan büyük olmalıdır")
    if (request.panel_length or 1) <= 0:
        raise HTTPException(status_code=400, detail="Panel uzunluğu 0'dan büyük olmalıdır")
    try:
        return save_order(
            order_id=request.order_id,
            m2=request.m2,
            panel_width=request.panel_width,
            panel_length=request.panel_length or 1.0,
            il=request.il,
            bitis_tarihi=request.bitis_tarihi,
            aciklama=request.aciklama,
            status=request.status or "Pending",
            id=request.id,
        )
    except SupabaseWriteError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e


@app.delete("/api/orders/{order_id}")
async def remove_order(order_id: str):
    """
    Siparişi siler.
    """
    ok = delete_order(order_id)
    if not ok:
        raise HTTPException(status_code=500, detail="Sipariş silinemedi")
    return {"ok": True}


@app.get("/api/stock-rolls")
async def get_stock_rolls():
    """
    Kayıtlı stok rulolarını listeler.
    """
    return {"stockRolls": list_stock_rolls()}


@app.post("/api/stock-rolls")
async def create_stock_roll(request: StockRollCreate):
    """
    Yeni rulo ekler.
    """
    if request.tonnage <= 0:
        raise HTTPException(status_code=400, detail="Tonaj 0'dan büyük olmalıdır")
    try:
        return add_stock_roll(tonnage=request.tonnage, source="manual")
    except SupabaseWriteError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e


@app.patch("/api/stock-rolls/{roll_id}")
async def patch_stock_roll(roll_id: str, request: StockRollCreate):
    """
    Rulo tonajını günceller.
    """
    if request.tonnage <= 0:
        raise HTTPException(status_code=400, detail="Tonaj 0'dan büyük olmalıdır")
    try:
        return update_stock_roll(roll_id, request.tonnage)
    except SupabaseWriteError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e


@app.delete("/api/stock-rolls/{roll_id}")
async def remove_stock_roll(roll_id: str):
    """
    Ruloyu siler.
    """
    try:
        delete_stock_roll(roll_id)
    except SupabaseWriteError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e
    return {"ok": True}


@app.post("/api/process-result/{file_id}")
async def process_result_endpoint(file_id: str):
    """
    Optimizasyon sonucunu işleme alır: kalan ruloları stoka ekler, siparişleri günceller.
    """
    _get_run_row_or_http_exception(file_id)
    ok = process_optimization_result(file_id)
    if not ok:
        raise HTTPException(status_code=500, detail="İşleme alınamadı")
    return {"ok": True, "fileId": file_id}


@app.post("/api/runs/{file_id}/cancel")
async def cancel_run_endpoint(file_id: str):
    """
    Optimizasyon çalıştırmasını iptal olarak işaretler.
    """
    _get_run_row_or_http_exception(file_id)
    ok = cancel_run(file_id)
    if not ok:
        raise HTTPException(status_code=500, detail="İptal edilemedi")
    return {"ok": True, "fileId": file_id}


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
    run = _get_run_row_or_http_exception(file_id)
    summary = run.get("summary") or {}
    input_data = run.get("input_data") or {}
    selected_modes = input_data.get("selectedModes") or input_data.get("strategyModes") or []
    selected_sync_levels = input_data.get("selectedSyncLevels") or input_data.get("syncLevels") or []
    mode_comparisons = input_data.get("modeComparisons") or []
    sync_comparisons = input_data.get("syncComparisons") or []
    line_events = input_data.get("lineEvents") or []
    line_schedule = input_data.get("lineSchedule") or []
    line_transitions_summary = input_data.get("lineTransitionsSummary") or {}
    return {
        "fileId": run.get("file_id", file_id),
        "status": run.get("status", "Optimal"),
        "objective": summary.get("totalCost", 0),
        "summary": summary,
        "cuttingPlan": run.get("cutting_plan") or [],
        "rollStatus": run.get("roll_status") or [],
        "configurationId": run.get("configuration_id"),
        "inputData": input_data,
        "selectedModes": selected_modes,
        "selectedModesCount": len(selected_modes),
        "comparisonEnabled": len(selected_modes) > 1,
        "modeComparisons": mode_comparisons,
        "selectedSyncLevels": selected_sync_levels,
        "selectedSyncLevelsCount": len(selected_sync_levels),
        "syncComparisons": sync_comparisons,
        "lineEvents": line_events,
        "lineSchedule": line_schedule,
        "lineTransitionsSummary": line_transitions_summary,
        "createdAt": run.get("created_at"),
        "reportUrl": run.get("report_url"),
        "runStatus": run.get("run_status", "saved"),
        "processedAt": run.get("processed_at"),
        "description": run.get("description"),
    }


@app.get("/api/runs/{file_id}/mode-comparison.csv")
async def get_mode_comparison_csv(file_id: str):
    """
    Belirli bir çalıştırma için mod karşılaştırma özetini CSV olarak döner.
    """
    run = _get_run_row_or_http_exception(file_id)
    input_data = run.get("input_data") or {}
    mode_comparisons = input_data.get("modeComparisons") or []
    if not mode_comparisons:
        raise HTTPException(status_code=404, detail="Bu çalıştırma için mod karşılaştırma verisi bulunamadı")
    header = "mode,status,totalCost,totalFire,rollChangeCount,surfaceSyncViolations"
    rows = [header]
    for item in mode_comparisons:
        rows.append(
            ",".join([
                str(item.get("mode", "")),
                str(item.get("status", "")),
                str(item.get("totalCost", "")),
                str(item.get("totalFire", "")),
                str(item.get("rollChangeCount", "")),
                str(item.get("surfaceSyncViolations", "")),
            ])
        )
    csv_content = "\n".join(rows)
    return PlainTextResponse(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="mode_comparison_{file_id}.csv"'},
    )


@app.get("/api/runs/{file_id}/sync-comparison.csv")
async def get_sync_comparison_csv(file_id: str):
    """
    Belirli bir çalıştırma için senkron seviye karşılaştırma özetini CSV olarak döner.
    """
    run = _get_run_row_or_http_exception(file_id)
    input_data = run.get("input_data") or {}
    sync_comparisons = input_data.get("syncComparisons") or []
    if not sync_comparisons:
        raise HTTPException(status_code=404, detail="Bu çalıştırma için senkron karşılaştırma verisi bulunamadı")
    header = "syncLevel,status,totalCost,totalFire,rollChangeCount,synchronousChanges,independentChanges"
    rows = [header]
    for item in sync_comparisons:
        rows.append(
            ",".join([
                str(item.get("syncLevel", "")),
                str(item.get("status", "")),
                str(item.get("totalCost", "")),
                str(item.get("totalFire", "")),
                str(item.get("rollChangeCount", "")),
                str(item.get("synchronousChanges", "")),
                str(item.get("independentChanges", "")),
            ])
        )
    csv_content = "\n".join(rows)
    return PlainTextResponse(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="sync_comparison_{file_id}.csv"'},
    )


@app.delete("/api/runs/{file_id}")
async def delete_run(file_id: str):
    """
    Belirli bir optimizasyon çalıştırmasını siler.

    Args:
        file_id: Çalıştırma ID değeri
    """
    _get_run_row_or_http_exception(file_id)

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
    if request.rollSettings.maxRollsPerOrder < 2:
        errors.append("Çift yüzey senaryosunda maksimum rulo/sipariş en az 2 olmalıdır")

    if request.maxInterleavingOrders is not None and request.maxInterleavingOrders < 0:
        errors.append("Araya max sipariş sayısı 0 veya pozitif olmalıdır")
    if request.interleavingPenaltyCost is not None and request.interleavingPenaltyCost < 0:
        errors.append("Sıra ceza birimi negatif olamaz")
    
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
        surface_factor=SURFACE_FACTOR_OPTIMIZE,
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


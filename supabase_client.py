"""
Supabase bağlantı ve veritabanı işlemleri.
Kesim sonuçları ve raporları Supabase'de saklanır.
"""
import os
import logging
from typing import Optional, Dict, List, Any
from datetime import datetime, timezone
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Lazy load - Supabase yoksa hata vermez
_client = None


class SupabaseTransportError(Exception):
    """
    Supabase'e TCP/DNS/zaman aşımı ile ulaşılamadığında fırlatılır.
    HTTP 503 ile ayrıştırılır; kayıt yok (404) ile karıştırılmamalıdır.
    """


class SupabaseWriteError(Exception):
    """
    Supabase insert/update/delete başarısız olduğunda fırlatılır (sipariş, stok rulosu vb.).
    HTTP yanıtı için status_code: 503 ağ/yapılandırma, 404 kayıt yok, 502 diğer.
    """

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# Geriye dönük importlar için
OrderSaveError = SupabaseWriteError


def _msg_supabase_required_for_writes() -> str:
    """
    Yazma işlemleri için ortamda Supabase yokken gösterilecek kullanıcı mesajı.
    """
    return (
        "Veritabanı (Supabase) gerekli. backend/.env içinde SUPABASE_URL "
        "(örn. https://xxxx.supabase.co) ve SUPABASE_SERVICE_KEY (secret) tanımlayın; "
        "URL erişilebilir ve DNS çözülebilir olmalıdır."
    )


def _is_network_related_error(exc: Exception) -> bool:
    """
    DNS/TCP/zaman aşımı gibi ulaşılamazlık hatalarını tespit eder (503 için).
    """
    try:
        import httpx
        if isinstance(exc, (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError)):
            return True
    except ImportError:
        pass
    low = str(exc).lower()
    return any(
        s in low
        for s in (
            "nodename nor servname",
            "name or service not known",
            "failed to resolve",
            "connection refused",
            "network is unreachable",
        )
    )


def _supabase_url_has_valid_host(url: str) -> bool:
    """
    SUPABASE_URL içinde http(s) şeması ve çözümlenebilir bir host adı olup olmadığını kabaca doğrular.
    Boş host (ör. 'https://') gibi değerlerde False döner.
    """
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").strip()
    return bool(host)


def _log_supabase_transport_error(operation: str, exc: Exception) -> None:
    """
    DNS / TCP / zaman aşımı kaynaklı Supabase hatalarında kısa uyarı; beklenmeyen hatalarda traceback.
    """
    try:
        import httpx
    except ImportError:
        httpx = None
    if httpx is not None and isinstance(exc, (httpx.ConnectError, httpx.TimeoutException)):
        logger.warning(
            "Supabase %s: sunucuya bağlanılamadı (%s). SUPABASE_URL (ör. https://xxxx.supabase.co) ve ağı kontrol edin.",
            operation,
            exc,
        )
        return
    logger.exception("Supabase %s hatası: %s", operation, exc)


def get_supabase_client():
    """
    Supabase istemcisi döndürür. Ortam değişkenleri ayarlı değilse None döner.
    """
    global _client
    if _client is not None:
        return _client

    url = (os.getenv("SUPABASE_URL") or "").strip()
    service_key = (os.getenv("SUPABASE_SERVICE_KEY") or "").strip()
    fallback_key = (os.getenv("SUPABASE_KEY") or "").strip()
    key = service_key or fallback_key
    key_source = "SUPABASE_SERVICE_KEY" if service_key else ("SUPABASE_KEY" if fallback_key else "")

    if not url or not key:
        logger.info("Supabase bağlantısı atlandı: SUPABASE_URL veya SUPABASE_KEY tanımlı değil")
        return None

    if not _supabase_url_has_valid_host(url):
        logger.info(
            "Supabase bağlantısı atlandı: SUPABASE_URL geçersiz veya host eksik (örn. 'https://xxxx.supabase.co'). "
            ".env içindeki değeri kontrol edin."
        )
        return None

    # Backend için sadece secret key kullanılmalı; publishable key "Invalid API key" verir
    if key.startswith("sb_publishable_"):
        logger.error(
            "Backend için publishable key kullanılıyor (SUPABASE_KEY?). Railway/ortamda SUPABASE_SERVICE_KEY "
            "değişkenine secret key (sb_secret_...) atayın. Publishable key sadece tarayıcı için."
        )
        return None

    try:
        from supabase import create_client, Client
        _client = create_client(url, key)
        return _client
    except ImportError:
        logger.warning("supabase paketi yüklü değil. pip install supabase")
        return None
    except Exception as e:
        err_msg = str(e)
        if "Invalid API key" in err_msg or "API key" in err_msg:
            logger.error(
                "Supabase API anahtarı geçersiz (kullanılan kaynak: %s, key sb_secret_ ile başlıyor mu: %s). "
                "Railway'de SUPABASE_SERVICE_KEY ortam değişkeninin secret key olduğundan emin olun.",
                key_source,
                key.startswith("sb_secret_"),
            )
        logger.exception("Supabase bağlantı hatası: %s", err_msg)
        return None


def _build_run_metrics(summary: Dict[str, Any], cutting_plan: List[Dict], roll_status: List[Dict], status: str) -> Dict[str, Any]:
    """
    Sonuç detay ekranındaki KPI ve grafik özetlerini tek satır metrik kaydına dönüştürür.

    Args:
        summary: Özet alanı (totalCost, totalFire, totalStock, openedRolls)
        cutting_plan: Kesim planı satırları
        roll_status: Rulo durum satırları
        status: Çalıştırma durumu

    Returns:
        optimization_run_metrics tablosuna yazılacak metrik sözlüğü
    """
    total_tonnage = sum(float(r.get("totalTonnage", 0) or 0) for r in roll_status)
    total_used_ton = sum(float(r.get("used", 0) or 0) for r in roll_status)
    total_fire_ton = sum(float(r.get("fire", 0) or 0) for r in roll_status)
    material_usage_pct = (total_used_ton / total_tonnage * 100) if total_tonnage > 0 else 0.0
    fire_pct = (total_fire_ton / total_tonnage * 100) if total_tonnage > 0 else 0.0
    total_panels = int(sum(int(c.get("panelCount", 0) or 0) for c in cutting_plan))
    total_m2 = float(sum(float(c.get("m2", 0) or 0) for c in cutting_plan))
    unique_rolls = len({int(c.get("rollId", 0) or 0) for c in cutting_plan if c.get("rollId") is not None})

    return {
        "status": status,
        "total_cost": float(summary.get("totalCost", 0) or 0),
        "total_fire_ton": float(summary.get("totalFire", 0) or 0),
        "total_stock_ton": float(summary.get("totalStock", 0) or 0),
        "opened_rolls": int(summary.get("openedRolls", 0) or 0),
        "total_tonnage": total_tonnage,
        "total_used_ton": total_used_ton,
        "material_usage_pct": round(material_usage_pct, 4),
        "fire_pct": round(fire_pct, 4),
        "total_panels": total_panels,
        "total_m2": round(total_m2, 4),
        "unique_rolls": unique_rolls,
    }


def _build_roll_status_rows(run_id: str, file_id: str, roll_status: List[Dict]) -> List[Dict[str, Any]]:
    """
    Rulo durum listesini satır bazlı tablo formatına dönüştürür.

    Args:
        run_id: optimization_runs.id
        file_id: Çalıştırma file_id değeri
        roll_status: API rollStatus listesi

    Returns:
        optimization_run_roll_status insert satırları
    """
    return [
        {
            "run_id": run_id,
            "file_id": file_id,
            "roll_id": int(r.get("rollId", 0) or 0),
            "total_tonnage": float(r.get("totalTonnage", 0) or 0),
            "used_ton": float(r.get("used", 0) or 0),
            "remaining_ton": float(r.get("remaining", 0) or 0),
            "fire_ton": float(r.get("fire", 0) or 0),
            "stock_ton": float(r.get("stock", 0) or 0),
            "orders_used": int(r.get("ordersUsed", 0) or 0),
        }
        for r in roll_status
    ]


def _build_cutting_plan_rows(run_id: str, file_id: str, cutting_plan: List[Dict]) -> List[Dict[str, Any]]:
    """
    Kesim planını sipariş-rulo kırılım tablosuna dönüştürür.

    Args:
        run_id: optimization_runs.id
        file_id: Çalıştırma file_id değeri
        cutting_plan: API cuttingPlan listesi

    Returns:
        optimization_run_cutting_plan insert satırları
    """
    return [
        {
            "run_id": run_id,
            "file_id": file_id,
            "roll_id": int(c.get("rollId", 0) or 0),
            "order_id": int(c.get("orderId", 0) or 0),
            "panel_count": int(c.get("panelCount", 0) or 0),
            "panel_width": float(c.get("panelWidth", 0) or 0),
            "tonnage": float(c.get("tonnage", 0) or 0),
            "m2": float(c.get("m2", 0) or 0),
        }
        for c in cutting_plan
    ]


def _persist_run_analytics(
    client: Any,
    run_id: str,
    file_id: str,
    summary: Dict[str, Any],
    cutting_plan: List[Dict],
    roll_status: List[Dict],
    status: str,
) -> None:
    """
    Sonuç ve detay ekranı için normalize analitik tablolarına yazım yapar.

    Args:
        client: Supabase istemcisi
        run_id: optimization_runs.id
        file_id: Çalıştırma file_id değeri
        summary: Özet veriler
        cutting_plan: Kesim planı listesi
        roll_status: Rulo durum listesi
        status: Çalıştırma durumu
    """
    metrics_row = _build_run_metrics(summary, cutting_plan, roll_status, status)
    metrics_row.update({"run_id": run_id, "file_id": file_id})
    client.table("optimization_run_metrics").insert(metrics_row).execute()

    roll_rows = _build_roll_status_rows(run_id, file_id, roll_status)
    if roll_rows:
        client.table("optimization_run_roll_status").insert(roll_rows).execute()

    cutting_rows = _build_cutting_plan_rows(run_id, file_id, cutting_plan)
    if cutting_rows:
        client.table("optimization_run_cutting_plan").insert(cutting_rows).execute()


def save_optimization_result(
    file_id: str,
    input_data: Dict[str, Any],
    summary: Dict[str, Any],
    cutting_plan: List[Dict],
    roll_status: List[Dict],
    configuration_id: Optional[str] = None,
    description: Optional[str] = None,
) -> Optional[str]:
    """
    Optimizasyon sonucunu Supabase'e kaydeder.

    Args:
        file_id: Benzersiz dosya/çalıştırma ID
        input_data: Giriş parametreleri (material, orders, rollSettings, costs)
        summary: Özet (totalCost, totalFire, totalStock, openedRolls)
        cutting_plan: Kesim planı listesi
        roll_status: Rulo durumları listesi
        configuration_id: İlişkili kayıtlı konfigürasyon ID (opsiyonel)
        description: Kısa açıklama (sonuçlar tablosunda gösterilir, opsiyonel)

    Returns:
        Kaydedilen run_id (UUID) veya None
    """
    client = get_supabase_client()
    if not client:
        return None

    try:
        run_status = str(summary.get("status", "Optimal")) if isinstance(summary, dict) else "Optimal"
        desc_trimmed = (description or "").strip()[:500] if description else None
        # Ana kayıt (status: solver durumu, run_status: saved/processed/cancelled)
        run_row = {
            "file_id": file_id,
            "configuration_id": configuration_id,
            "input_data": input_data,
            "summary": summary,
            "cutting_plan": cutting_plan,
            "roll_status": roll_status,
            "status": run_status,
            "run_status": "saved",
        }
        if desc_trimmed is not None:
            run_row["description"] = desc_trimmed
        resp = client.table("optimization_runs").insert(run_row).execute()
        if resp.data and len(resp.data) > 0:
            run_id = str(resp.data[0].get("id"))
            try:
                _persist_run_analytics(
                    client=client,
                    run_id=run_id,
                    file_id=file_id,
                    summary=summary,
                    cutting_plan=cutting_plan,
                    roll_status=roll_status,
                    status=run_status,
                )
            except Exception as analytics_err:
                logger.exception(
                    "Analitik tablo yazımı hatası (ana kayıt tutuldu): file_id=%s, run_id=%s, err=%s",
                    file_id,
                    run_id,
                    str(analytics_err),
                )
            logger.info("Supabase'e kaydedildi: file_id=%s, run_id=%s", file_id, run_id)
            return run_id
    except Exception as e:
        logger.exception("Supabase kayıt hatası: %s", str(e))
    return None


def save_configuration(
    *,
    config_id: Optional[str],
    name: Optional[str],
    material: Dict[str, Any],
    safety_stock: float,
    roll_settings: Dict[str, Any],
    costs: Dict[str, Any],
    orders: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    Konfigürasyonu kaydeder veya günceller.

    Args:
        config_id: Güncellenecek kayıt ID'si (None ise yeni kayıt açılır)
        name: Konfigürasyon adı (opsiyonel)
        material: Malzeme bilgileri
        safety_stock: Emniyet stok yüzdesi
        roll_settings: Rulo ayarları
        costs: Maliyet ayarları
        orders: Sipariş listesi

    Returns:
        Kaydedilen satır (id, created_at, updated_at, ... ) veya None
    """
    client = get_supabase_client()
    if not client:
        return None

    try:
        row = {
            "name": name,
            "material_thickness": float(material.get("thickness", 0) or 0),
            "material_density": float(material.get("density", 0) or 0),
            "safety_stock": float(safety_stock or 0),
            "max_orders_per_roll": int(roll_settings.get("maxOrdersPerRoll", 0) or 0),
            "max_rolls_per_order": int(roll_settings.get("maxRollsPerOrder", 0) or 0),
            "fire_cost": float(costs.get("fireCost", 0) or 0),
            "setup_cost": float(costs.get("setupCost", 0) or 0),
            "stock_cost": float(costs.get("stockCost", 0) or 0),
            "rolls": roll_settings.get("rolls", []),
            "orders": orders,
        }

        if config_id:
            row["updated_at"] = datetime.now(timezone.utc).isoformat()
            resp = (
                client.table("optimization_configurations")
                .update(row)
                .eq("id", config_id)
                .execute()
            )
            if resp.data and len(resp.data) > 0:
                return resp.data[0]
            return None

        resp = client.table("optimization_configurations").insert(row).execute()
        if resp.data and len(resp.data) > 0:
            return resp.data[0]
    except Exception as e:
        logger.exception("Konfigürasyon kayıt hatası: %s", str(e))
    return None


def get_configuration_by_id(config_id: str) -> Optional[Dict[str, Any]]:
    """
    Kayıtlı konfigürasyonu ID ile getirir.

    Args:
        config_id: Konfigürasyon UUID değeri

    Returns:
        Konfigürasyon satırı veya None
    """
    client = get_supabase_client()
    if not client:
        return None

    try:
        resp = (
            client.table("optimization_configurations")
            .select("*")
            .eq("id", config_id)
            .execute()
        )
        if resp.data and len(resp.data) > 0:
            return resp.data[0]
    except Exception as e:
        logger.exception("Konfigürasyon sorgu hatası: %s", str(e))
    return None


def update_run_configuration_id(file_id: str, configuration_id: str) -> bool:
    """
    Çalıştırma kaydının iliştirilmiş konfigürasyon ID'sini günceller.

    Args:
        file_id: Çalıştırma file_id değeri
        configuration_id: optimization_configurations.id değeri

    Returns:
        Güncelleme başarılıysa True, aksi halde False
    """
    client = get_supabase_client()
    if not client:
        return False

    try:
        client.table("optimization_runs").update(
            {"configuration_id": configuration_id}
        ).eq("file_id", file_id).execute()
        return True
    except Exception as e:
        logger.exception("run configuration_id güncelleme hatası: %s", str(e))
        return False


def upload_report_to_storage(file_path: str, file_id: str) -> Optional[str]:
    """
    Excel raporunu Supabase Storage'a yükler.

    Args:
        file_path: Yerel dosya yolu
        file_id: Dosya ID

    Returns:
        Public URL veya None
    """
    client = get_supabase_client()
    if not client:
        return None

    if not os.path.exists(file_path):
        logger.warning("Rapor dosyası bulunamadı: %s", file_path)
        return None

    try:
        with open(file_path, "rb") as f:
            data = f.read()

        storage_path = f"reports/cozum_raporu_{file_id}.xlsx"
        bucket = "optimization-reports"

        client.storage.from_(bucket).upload(
            storage_path,
            data,
            {"content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
        )
        url = client.storage.from_(bucket).get_public_url(storage_path)
        logger.info("Rapor Storage'a yüklendi: %s", storage_path)
        return url
    except Exception as e:
        logger.exception("Storage yükleme hatası: %s", str(e))
    return None


def update_report_url(file_id: str, report_url: str) -> bool:
    """
    Optimizasyon çalıştırmasının rapor URL'ini günceller.
    """
    client = get_supabase_client()
    if not client:
        return False
    try:
        client.table("optimization_runs").update({"report_url": report_url}).eq("file_id", file_id).execute()
        return True
    except Exception as e:
        logger.exception("report_url güncelleme hatası: %s", str(e))
        return False


def list_runs(limit: int = 50, offset: int = 0) -> List[Dict]:
    """
    Optimizasyon çalıştırmalarını listeler (en yeniden eskiye).
    """
    client = get_supabase_client()
    if not client:
        return []

    try:
        resp = (
            client.table("optimization_runs")
            .select("id, file_id, created_at, summary, status, run_status, processed_at, description")
            .order("created_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
        return resp.data or []
    except Exception as e:
        _log_supabase_transport_error("list_runs", e)
        return []


def get_run_by_file_id(file_id: str) -> Optional[Dict]:
    """
    file_id ile optimizasyon çalıştırmasını getirir.
    """
    client = get_supabase_client()
    if not client:
        return None

    try:
        import httpx
    except ImportError:
        httpx = None
    try:
        resp = client.table("optimization_runs").select("*").eq("file_id", file_id).execute()
        if resp.data and len(resp.data) > 0:
            return resp.data[0]
    except Exception as e:
        _log_supabase_transport_error("get_run_by_file_id", e)
        if httpx is not None and isinstance(e, (httpx.ConnectError, httpx.TimeoutException)):
            raise SupabaseTransportError(str(e)) from e
    return None


def list_orders(status_filter: Optional[str] = None) -> List[Dict]:
    """
    Kayıtlı siparişleri listeler.

    Args:
        status_filter: Opsiyonel durum filtresi (örn. 'Pending')

    Returns:
        Sipariş satırları
    """
    client = get_supabase_client()
    if not client:
        return []
    try:
        query = client.table("orders").select("*").order("created_at", desc=True)
        if status_filter:
            query = query.eq("status", status_filter)
        resp = query.execute()
        return resp.data or []
    except Exception as e:
        _log_supabase_transport_error("list_orders", e)
        return []


def save_order(
    *,
    order_id: Optional[str] = None,
    m2: float,
    panel_width: float,
    panel_length: float = 1.0,
    il: Optional[str] = None,
    bitis_tarihi: Optional[str] = None,
    aciklama: Optional[str] = None,
    status: str = "Pending",
    id: Optional[str] = None,
) -> Dict:
    """
    Sipariş kaydeder veya günceller.

    Args:
        order_id: Kullanıcı dostu ID (opsiyonel)
        m2: Talep m²
        panel_width: Panel genişliği (m)
        panel_length: Panel kesim uzunluğu (m)
        il: İl (opsiyonel)
        bitis_tarihi: Bitiş tarihi ISO format (opsiyonel)
        aciklama: Açıklama (opsiyonel)
        status: Durum (Pending, Optimized, In Production)
        id: Güncellenecek sipariş UUID (opsiyonel)

    Returns:
        Kaydedilen sipariş satırı

    Raises:
        SupabaseWriteError: Supabase yok, ağ hatası veya kayıt başarısız
    """
    client = get_supabase_client()
    if not client:
        raise SupabaseWriteError(_msg_supabase_required_for_writes(), status_code=503)
    try:
        row = {
            "order_id": order_id,
            "m2": float(m2),
            "panel_width": float(panel_width),
            "panel_length": float(panel_length),
            "il": il,
            "bitis_tarihi": bitis_tarihi,
            "aciklama": aciklama,
            "status": status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if id:
            resp = client.table("orders").update(row).eq("id", id).execute()
            if resp.data and len(resp.data) > 0:
                return resp.data[0]
            raise SupabaseWriteError(
                "Sipariş güncellenemedi: kayıt bulunamadı veya yetki/RLS engeli olabilir (id doğru mu?).",
                status_code=404,
            )
        row.pop("updated_at", None)
        resp = client.table("orders").insert(row).execute()
        if resp.data and len(resp.data) > 0:
            return resp.data[0]
        raise SupabaseWriteError(
            "Kayıt oluşturulamadı (boş yanıt). Supabase orders tablosu ve RLS politikalarını kontrol edin.",
            status_code=502,
        )
    except SupabaseWriteError:
        raise
    except Exception as e:
        logger.exception("Sipariş kaydedilemedi: %s", str(e))
        code = 503 if _is_network_related_error(e) else 502
        hint = (
            " Veritabanı sunucusuna ulaşılamıyor; SUPABASE_URL ve ağı kontrol edin."
            if code == 503
            else ""
        )
        raise SupabaseWriteError(f"Sipariş kaydedilemedi: {e!s}.{hint}", status_code=code) from e


def delete_order(order_id: str) -> bool:
    """
    Siparişi siler.

    Args:
        order_id: Silinecek sipariş UUID

    Returns:
        Silme başarılıysa True
    """
    client = get_supabase_client()
    if not client:
        return False
    try:
        client.table("orders").delete().eq("id", order_id).execute()
        return True
    except Exception as e:
        logger.exception("Sipariş silinemedi: %s", str(e))
        return False


def list_stock_rolls() -> List[Dict]:
    """
    Kayıtlı stok rulolarını listeler.

    Returns:
        Rulo satırları
    """
    client = get_supabase_client()
    if not client:
        return []
    try:
        resp = (
            client.table("stock_rolls")
            .select("*")
            .order("created_at", desc=True)
            .execute()
        )
        return resp.data or []
    except Exception as e:
        _log_supabase_transport_error("list_stock_rolls", e)
        return []


def add_stock_roll(tonnage: float, source: str = "manual", run_id: Optional[str] = None) -> Dict:
    """
    Yeni rulo ekler.

    Args:
        tonnage: Rulo tonajı
        source: Kaynak ('manual' | 'optimization_leftover')
        run_id: Optimizasyondan geldiyse run UUID (opsiyonel)

    Returns:
        Eklenen rulo satırı

    Raises:
        SupabaseWriteError: Supabase yok, ağ veya API hatası
    """
    client = get_supabase_client()
    if not client:
        raise SupabaseWriteError(_msg_supabase_required_for_writes(), status_code=503)
    try:
        row = {
            "tonnage": float(tonnage),
            "source": source,
            "run_id": run_id,
        }
        resp = client.table("stock_rolls").insert(row).execute()
        if resp.data and len(resp.data) > 0:
            return resp.data[0]
        raise SupabaseWriteError(
            "Rulo eklenemedi (boş yanıt). stock_rolls tablosu ve RLS politikalarını kontrol edin.",
            status_code=502,
        )
    except SupabaseWriteError:
        raise
    except Exception as e:
        logger.exception("Rulo eklenemedi: %s", str(e))
        code = 503 if _is_network_related_error(e) else 502
        hint = " Veritabanına ulaşılamıyor; SUPABASE_URL ve DNS/ağı kontrol edin." if code == 503 else ""
        raise SupabaseWriteError(f"Rulo eklenemedi: {e!s}.{hint}", status_code=code) from e


def update_stock_roll(roll_id: str, tonnage: float) -> Dict:
    """
    Rulo tonajını günceller.

    Args:
        roll_id: Güncellenecek rulo UUID
        tonnage: Yeni tonaj (ton)

    Returns:
        Güncellenen rulo satırı

    Raises:
        SupabaseWriteError: Kayıt yok, ağ veya yetki hatası
    """
    client = get_supabase_client()
    if not client:
        raise SupabaseWriteError(_msg_supabase_required_for_writes(), status_code=503)
    try:
        resp = (
            client.table("stock_rolls")
            .update({"tonnage": float(tonnage)})
            .eq("id", roll_id)
            .execute()
        )
        if resp.data and len(resp.data) > 0:
            return resp.data[0]
        raise SupabaseWriteError(
            "Rulo bulunamadı veya güncellenemedi (id doğru mu? RLS?).",
            status_code=404,
        )
    except SupabaseWriteError:
        raise
    except Exception as e:
        logger.exception("Rulo güncellenemedi: %s", str(e))
        code = 503 if _is_network_related_error(e) else 502
        hint = " Veritabanına ulaşılamıyor; SUPABASE_URL ve ağı kontrol edin." if code == 503 else ""
        raise SupabaseWriteError(f"Rulo güncellenemedi: {e!s}.{hint}", status_code=code) from e


def delete_stock_roll(roll_id: str) -> None:
    """
    Ruloyu siler.

    Args:
        roll_id: Silinecek rulo UUID

    Raises:
        SupabaseWriteError: Supabase yok veya silme başarısız
    """
    client = get_supabase_client()
    if not client:
        raise SupabaseWriteError(_msg_supabase_required_for_writes(), status_code=503)
    try:
        client.table("stock_rolls").delete().eq("id", roll_id).execute()
    except Exception as e:
        logger.exception("Rulo silinemedi: %s", str(e))
        code = 503 if _is_network_related_error(e) else 502
        hint = " Veritabanına ulaşılamıyor; SUPABASE_URL ve ağı kontrol edin." if code == 503 else ""
        raise SupabaseWriteError(f"Rulo silinemedi: {e!s}.{hint}", status_code=code) from e


def process_optimization_result(file_id: str) -> bool:
    """
    Optimizasyon sonucunu işleme alır: kullanılan stok rulolarını stoktan düşer,
    kalan (stock) tonajları yeni rulo olarak stoka yazar, siparişleri In Production yapar,
    çalıştırmayı işlendi olarak işaretler.

    Args:
        file_id: Çalıştırma file_id değeri

    Returns:
        İşlem başarılıysa True
    """
    client = get_supabase_client()
    if not client:
        return False

    try:
        run = get_run_by_file_id(file_id)
        if not run:
            logger.warning("process_optimization_result: Run bulunamadı file_id=%s", file_id)
            return False

        input_data = run.get("input_data") or {}
        orders_input = input_data.get("orders") or []
        run_id = run.get("id")

        # 1) Bu çalıştırmada kullanılan stok rulolarını stoktan sil (optimizasyona giren rulolar)
        stock_roll_ids = input_data.get("stockRollIds") or []
        for roll_id in stock_roll_ids:
            if roll_id:
                delete_stock_roll(roll_id)

        # 2) roll_status'ta stock > 0 olan her rulo için stoka yeni rulo ekle (kalan stok)
        roll_status = run.get("roll_status") or []
        for item in roll_status:
            stock_ton = float(item.get("stock", 0) or 0)
            if stock_ton > 0:
                add_stock_roll(tonnage=stock_ton, source="optimization_leftover", run_id=str(run_id) if run_id else None)

        # 3) cutting_plan'da kullanılan sipariş indekslerini bul
        cutting_plan = run.get("cutting_plan") or []
        used_order_indices = {int(c.get("orderId", -1)) for c in cutting_plan if c.get("orderId") is not None}

        # 4) input_data.orders'dan orderId (UUID) al ve orders tablosunu güncelle
        for idx in used_order_indices:
            if 0 <= idx < len(orders_input):
                order_obj = orders_input[idx]
                db_order_id = order_obj.get("orderId") if isinstance(order_obj, dict) else getattr(order_obj, "orderId", None)
                if db_order_id:
                    client.table("orders").update(
                        {"status": "In Production", "updated_at": datetime.now(timezone.utc).isoformat()}
                    ).eq("id", db_order_id).execute()

        # 5) optimization_runs'ı işlendi olarak işaretle
        client.table("optimization_runs").update({
            "processed_at": datetime.now(timezone.utc).isoformat(),
            "run_status": "processed",
        }).eq("file_id", file_id).execute()

        logger.info("İşleme alındı: file_id=%s (stoktan %s rulo düşüldü, %s kalan rulo eklendi)",
                    file_id, len(stock_roll_ids), sum(1 for r in roll_status if float(r.get("stock", 0) or 0) > 0))
        return True
    except Exception as e:
        logger.exception("process_optimization_result hatası: %s", str(e))
        return False


def cancel_run(file_id: str) -> bool:
    """
    Optimizasyon çalıştırmasını iptal olarak işaretler (silmez).

    Args:
        file_id: Çalıştırma file_id değeri

    Returns:
        Güncelleme başarılıysa True
    """
    client = get_supabase_client()
    if not client:
        return False
    try:
        client.table("optimization_runs").update({
            "run_status": "cancelled",
        }).eq("file_id", file_id).execute()
        logger.info("İptal edildi: file_id=%s", file_id)
        return True
    except Exception as e:
        logger.exception("cancel_run hatası: %s", str(e))
        return False


# Eski order_sets / stock_sets fonksiyonları - geriye dönük uyumluluk (migration sonrası kaldırılabilir)
def list_order_sets() -> List[Dict]:
    """Eski API: order_sets tablosu kaldırıldı. Boş liste döner."""
    return []


def save_order_set(name: str, orders: List[Dict], set_id: Optional[str] = None) -> Optional[Dict]:
    """Eski API: order_sets kaldırıldı."""
    return None


def delete_order_set(set_id: str) -> bool:
    """Eski API: order_sets kaldırıldı."""
    return False


def list_stock_sets() -> List[Dict]:
    """Eski API: stock_sets kaldırıldı. stock_rolls'tan set benzeri gruplama yapılabilir."""
    rolls = list_stock_rolls()
    if not rolls:
        return []
    return [{"id": "default", "name": "Mevcut Rulolar", "rolls": [float(r.get("tonnage", 0)) for r in rolls]}]


def save_stock_set(name: str, rolls: List[float], set_id: Optional[str] = None) -> Optional[Dict]:
    """Eski API: Her ruloyu ayrı ekler."""
    client = get_supabase_client()
    if not client:
        return None
    try:
        for ton in rolls:
            if float(ton) > 0:
                add_stock_roll(tonnage=float(ton), source="manual")
    except SupabaseWriteError:
        return None
    return {"id": "default", "name": name, "rolls": rolls}


def delete_stock_set(set_id: str) -> bool:
    """Eski API: stock_sets kaldırıldı."""
    return False


def delete_report_from_storage(file_id: str) -> bool:
    """
    Supabase Storage'daki Excel rapor dosyasını siler.

    Args:
        file_id: Çalıştırma file_id değeri

    Returns:
        Silme işlemi başarılıysa True, aksi halde False
    """
    client = get_supabase_client()
    if not client:
        return False

    try:
        bucket = "optimization-reports"
        storage_path = f"reports/cozum_raporu_{file_id}.xlsx"
        client.storage.from_(bucket).remove([storage_path])
        return True
    except Exception as e:
        logger.exception("Storage rapor silme hatası: %s", str(e))
        return False


def delete_run_by_file_id(file_id: str) -> bool:
    """
    file_id ile optimizasyon çalıştırmasını ve ilişkili kayıtlarını siler.

    Args:
        file_id: Çalıştırma file_id değeri

    Returns:
        Silme başarılıysa True, aksi halde False
    """
    client = get_supabase_client()
    if not client:
        return False

    try:
        client.table("optimization_runs").delete().eq("file_id", file_id).execute()
        return True
    except Exception as e:
        logger.exception("Run silme hatası: %s", str(e))
        return False

"""
Supabase bağlantı ve veritabanı işlemleri.
Kesim sonuçları ve raporları Supabase'de saklanır.
"""
import os
import logging
from typing import Optional, Dict, List, Any
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Lazy load - Supabase yoksa hata vermez
_client = None


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

    Returns:
        Kaydedilen run_id (UUID) veya None
    """
    client = get_supabase_client()
    if not client:
        return None

    try:
        run_status = str(summary.get("status", "Optimal")) if isinstance(summary, dict) else "Optimal"
        # Ana kayıt
        run_row = {
            "file_id": file_id,
            "configuration_id": configuration_id,
            "input_data": input_data,
            "summary": summary,
            "cutting_plan": cutting_plan,
            "roll_status": roll_status,
            "status": run_status,
        }
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
            .select("id, file_id, created_at, summary, status")
            .order("created_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
        return resp.data or []
    except Exception as e:
        logger.exception("Supabase list_runs hatası: %s", str(e))
        return []


def get_run_by_file_id(file_id: str) -> Optional[Dict]:
    """
    file_id ile optimizasyon çalıştırmasını getirir.
    """
    client = get_supabase_client()
    if not client:
        return None

    try:
        resp = client.table("optimization_runs").select("*").eq("file_id", file_id).execute()
        if resp.data and len(resp.data) > 0:
            return resp.data[0]
    except Exception as e:
        logger.exception("Supabase sorgu hatası: %s", str(e))
    return None


def list_order_sets() -> List[Dict]:
    """
    Kayıtlı sipariş setlerini listeler.

    Returns:
        Sipariş seti satırları
    """
    client = get_supabase_client()
    if not client:
        return []
    try:
        resp = (
            client.table("order_sets")
            .select("*")
            .order("updated_at", desc=True)
            .execute()
        )
        return resp.data or []
    except Exception as e:
        logger.exception("Sipariş setleri listelenemedi: %s", str(e))
        return []


def save_order_set(name: str, orders: List[Dict], set_id: Optional[str] = None) -> Optional[Dict]:
    """
    Sipariş seti kaydeder veya günceller.

    Args:
        name: Set adı
        orders: Sipariş satırları
        set_id: Güncellenecek set ID değeri (opsiyonel)

    Returns:
        Kaydedilen set satırı veya None
    """
    client = get_supabase_client()
    if not client:
        return None
    try:
        row = {
            "name": name,
            "orders": orders,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if set_id:
            resp = client.table("order_sets").update(row).eq("id", set_id).execute()
            if resp.data and len(resp.data) > 0:
                return resp.data[0]
            return None
        resp = client.table("order_sets").insert({"name": name, "orders": orders}).execute()
        if resp.data and len(resp.data) > 0:
            return resp.data[0]
        return None
    except Exception as e:
        logger.exception("Sipariş seti kaydedilemedi: %s", str(e))
        return None


def delete_order_set(set_id: str) -> bool:
    """
    Sipariş setini siler.

    Args:
        set_id: Silinecek set ID değeri

    Returns:
        Silme başarılıysa True
    """
    client = get_supabase_client()
    if not client:
        return False
    try:
        client.table("order_sets").delete().eq("id", set_id).execute()
        return True
    except Exception as e:
        logger.exception("Sipariş seti silinemedi: %s", str(e))
        return False


def list_stock_sets() -> List[Dict]:
    """
    Kayıtlı stok/rulo setlerini listeler.

    Returns:
        Stok seti satırları
    """
    client = get_supabase_client()
    if not client:
        return []
    try:
        resp = (
            client.table("stock_sets")
            .select("*")
            .order("updated_at", desc=True)
            .execute()
        )
        return resp.data or []
    except Exception as e:
        logger.exception("Stok setleri listelenemedi: %s", str(e))
        return []


def save_stock_set(name: str, rolls: List[float], set_id: Optional[str] = None) -> Optional[Dict]:
    """
    Stok/rulo seti kaydeder veya günceller.

    Args:
        name: Set adı
        rolls: Rulo tonaj listesi
        set_id: Güncellenecek set ID değeri (opsiyonel)

    Returns:
        Kaydedilen set satırı veya None
    """
    client = get_supabase_client()
    if not client:
        return None
    try:
        row = {
            "name": name,
            "rolls": rolls,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if set_id:
            resp = client.table("stock_sets").update(row).eq("id", set_id).execute()
            if resp.data and len(resp.data) > 0:
                return resp.data[0]
            return None
        resp = client.table("stock_sets").insert({"name": name, "rolls": rolls}).execute()
        if resp.data and len(resp.data) > 0:
            return resp.data[0]
        return None
    except Exception as e:
        logger.exception("Stok seti kaydedilemedi: %s", str(e))
        return None


def delete_stock_set(set_id: str) -> bool:
    """
    Stok/rulo setini siler.

    Args:
        set_id: Silinecek set ID değeri

    Returns:
        Silme başarılıysa True
    """
    client = get_supabase_client()
    if not client:
        return False
    try:
        client.table("stock_sets").delete().eq("id", set_id).execute()
        return True
    except Exception as e:
        logger.exception("Stok seti silinemedi: %s", str(e))
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

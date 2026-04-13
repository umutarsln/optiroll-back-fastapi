# Backend API - Kesme Stoku Optimizasyon

FastAPI ile geliştirilmiş optimizasyon API'si.

## Supabase (Opsiyonel)

Kesim sonuçları ve raporları Supabase'de saklamak için:

1. **Supabase projesi oluşturun** ve `.env` dosyasına ekleyin:
   ```bash
   cp .env.example .env
   # .env içinde SUPABASE_URL ve SUPABASE_SERVICE_KEY değerlerini girin
   ```

2. **SQL şemasını çalıştırın**: `supabase_schema.sql` dosyasını Supabase Dashboard > SQL Editor'da çalıştırın.

3. **Müşteri talepleri (teklif formu) için**: `supabase_customer_requests.sql` dosyasını aynı SQL Editor'da çalıştırın (sipariş dönüşümü ve admin listesi bu tabloya bağlıdır).

4. **Storage bucket oluşturun**: Dashboard > Storage > New bucket
   - İsim: `optimization-reports`
   - Public: Evet

Supabase ayarlanmamışsa optimizasyon yine çalışır; sonuçlar sadece yerel Excel dosyası olarak kalır.

## Kurulum

```bash
# Virtual environment oluştur (önerilir)
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Bağımlılıkları yükle
pip install -r requirements.txt
```

## Çalıştırma

```bash
# Development server
uvicorn main:app --reload --host 0.0.0.0 --port 8000

# Production server
uvicorn main:app --host 0.0.0.0 --port 8000
```

API dokümantasyonu: http://localhost:8000/docs

## API Endpoints

- `POST /api/optimize` - Optimizasyon çalıştır
- `GET /api/results/{file_id}` - Excel dosyasını indir
- `POST /api/validate` - Input validasyonu
- `POST /api/customer-requests` - Müşteri teklif talebi (halka açık; IP rate limit)
- `GET|PATCH|DELETE /api/customer-requests/{id}`, `POST .../convert-to-order` — Talep listesi, güncelleme, **yalnızca reddedilmiş** talebin silinmesi ve siparişe dönüşüm; şimdilik ek API anahtarı yok (erişim dashboard girişiyle istemci tarafında sınırlanır; üretimde backend’i ağ veya proxy ile kısıtlamayı değerlendirin).



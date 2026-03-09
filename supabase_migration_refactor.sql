-- OptiRoll Refaktör Migration
-- Sipariş-Stok-Optimizasyon refaktörü için Supabase migration.
-- Bu SQL'i Supabase Dashboard > SQL Editor'da çalıştırın.

-- 1) orders tablosu (proje kavramı kaldırılıyor)
CREATE TABLE IF NOT EXISTS orders (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  order_id TEXT,
  m2 NUMERIC NOT NULL,
  panel_width NUMERIC NOT NULL,
  panel_length NUMERIC DEFAULT 1,
  il TEXT,
  bitis_tarihi DATE,
  aciklama TEXT,
  status TEXT DEFAULT 'Pending',
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

-- 2) stock_rolls tablosu (set kavramı kaldırılıyor)
CREATE TABLE IF NOT EXISTS stock_rolls (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tonnage NUMERIC NOT NULL,
  source TEXT DEFAULT 'manual',
  run_id UUID REFERENCES optimization_runs(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ DEFAULT now()
);

-- 3) optimization_runs'a processed_at
ALTER TABLE optimization_runs ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ;

-- 4) optimization_runs'a run_status (cancelled desteği)
ALTER TABLE optimization_runs ADD COLUMN IF NOT EXISTS run_status TEXT DEFAULT 'saved';
-- run_status: 'saved' | 'processed' | 'cancelled'

-- 4b) optimization_runs'a kısa açıklama (sonuçlar tablosunda gösterilir)
ALTER TABLE optimization_runs ADD COLUMN IF NOT EXISTS description TEXT;

-- 5) İndeksler
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_stock_rolls_source ON stock_rolls(source);
CREATE INDEX IF NOT EXISTS idx_stock_rolls_created_at ON stock_rolls(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_stock_rolls_run_id ON stock_rolls(run_id);

-- 6) RLS ve policies
ALTER TABLE orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE stock_rolls ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE policyname = 'Allow all for service role'
      AND schemaname = 'public'
      AND tablename = 'orders'
  ) THEN
    CREATE POLICY "Allow all for service role" ON orders
      FOR ALL USING (true) WITH CHECK (true);
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE policyname = 'Allow all for service role'
      AND schemaname = 'public'
      AND tablename = 'stock_rolls'
  ) THEN
    CREATE POLICY "Allow all for service role" ON stock_rolls
      FOR ALL USING (true) WITH CHECK (true);
  END IF;
END $$;

-- 7) Eski tablolar (sıfırdan başlanacak - migration sonrası çalıştırılacak)
-- Dikkat: Bu komutlar mevcut order_sets ve stock_sets verilerini siler!
DROP TABLE IF EXISTS order_sets;
DROP TABLE IF EXISTS stock_sets;

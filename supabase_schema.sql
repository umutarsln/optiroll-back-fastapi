-- OptiRoll Supabase Schema
-- Bu SQL'i Supabase Dashboard > SQL Editor'da çalıştırın.

-- Optimizasyon çalıştırmaları tablosu
CREATE TABLE IF NOT EXISTS optimization_runs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  file_id TEXT NOT NULL UNIQUE,
  configuration_id UUID,
  created_at TIMESTAMPTZ DEFAULT now(),
  status TEXT NOT NULL DEFAULT 'Optimal',
  input_data JSONB NOT NULL,
  summary JSONB NOT NULL,
  cutting_plan JSONB NOT NULL,
  roll_status JSONB NOT NULL,
  report_url TEXT
);

-- Konfigürasyon kayıtları: kullanıcı parametrelerini tekrar düzenleyebilmek için
CREATE TABLE IF NOT EXISTS optimization_configurations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT,
  material_thickness NUMERIC NOT NULL,
  material_density NUMERIC NOT NULL,
  safety_stock NUMERIC NOT NULL,
  max_orders_per_roll INT NOT NULL,
  max_rolls_per_order INT NOT NULL,
  fire_cost NUMERIC NOT NULL,
  setup_cost NUMERIC NOT NULL,
  stock_cost NUMERIC NOT NULL,
  rolls JSONB NOT NULL,
  orders JSONB NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

-- Kayıtlı sipariş setleri: kullanıcı birden fazla siparişi şablon olarak saklayabilir
CREATE TABLE IF NOT EXISTS order_sets (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL,
  orders JSONB NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

-- Kayıtlı stok/rulo setleri: kullanıcı rulo tonaj listelerini şablon olarak saklayabilir
CREATE TABLE IF NOT EXISTS stock_sets (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL,
  rolls JSONB NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

-- Eski tabloda kolon yoksa güvenli şekilde ekle
ALTER TABLE optimization_runs
  ADD COLUMN IF NOT EXISTS configuration_id UUID;

-- optimization_runs -> optimization_configurations ilişkisi (yoksa ekle)
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'optimization_runs_configuration_id_fkey'
  ) THEN
    ALTER TABLE optimization_runs
      ADD CONSTRAINT optimization_runs_configuration_id_fkey
      FOREIGN KEY (configuration_id)
      REFERENCES optimization_configurations(id)
      ON DELETE SET NULL;
  END IF;
END $$;

-- Sonuç detay sayfasındaki KPI/metrikler için normalize tablo
CREATE TABLE IF NOT EXISTS optimization_run_metrics (
  run_id UUID PRIMARY KEY REFERENCES optimization_runs(id) ON DELETE CASCADE,
  file_id TEXT NOT NULL UNIQUE REFERENCES optimization_runs(file_id) ON DELETE CASCADE,
  status TEXT NOT NULL,
  total_cost NUMERIC NOT NULL,
  total_fire_ton NUMERIC NOT NULL,
  total_stock_ton NUMERIC NOT NULL,
  opened_rolls INT NOT NULL,
  total_tonnage NUMERIC NOT NULL,
  total_used_ton NUMERIC NOT NULL,
  material_usage_pct NUMERIC NOT NULL,
  fire_pct NUMERIC NOT NULL,
  total_panels INT NOT NULL,
  total_m2 NUMERIC NOT NULL,
  unique_rolls INT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now()
);

-- Rulo bazlı durumlar (detay tablosu + grafik)
CREATE TABLE IF NOT EXISTS optimization_run_roll_status (
  id BIGSERIAL PRIMARY KEY,
  run_id UUID NOT NULL REFERENCES optimization_runs(id) ON DELETE CASCADE,
  file_id TEXT NOT NULL REFERENCES optimization_runs(file_id) ON DELETE CASCADE,
  roll_id INT NOT NULL,
  total_tonnage NUMERIC NOT NULL,
  used_ton NUMERIC NOT NULL,
  remaining_ton NUMERIC NOT NULL,
  fire_ton NUMERIC NOT NULL,
  stock_ton NUMERIC NOT NULL,
  orders_used INT NOT NULL
);

-- Sipariş-rulo kesim kırılımı (stacked chart ve detay analiz)
CREATE TABLE IF NOT EXISTS optimization_run_cutting_plan (
  id BIGSERIAL PRIMARY KEY,
  run_id UUID NOT NULL REFERENCES optimization_runs(id) ON DELETE CASCADE,
  file_id TEXT NOT NULL REFERENCES optimization_runs(file_id) ON DELETE CASCADE,
  roll_id INT NOT NULL,
  order_id INT NOT NULL,
  panel_count INT NOT NULL,
  panel_width NUMERIC NOT NULL,
  tonnage NUMERIC NOT NULL,
  m2 NUMERIC NOT NULL
);

-- İndeksler (sorgu performansı için)
CREATE INDEX IF NOT EXISTS idx_optimization_runs_file_id ON optimization_runs(file_id);
CREATE INDEX IF NOT EXISTS idx_optimization_runs_created_at ON optimization_runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_optimization_runs_configuration_id ON optimization_runs(configuration_id);
CREATE INDEX IF NOT EXISTS idx_optimization_configurations_updated_at ON optimization_configurations(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_order_sets_updated_at ON order_sets(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_stock_sets_updated_at ON stock_sets(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_run_metrics_file_id ON optimization_run_metrics(file_id);
CREATE INDEX IF NOT EXISTS idx_roll_status_run_id ON optimization_run_roll_status(run_id);
CREATE INDEX IF NOT EXISTS idx_roll_status_file_id ON optimization_run_roll_status(file_id);
CREATE INDEX IF NOT EXISTS idx_cutting_plan_run_id ON optimization_run_cutting_plan(run_id);
CREATE INDEX IF NOT EXISTS idx_cutting_plan_file_id ON optimization_run_cutting_plan(file_id);
CREATE INDEX IF NOT EXISTS idx_cutting_plan_roll_order ON optimization_run_cutting_plan(run_id, roll_id, order_id);

-- RLS (Row Level Security) - isteğe bağlı
ALTER TABLE optimization_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE optimization_configurations ENABLE ROW LEVEL SECURITY;
ALTER TABLE order_sets ENABLE ROW LEVEL SECURITY;
ALTER TABLE stock_sets ENABLE ROW LEVEL SECURITY;
ALTER TABLE optimization_run_metrics ENABLE ROW LEVEL SECURITY;
ALTER TABLE optimization_run_roll_status ENABLE ROW LEVEL SECURITY;
ALTER TABLE optimization_run_cutting_plan ENABLE ROW LEVEL SECURITY;

-- Herkese okuma/yazma (servis key ile backend erişir; production'da kısıtlayabilirsiniz)
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE policyname = 'Allow all for service role'
      AND schemaname = 'public'
      AND tablename = 'optimization_runs'
  ) THEN
    CREATE POLICY "Allow all for service role" ON optimization_runs
      FOR ALL USING (true) WITH CHECK (true);
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE policyname = 'Allow all for service role'
      AND schemaname = 'public'
      AND tablename = 'order_sets'
  ) THEN
    CREATE POLICY "Allow all for service role" ON order_sets
      FOR ALL USING (true) WITH CHECK (true);
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE policyname = 'Allow all for service role'
      AND schemaname = 'public'
      AND tablename = 'stock_sets'
  ) THEN
    CREATE POLICY "Allow all for service role" ON stock_sets
      FOR ALL USING (true) WITH CHECK (true);
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE policyname = 'Allow all for service role'
      AND schemaname = 'public'
      AND tablename = 'optimization_configurations'
  ) THEN
    CREATE POLICY "Allow all for service role" ON optimization_configurations
      FOR ALL USING (true) WITH CHECK (true);
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE policyname = 'Allow all for service role'
      AND schemaname = 'public'
      AND tablename = 'optimization_run_metrics'
  ) THEN
    CREATE POLICY "Allow all for service role" ON optimization_run_metrics
      FOR ALL USING (true) WITH CHECK (true);
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE policyname = 'Allow all for service role'
      AND schemaname = 'public'
      AND tablename = 'optimization_run_roll_status'
  ) THEN
    CREATE POLICY "Allow all for service role" ON optimization_run_roll_status
      FOR ALL USING (true) WITH CHECK (true);
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE policyname = 'Allow all for service role'
      AND schemaname = 'public'
      AND tablename = 'optimization_run_cutting_plan'
  ) THEN
    CREATE POLICY "Allow all for service role" ON optimization_run_cutting_plan
      FOR ALL USING (true) WITH CHECK (true);
  END IF;
END $$;

-- Storage bucket'ı Supabase Dashboard > Storage > New bucket ile oluşturun:
-- - Bucket adı: optimization-reports
-- - Public: Evet (rapor indirme için)

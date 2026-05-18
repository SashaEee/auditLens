-- 004: расширение product_category до полного каталога + поля CBR registry
-- Идемпотентно: ALTER TYPE ... ADD VALUE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS

ALTER TYPE product_category ADD VALUE IF NOT EXISTS 'savings_account';
ALTER TYPE product_category ADD VALUE IF NOT EXISTS 'refinance';
ALTER TYPE product_category ADD VALUE IF NOT EXISTS 'mortgage_refinance';
ALTER TYPE product_category ADD VALUE IF NOT EXISTS 'microloan';
ALTER TYPE product_category ADD VALUE IF NOT EXISTS 'invest_broker';
ALTER TYPE product_category ADD VALUE IF NOT EXISTS 'invest_pif';
ALTER TYPE product_category ADD VALUE IF NOT EXISTS 'npf';
ALTER TYPE product_category ADD VALUE IF NOT EXISTS 'osago';
ALTER TYPE product_category ADD VALUE IF NOT EXISTS 'kasko';
ALTER TYPE product_category ADD VALUE IF NOT EXISTS 'insurance_mortgage';
ALTER TYPE product_category ADD VALUE IF NOT EXISTS 'insurance_travel';
ALTER TYPE product_category ADD VALUE IF NOT EXISTS 'insurance_life';
ALTER TYPE product_category ADD VALUE IF NOT EXISTS 'rko';
ALTER TYPE product_category ADD VALUE IF NOT EXISTS 'business_loan';
ALTER TYPE product_category ADD VALUE IF NOT EXISTS 'leasing';
ALTER TYPE product_category ADD VALUE IF NOT EXISTS 'factoring';
ALTER TYPE product_category ADD VALUE IF NOT EXISTS 'acquiring';
ALTER TYPE product_category ADD VALUE IF NOT EXISTS 'currency_exchange';
ALTER TYPE product_category ADD VALUE IF NOT EXISTS 'bank_rating';

-- Дополнительные поля банка из реестра ЦБ
ALTER TABLE bank ADD COLUMN IF NOT EXISTS cbr_license_no TEXT;
ALTER TABLE bank ADD COLUMN IF NOT EXISTS cbr_reg_no     TEXT;
ALTER TABLE bank ADD COLUMN IF NOT EXISTS cbr_status     TEXT;       -- 'active'|'revoked'|'liquidated'
ALTER TABLE bank ADD COLUMN IF NOT EXISTS region         TEXT;
ALTER TABLE bank ADD COLUMN IF NOT EXISTS website        TEXT;
ALTER TABLE bank ADD COLUMN IF NOT EXISTS assets_rub     NUMERIC(20,2);
ALTER TABLE bank ADD COLUMN IF NOT EXISTS assets_rank    INT;
ALTER TABLE bank ADD COLUMN IF NOT EXISTS sources        JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS bank_cbr_license_idx ON bank(cbr_license_no);
CREATE INDEX IF NOT EXISTS bank_status_idx ON bank(cbr_status);

-- View покрытия: сколько банков из реестра имеют офферы в каждой категории
CREATE OR REPLACE VIEW v_bank_coverage AS
SELECT
  c.cat                               AS category,
  COUNT(DISTINCT b.bank_id)           AS banks_total,
  COUNT(DISTINCT o.bank_id)           AS banks_with_offers,
  ROUND(100.0 * COUNT(DISTINCT o.bank_id)
        / NULLIF(COUNT(DISTINCT b.bank_id),0), 2) AS coverage_pct
FROM (SELECT unnest(enum_range(NULL::product_category)) AS cat) c
CROSS JOIN bank b
LEFT JOIN product_offer o
       ON o.bank_id = b.bank_id
      AND o.category = c.cat
      AND o.is_active
WHERE COALESCE(b.cbr_status,'active') = 'active'
GROUP BY c.cat
ORDER BY c.cat;

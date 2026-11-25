-- docs/contracts/catalog_schema_reference.sql
-- СПЕЦИФИКАЦИЯ целевого состояния PostgreSQL 16, НЕ ИСПОЛНЯТЬ КАК МИГРАЦИЮ!
-- Реализация только через последовательные безопасные миграции Alembic;
-- существующие categories/taxonomy_snapshots/taxonomy_import_runs сохранить без пересоздания.
-- Все TIMESTAMPTZ в UTC на уровне приложения; логические enum через CHECK.

-- ДОПОЛНЕНИЕ существующей таблицы (без изменения UUID, parent_id, imported_values):
ALTER TABLE categories ADD COLUMN is_assignable boolean NOT NULL DEFAULT true;
ALTER TABLE categories ADD COLUMN review_status text NOT NULL DEFAULT 'provisional'
    CHECK (review_status IN ('provisional','approved','deprecated'));
ALTER TABLE categories ADD COLUMN semantic_description text;
ALTER TABLE categories ADD COLUMN semantic_purpose text;
ALTER TABLE categories ADD COLUMN semantic_exclusions text;
-- Существующие категории нельзя объявлять deprecated/неassignable без решения владельца.

CREATE TABLE brands (
    id uuid PRIMARY KEY, canonical_name text NOT NULL CHECK (btrim(canonical_name) <> ''),
    normalized_name text NOT NULL, status text NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('candidate','approved','rejected','merged')),
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_brands_approved_name ON brands (normalized_name) WHERE status='approved';

CREATE TABLE brand_aliases (
    id uuid PRIMARY KEY, brand_id uuid NOT NULL REFERENCES brands(id) ON DELETE RESTRICT,
    alias text NOT NULL, normalized_alias text NOT NULL,
    status text NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('candidate','approved','rejected')),
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    approved_by text, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_brand_alias_approved ON brand_aliases(normalized_alias) WHERE status='approved';

CREATE TABLE manufacturers (
    id uuid PRIMARY KEY, legal_name text NOT NULL, normalized_name text,
    country_code char(2), inn text, website text,
    status text NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('candidate','approved','rejected','merged')),
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_manufacturer_inn ON manufacturers(country_code, inn) WHERE inn IS NOT NULL AND status='approved';

CREATE TABLE sellers (
    id uuid PRIMARY KEY, legal_name text NOT NULL, normalized_name text,
    country_code char(2), inn text, website text,
    status text NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('candidate','approved','rejected','merged')),
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_seller_inn ON sellers(country_code, inn) WHERE inn IS NOT NULL AND status='approved';

CREATE TABLE sources (
    id uuid PRIMARY KEY, name text NOT NULL, kind text NOT NULL
        CHECK (kind IN ('archive','merchant_site','manufacturer_site','price_file','api','other')),
    base_url text, seller_id uuid REFERENCES sellers(id) ON DELETE RESTRICT,
    parent_source_id uuid REFERENCES sources(id) ON DELETE RESTRICT,
    access_policy text NOT NULL DEFAULT 'unverified'
        CHECK (access_policy IN ('unverified','approved','blocked')),
    enabled boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE source_runs (
    id uuid PRIMARY KEY, source_id uuid NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
    input_sha256 char(64) NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
    status text NOT NULL CHECK(status IN ('pending','running','completed','failed','cancelled')),
    counters jsonb NOT NULL DEFAULT '{}'::jsonb,
    errors jsonb NOT NULL DEFAULT '[]'::jsonb,
    CHECK(finished_at IS NULL OR finished_at >= started_at)
);
CREATE INDEX ix_source_runs_source_time ON source_runs(source_id,started_at DESC);

CREATE TABLE raw_records (
    id uuid PRIMARY KEY,
    source_id uuid NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
    first_run_id uuid NOT NULL REFERENCES source_runs(id) ON DELETE RESTRICT,
    source_key text NOT NULL, payload_sha256 char(64) NOT NULL,
    payload jsonb NOT NULL, origin_url text,
    collected_at timestamptz NOT NULL,
    source_reported_date date,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(source_id, source_key, payload_sha256),
    CHECK(btrim(source_key) <> ''),
    CHECK(jsonb_typeof(payload) = 'object')
);
CREATE INDEX ix_raw_records_source_key ON raw_records(source_id,source_key);

CREATE TABLE normalized_records (
    id uuid PRIMARY KEY, raw_record_id uuid NOT NULL REFERENCES raw_records(id) ON DELETE RESTRICT,
    normalizer_version text NOT NULL,
    normalized_name text, brand_candidate text, mpn_candidate text,
    mpn_candidate_origin text CHECK (mpn_candidate_origin IN ('manufacturer','seller','unknown') OR mpn_candidate_origin IS NULL),
    declared_product_type text, unit_candidate text, attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
    issues jsonb NOT NULL DEFAULT '[]'::jsonb,
    status text NOT NULL DEFAULT 'parsed'
        CHECK(status IN ('parsed','invalid','needs_review')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(raw_record_id, normalizer_version)
);

CREATE TABLE products (
    id uuid PRIMARY KEY,
    canonical_name text NOT NULL CHECK(btrim(canonical_name) <> ''),
    brand_id uuid REFERENCES brands(id) ON DELETE RESTRICT,
    manufacturer_id uuid REFERENCES manufacturers(id) ON DELETE RESTRICT,
    category_id uuid REFERENCES categories(id) ON DELETE RESTRICT,
    model text, mpn text, normalized_mpn text,
    product_type text, unit_code text,
    attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'provisional'
        CHECK(status IN ('provisional','identified','needs_review','retired')),
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_products_category ON products(category_id);
CREATE INDEX ix_products_brand_mpn ON products(brand_id,normalized_mpn) WHERE normalized_mpn IS NOT NULL;
CREATE INDEX ix_products_manufacturer_mpn ON products(manufacturer_id,normalized_mpn) WHERE normalized_mpn IS NOT NULL;
-- Уникальность brand+MPN не вводить без оценки вариантов упаковки/серии/issuer.

CREATE TABLE product_identifiers (
    id uuid PRIMARY KEY, product_id uuid NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    identifier_type text NOT NULL CHECK(identifier_type IN ('MPN','GTIN','EAN','SKU','OTHER')),
    issuer text, raw_value text NOT NULL, normalized_value text NOT NULL,
    status text NOT NULL DEFAULT 'candidate'
        CHECK(status IN ('candidate','confirmed','rejected')),
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE NULLS NOT DISTINCT (product_id, identifier_type, issuer, normalized_value)
);
CREATE INDEX ix_product_identifiers_lookup ON product_identifiers(identifier_type,normalized_value);
-- Глобальная уникальность любого SKU запрещена: продавец может переиспользовать SKU.

CREATE TABLE product_understandings (
    id uuid PRIMARY KEY, normalized_record_id uuid NOT NULL REFERENCES normalized_records(id) ON DELETE RESTRICT,
    input_sha256 char(64) NOT NULL, prompt_sha256 char(64) NOT NULL,
    model_logical_id text NOT NULL, model_revision text NOT NULL,
    answer_json jsonb, raw_response_text text, evidence_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
    status text NOT NULL CHECK(status IN ('recognized','insufficient_evidence','invalid_output','unavailable')),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK(status <> 'recognized' OR answer_json IS NOT NULL),
    UNIQUE(normalized_record_id,input_sha256,prompt_sha256,model_logical_id,model_revision)
);

CREATE TABLE product_match_decisions (
    id uuid PRIMARY KEY, normalized_record_id uuid NOT NULL REFERENCES normalized_records(id) ON DELETE RESTRICT,
    product_id uuid REFERENCES products(id) ON DELETE RESTRICT,
    stage text NOT NULL CHECK(stage IN ('fast_identifiers','after_understanding','manual')),
    method text NOT NULL, candidate_product_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL CHECK(status IN ('confirmed','candidate','rejected','unresolved')),
    decided_by text NOT NULL DEFAULT 'system', created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_product_match_record ON product_match_decisions(normalized_record_id,created_at DESC);

CREATE TABLE category_aliases (
    id uuid PRIMARY KEY, category_id uuid NOT NULL REFERENCES categories(id) ON DELETE RESTRICT,
    alias text NOT NULL, normalized_alias text NOT NULL,
    status text NOT NULL DEFAULT 'candidate'
        CHECK(status IN ('candidate','approved','rejected')),
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    approved_by text, updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(category_id,normalized_alias)
);
CREATE INDEX ix_category_alias_category ON category_aliases(category_id);

CREATE TABLE category_source_mappings (
    id uuid PRIMARY KEY, source_id uuid NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
    external_category_key text NOT NULL,
    category_id uuid REFERENCES categories(id) ON DELETE RESTRICT,
    status text NOT NULL DEFAULT 'candidate'
        CHECK(status IN ('candidate','approved','rejected','ambiguous')),
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    approved_by text, updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(source_id,external_category_key)
);

CREATE TABLE taxonomy_gaps (
    id uuid PRIMARY KEY, normalized_record_id uuid NOT NULL REFERENCES normalized_records(id) ON DELETE RESTRICT,
    product_id uuid REFERENCES products(id) ON DELETE RESTRICT,
    proposed_type text, proposed_parent_id uuid REFERENCES categories(id) ON DELETE RESTRICT,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'open'
        CHECK(status IN ('open','resolved','excluded','deferred')),
    resolved_category_id uuid REFERENCES categories(id) ON DELETE RESTRICT,
    resolved_by text, resolved_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE classification_decisions (
    id uuid PRIMARY KEY, normalized_record_id uuid NOT NULL REFERENCES normalized_records(id) ON DELETE RESTRICT,
    product_id uuid REFERENCES products(id) ON DELETE RESTRICT,
    category_id uuid REFERENCES categories(id) ON DELETE RESTRICT,
    method text NOT NULL CHECK(method IN ('existing_product','approved_mapping','approved_rule','retrieval_vlm','manual','none')),
    selection_type text NOT NULL CHECK(selection_type IN ('exact','broader','none')),
    status text NOT NULL CHECK(status IN ('proposed','approved','rejected','needs_review','no_suitable_category','superseded')),
    candidates jsonb NOT NULL DEFAULT '[]'::jsonb,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    prompt_sha256 char(64), model_revision text, embedding_model_revision text,
    taxonomy_revision bigint,
    decided_by text NOT NULL DEFAULT 'system', created_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((status IN ('needs_review','no_suitable_category') AND category_id IS NULL)
       OR status NOT IN ('needs_review','no_suitable_category'))
);
CREATE INDEX ix_classification_record ON classification_decisions(normalized_record_id,created_at DESC);
CREATE INDEX ix_classification_product ON classification_decisions(product_id,created_at DESC);

CREATE TABLE offers (
    id uuid PRIMARY KEY, product_id uuid REFERENCES products(id) ON DELETE RESTRICT,
    seller_id uuid REFERENCES sellers(id) ON DELETE RESTRICT,
    source_id uuid NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
    source_offer_key text NOT NULL, origin_url text,
    sale_unit text, pack_quantity numeric(18,6),
    status text NOT NULL DEFAULT 'unverified'
        CHECK(status IN ('unverified','active','unavailable','retired')),
    last_seen_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(source_id,source_offer_key),
    CHECK(pack_quantity IS NULL OR pack_quantity > 0)
);
CREATE INDEX ix_offers_product_seller ON offers(product_id,seller_id);

CREATE TABLE price_observations (
    id uuid PRIMARY KEY, offer_id uuid NOT NULL REFERENCES offers(id) ON DELETE RESTRICT,
    raw_record_id uuid NOT NULL REFERENCES raw_records(id) ON DELETE RESTRICT,
    raw_price text, amount numeric(20,6), currency char(3), sale_unit text,
    price_type text CHECK(price_type IN ('retail','wholesale','other','unknown') OR price_type IS NULL),
    vat_status text CHECK(vat_status IN ('included','excluded','unknown') OR vat_status IS NULL),
    source_reported_date date, observed_at timestamptz NOT NULL,
    quality_status text NOT NULL CHECK(quality_status IN ('parsed','invalid','unverified','verified')),
    issues jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(offer_id,raw_record_id),
    CHECK(amount IS NULL OR amount >= 0)
);
CREATE INDEX ix_price_observations_offer_time ON price_observations(offer_id,observed_at DESC);

CREATE TABLE price_history (
    id uuid PRIMARY KEY, offer_id uuid NOT NULL REFERENCES offers(id) ON DELETE RESTRICT,
    price_observation_id uuid NOT NULL UNIQUE REFERENCES price_observations(id) ON DELETE RESTRICT,
    amount numeric(20,6) NOT NULL CHECK(amount >= 0), currency char(3) NOT NULL,
    sale_unit text NOT NULL, price_type text NOT NULL,
    vat_status text NOT NULL DEFAULT 'unknown',
    valid_from timestamptz NOT NULL, valid_to timestamptz,
    verified_by text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
    CHECK(valid_to IS NULL OR valid_to > valid_from)
);
CREATE INDEX ix_price_history_offer_since ON price_history(offer_id,valid_from DESC);
-- Историю интервалов не пересекать: дополнительное исключающее ограничение/блокировка UoW.

CREATE TABLE review_tasks (
    id uuid PRIMARY KEY, normalized_record_id uuid REFERENCES normalized_records(id) ON DELETE RESTRICT,
    product_id uuid REFERENCES products(id) ON DELETE RESTRICT,
    kind text NOT NULL CHECK(kind IN ('product_match','category_selection','taxonomy_gap','data_quality','singleton_category')),
    status text NOT NULL DEFAULT 'open'
        CHECK(status IN ('open','approved','rejected','deferred','resolved')),
    context jsonb NOT NULL DEFAULT '{}'::jsonb,
    resolution jsonb, actor text, resolved_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_review_tasks_queue ON review_tasks(status,kind,created_at);

CREATE TABLE category_change_sets (
    id uuid PRIMARY KEY, action text NOT NULL
        CHECK(action IN ('keep','remove_child','remove_parent_reparent','merge','deactivate')),
    category_id uuid NOT NULL REFERENCES categories(id) ON DELETE RESTRICT,
    target_category_id uuid REFERENCES categories(id) ON DELETE RESTRICT,
    preview jsonb NOT NULL DEFAULT '{}'::jsonb,
    reason text NOT NULL, status text NOT NULL DEFAULT 'proposed'
        CHECK(status IN ('proposed','approved','applied','rejected')),
    approved_by text, applied_at timestamptz, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE category_redirects (
    old_category_id uuid PRIMARY KEY,
    new_category_id uuid NOT NULL REFERENCES categories(id) ON DELETE RESTRICT,
    change_set_id uuid NOT NULL REFERENCES category_change_sets(id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK(old_category_id <> new_category_id)
);

-- QDRANT / POSTGRESQL transactional outbox; изменение categories через psql тоже триггерит событие.
CREATE TABLE taxonomy_index_outbox (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entity_kind text NOT NULL CHECK(entity_kind IN ('category','category_alias')),
    entity_id uuid NOT NULL, event_kind text NOT NULL
        CHECK(event_kind IN ('upsert','delete','rebuild_subtree')),
    old_parent_id uuid, new_parent_id uuid,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz, attempts int NOT NULL DEFAULT 0,
    last_error text,
    next_attempt_at timestamptz,
    CHECK(attempts >= 0)
);
CREATE INDEX ix_taxonomy_outbox_pending ON taxonomy_index_outbox(next_attempt_at,id) WHERE processed_at IS NULL;
CREATE TABLE taxonomy_index_state (
    id smallint PRIMARY KEY CHECK(id=1),
    collection_alias text NOT NULL, collection_name text,
    embedding_model_revision text, document_version text,
    embedding_dimensions int NOT NULL CHECK(embedding_dimensions > 0),
    taxonomy_fingerprint char(64), last_successful_reconciliation timestamptz,
    last_indexed_at timestamptz, last_error text,
    status text NOT NULL DEFAULT 'uninitialized'
        CHECK(status IN ('uninitialized','syncing','ready','stale','error'))
);
-- Требуются PostgreSQL-триггеры категорий/алиасов + idempotent worker;
-- триггеры/функции реализует Alembic по ТЗ, а не вручную этим reference SQL.

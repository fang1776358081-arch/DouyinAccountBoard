-- Optional, additive table-sync pilot. Never run automatically at startup.
-- Source files and disallowed/private source columns are never persisted.
CREATE TABLE IF NOT EXISTS table_sync_pilots(
 office_id TEXT PRIMARY KEY REFERENCES offices(id),
 state TEXT NOT NULL CHECK(state IN ('disabled','enabled','paused','ended')),
 revision BIGINT NOT NULL CHECK(revision>0),updated_at BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS table_sync_account_sources(
 account_id TEXT PRIMARY KEY REFERENCES accounts(id),office_id TEXT NOT NULL REFERENCES offices(id),
 responsible TEXT NOT NULL DEFAULT '',usage TEXT NOT NULL DEFAULT '',violation TEXT NOT NULL DEFAULT '',
 business_type TEXT NOT NULL DEFAULT '',storefront TEXT NOT NULL DEFAULT '',source_data TEXT NOT NULL DEFAULT '{}',
 revision BIGINT NOT NULL CHECK(revision>0));
CREATE INDEX IF NOT EXISTS table_sync_sources_office ON table_sync_account_sources(office_id,account_id);
CREATE TABLE IF NOT EXISTS table_sync_batches(
 id TEXT PRIMARY KEY,office_id TEXT NOT NULL REFERENCES offices(id),actor_id BIGINT NOT NULL,
 created_at BIGINT NOT NULL,kind TEXT NOT NULL CHECK(kind IN ('add','update','rollback')),
 reverts_batch_id TEXT REFERENCES table_sync_batches(id),receipt_json TEXT NOT NULL,plan_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS table_sync_batches_office_time ON table_sync_batches(office_id,created_at DESC,id);
CREATE UNIQUE INDEX IF NOT EXISTS table_sync_single_reverse ON table_sync_batches(reverts_batch_id) WHERE reverts_batch_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS table_sync_review_items(
 id TEXT PRIMARY KEY,batch_id TEXT NOT NULL REFERENCES table_sync_batches(id),office_id TEXT NOT NULL REFERENCES offices(id),
 state TEXT NOT NULL CHECK(state IN ('open','confirmed_no_action','corrected')),payload_json TEXT NOT NULL,
 note TEXT NOT NULL DEFAULT '',updated_at BIGINT NOT NULL);
CREATE INDEX IF NOT EXISTS table_sync_review_batch ON table_sync_review_items(batch_id,id);
CREATE TABLE IF NOT EXISTS table_sync_events(
 id TEXT PRIMARY KEY,office_id TEXT NOT NULL REFERENCES offices(id),actor_id BIGINT NOT NULL,
 at BIGINT NOT NULL,kind TEXT NOT NULL,target TEXT NOT NULL,before_json TEXT,after_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS table_sync_requests(
 actor_id BIGINT NOT NULL,request_id TEXT NOT NULL,generation TEXT NOT NULL,fingerprint TEXT NOT NULL,
 result_json TEXT NOT NULL,PRIMARY KEY(actor_id,request_id));

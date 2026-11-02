-- Additive publish-review schema, ordered after employee-card 0005.
-- NOT a startup migration. Apply only through the reviewed maintenance migration process.

CREATE TABLE IF NOT EXISTS publish_review_samples(
 id TEXT PRIMARY KEY,account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
 day TEXT NOT NULL,sampled_at TEXT NOT NULL,total INTEGER,result TEXT NOT NULL CHECK(result IN ('ok','failed')),
 source TEXT NOT NULL,account_identity TEXT NOT NULL,CHECK((result='ok' AND total>=0 AND total<=10000000) OR (result='failed' AND total IS NULL)));
CREATE INDEX IF NOT EXISTS publish_review_samples_account_time ON publish_review_samples(account_id,sampled_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS publish_review_samples_account_day ON publish_review_samples(account_id,day,result,sampled_at DESC,id DESC);
CREATE TABLE IF NOT EXISTS publish_review_baselines(
 account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,day TEXT NOT NULL,
 sample_id TEXT NOT NULL REFERENCES publish_review_samples(id) ON DELETE CASCADE,sealed_at TEXT NOT NULL,
 PRIMARY KEY(account_id,day));
CREATE TABLE IF NOT EXISTS publish_review_tasks(
 account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,day TEXT NOT NULL,
 target INTEGER CHECK(target>=0 AND target<=100000),task_state TEXT NOT NULL CHECK(task_state IN ('active','paused','none')),
 deadline TEXT NOT NULL,computer_id TEXT NOT NULL,computer_name TEXT NOT NULL,operator TEXT NOT NULL,
 office_id TEXT NOT NULL,account_name TEXT NOT NULL,douyin_id TEXT NOT NULL,account_status TEXT NOT NULL,
 revision INTEGER NOT NULL,reason TEXT NOT NULL,updated_at TEXT NOT NULL,
 PRIMARY KEY(account_id,day));
CREATE INDEX IF NOT EXISTS publish_review_tasks_day_scope ON publish_review_tasks(day,office_id,computer_id);
CREATE TABLE IF NOT EXISTS publish_review_requests(
 actor_id BIGINT NOT NULL,request_id TEXT NOT NULL,generation TEXT NOT NULL,fingerprint TEXT NOT NULL,
 result_json TEXT NOT NULL,PRIMARY KEY(actor_id,request_id));

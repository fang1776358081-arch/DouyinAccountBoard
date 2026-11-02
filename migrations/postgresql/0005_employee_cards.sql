-- Additive employee card records. Never infer real-name ownership from legacy text.
CREATE TABLE IF NOT EXISTS employee_card_employees(
 id TEXT PRIMARY KEY, employee_no TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
 office_id TEXT NOT NULL DEFAULT '', auth_user_id BIGINT UNIQUE REFERENCES auth_users(id),
 birth_date TEXT NOT NULL DEFAULT '', employment_status TEXT NOT NULL DEFAULT 'unknown'
 CHECK(employment_status IN ('active','left','unknown')),
 consent_source TEXT NOT NULL DEFAULT '', consent_on TEXT NOT NULL DEFAULT '',
 revision BIGINT NOT NULL DEFAULT 0, created_at BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS employee_card_operators(
 id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, payload TEXT NOT NULL,
 revision BIGINT NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS employee_card_rules(
 id TEXT PRIMARY KEY, payload TEXT NOT NULL, revision BIGINT NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS employee_card_applications(
 id TEXT PRIMARY KEY, employee_id TEXT NOT NULL REFERENCES employee_card_employees(id),
 operator_id TEXT NOT NULL REFERENCES employee_card_operators(id), applied_on TEXT NOT NULL,
 result TEXT NOT NULL, quantity BIGINT NOT NULL CHECK(quantity>0), payload TEXT NOT NULL,
 revision BIGINT NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS employee_card_application_employee ON employee_card_applications(employee_id,applied_on,id);
CREATE TABLE IF NOT EXISTS employee_card_units(
 id TEXT PRIMARY KEY, employee_id TEXT NOT NULL REFERENCES employee_card_employees(id),
 operator_id TEXT NOT NULL REFERENCES employee_card_operators(id),
 application_id TEXT REFERENCES employee_card_applications(id),
 phase TEXT NOT NULL CHECK(phase IN ('pending','active','released','failed','cancelled')),
 requested_on TEXT NOT NULL DEFAULT '', activated_on TEXT NOT NULL DEFAULT '',
 release_confirmed_on TEXT NOT NULL DEFAULT '', release_source TEXT NOT NULL DEFAULT '',
 release_policy TEXT NOT NULL DEFAULT '',
 network_policy TEXT NOT NULL DEFAULT '',
 cancelled_on TEXT NOT NULL DEFAULT '', cancel_source TEXT NOT NULL DEFAULT '',
 current_card_id BIGINT REFERENCES phone_cards(id) ON DELETE SET NULL,
 source TEXT NOT NULL DEFAULT '', revision BIGINT NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS employee_card_unit_employee ON employee_card_units(employee_id,operator_id,phase,id);
CREATE TABLE IF NOT EXISTS employee_card_links(
 id TEXT PRIMARY KEY, unit_id TEXT NOT NULL REFERENCES employee_card_units(id),
 card_id BIGINT UNIQUE REFERENCES phone_cards(id) ON DELETE SET NULL,
 phone_snapshot TEXT NOT NULL UNIQUE, source TEXT NOT NULL, linked_on TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS employee_card_link_unit ON employee_card_links(unit_id,id);
CREATE TABLE IF NOT EXISTS employee_card_verifications(
 id TEXT PRIMARY KEY, employee_id TEXT NOT NULL REFERENCES employee_card_employees(id),
 payload TEXT NOT NULL, created_at BIGINT NOT NULL, revision BIGINT NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS employee_card_verification_employee ON employee_card_verifications(employee_id,created_at,id);
CREATE TABLE IF NOT EXISTS employee_card_events(
 id TEXT PRIMARY KEY, employee_id TEXT NOT NULL DEFAULT '', at BIGINT NOT NULL,
 actor_id BIGINT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS employee_card_event_employee ON employee_card_events(employee_id,at,id);
CREATE TABLE IF NOT EXISTS employee_card_requests(
 actor_id BIGINT NOT NULL, request_id TEXT NOT NULL, restore_generation TEXT NOT NULL,
 fingerprint TEXT NOT NULL,
 employee_id TEXT NOT NULL DEFAULT '', action TEXT NOT NULL, result TEXT NOT NULL,
 created_at BIGINT NOT NULL, PRIMARY KEY(actor_id,request_id));

-- Configuration only, based on user-provided current business rules.
INSERT INTO employee_card_operators(id,name,payload) VALUES('minsheng','民生','{"age_confirmed":true,"channel_id":"","effective_from":"2026-11-02","id":"minsheng","min_age":21,"name":"民生","network":"unknown","occupied_phases":["active"],"release_delay_days":null,"release_mode":"unknown","source":"用户提供的当前业务规则（2026-11-02），未独立核验运营商正式条款","verification_valid_days":null,"waiting_basis":"","waiting_confirmed":false,"waiting_days":null}') ON CONFLICT(id) DO NOTHING;
INSERT INTO employee_card_operators(id,name,payload) VALUES('zhongxing','中兴','{"age_confirmed":false,"channel_id":"","effective_from":"2026-11-02","id":"zhongxing","min_age":null,"name":"中兴","network":"unknown","occupied_phases":["active"],"release_delay_days":null,"release_mode":"unknown","source":"用户提供的当前业务规则（2026-11-02），未独立核验运营商正式条款","verification_valid_days":null,"waiting_basis":"","waiting_confirmed":false,"waiting_days":null}') ON CONFLICT(id) DO NOTHING;
INSERT INTO employee_card_operators(id,name,payload) VALUES('changcheng','长城','{"age_confirmed":false,"channel_id":"","effective_from":"2026-11-02","id":"changcheng","min_age":null,"name":"长城","network":"unknown","occupied_phases":["active"],"release_delay_days":null,"release_mode":"unknown","source":"用户提供的当前业务规则（2026-11-02），未独立核验运营商正式条款","verification_valid_days":null,"waiting_basis":"","waiting_confirmed":false,"waiting_days":null}') ON CONFLICT(id) DO NOTHING;
INSERT INTO employee_card_operators(id,name,payload) VALUES('liantong','联通','{"age_confirmed":false,"channel_id":"","effective_from":"2026-11-02","id":"liantong","min_age":null,"name":"联通","network":"unknown","occupied_phases":["active"],"release_delay_days":null,"release_mode":"unknown","source":"用户提供的当前业务规则（2026-11-02），未独立核验运营商正式条款","verification_valid_days":null,"waiting_basis":"","waiting_confirmed":false,"waiting_days":null}') ON CONFLICT(id) DO NOTHING;
INSERT INTO employee_card_operators(id,name,payload) VALUES('dianxin','电信','{"age_confirmed":false,"channel_id":"","effective_from":"2026-11-02","id":"dianxin","min_age":null,"name":"电信","network":"unknown","occupied_phases":["active"],"release_delay_days":null,"release_mode":"unknown","source":"用户提供的当前业务规则（2026-11-02），未独立核验运营商正式条款","verification_valid_days":null,"waiting_basis":"","waiting_confirmed":false,"waiting_days":null}') ON CONFLICT(id) DO NOTHING;
INSERT INTO employee_card_operators(id,name,payload) VALUES('yidong','移动','{"age_confirmed":false,"channel_id":"","effective_from":"2026-11-02","id":"yidong","min_age":null,"name":"移动","network":"unknown","occupied_phases":["active"],"release_delay_days":null,"release_mode":"unknown","source":"用户提供的当前业务规则（2026-11-02），未独立核验运营商正式条款","verification_valid_days":null,"waiting_basis":"","waiting_confirmed":false,"waiting_days":null}') ON CONFLICT(id) DO NOTHING;
INSERT INTO employee_card_rules(id,payload) VALUES('global-stock','{"confirmed":true,"effective_from":"2026-11-02","id":"global-stock","limit":10,"pending_counts":null,"period":"stock","scope":"all","scope_id":"","source":"用户提供的当前业务规则（2026-11-02），未独立核验运营商正式条款"}') ON CONFLICT(id) DO NOTHING;
INSERT INTO employee_card_rules(id,payload) VALUES('unicom-month','{"confirmed":true,"effective_from":"2026-11-02","id":"unicom-month","limit":3,"pending_counts":null,"period":"month","scope":"network","scope_id":"unicom","source":"用户提供的当前业务规则（2026-11-02），未独立核验运营商正式条款"}') ON CONFLICT(id) DO NOTHING;
INSERT INTO employee_card_rules(id,payload) VALUES('minsheng-day','{"confirmed":true,"effective_from":"2026-11-02","id":"minsheng-day","limit":2,"pending_counts":null,"period":"day","scope":"operator","scope_id":"minsheng","source":"用户提供的当前业务规则（2026-11-02），未独立核验运营商正式条款"}') ON CONFLICT(id) DO NOTHING;
INSERT INTO employee_card_rules(id,payload) VALUES('zhongxing-day','{"confirmed":true,"effective_from":"2026-11-02","id":"zhongxing-day","limit":2,"pending_counts":null,"period":"day","scope":"operator","scope_id":"zhongxing","source":"用户提供的当前业务规则（2026-11-02），未独立核验运营商正式条款"}') ON CONFLICT(id) DO NOTHING;
INSERT INTO employee_card_rules(id,payload) VALUES('changcheng-day','{"confirmed":true,"effective_from":"2026-11-02","id":"changcheng-day","limit":2,"pending_counts":null,"period":"day","scope":"operator","scope_id":"changcheng","source":"用户提供的当前业务规则（2026-11-02），未独立核验运营商正式条款"}') ON CONFLICT(id) DO NOTHING;
INSERT INTO employee_card_rules(id,payload) VALUES('liantong-day','{"confirmed":true,"effective_from":"2026-11-02","id":"liantong-day","limit":2,"pending_counts":null,"period":"day","scope":"operator","scope_id":"liantong","source":"用户提供的当前业务规则（2026-11-02），未独立核验运营商正式条款"}') ON CONFLICT(id) DO NOTHING;
INSERT INTO employee_card_rules(id,payload) VALUES('dianxin-day','{"confirmed":true,"effective_from":"2026-11-02","id":"dianxin-day","limit":2,"pending_counts":null,"period":"day","scope":"operator","scope_id":"dianxin","source":"用户提供的当前业务规则（2026-11-02），未独立核验运营商正式条款"}') ON CONFLICT(id) DO NOTHING;
INSERT INTO employee_card_rules(id,payload) VALUES('yidong-day','{"confirmed":true,"effective_from":"2026-11-02","id":"yidong-day","limit":2,"pending_counts":null,"period":"day","scope":"operator","scope_id":"yidong","source":"用户提供的当前业务规则（2026-11-02），未独立核验运营商正式条款"}') ON CONFLICT(id) DO NOTHING;

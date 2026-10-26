-- Explicit migration only. Do not invoke from website startup or deployment sync.
-- Visible device numbers are reusable; physical identities and login history are not.
CREATE TABLE phone_units(
  id TEXT PRIMARY KEY,device_id TEXT NOT NULL REFERENCES phone_devices(id),
  original_device_id TEXT NOT NULL REFERENCES phone_devices(id),
  generation BIGINT NOT NULL CHECK(generation>0),active BIGINT NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
  status TEXT NOT NULL DEFAULT '待确认',model TEXT NOT NULL DEFAULT '',note TEXT NOT NULL DEFAULT '',
  created_at BIGINT NOT NULL,revision BIGINT NOT NULL DEFAULT 0,UNIQUE(device_id,generation));
CREATE UNIQUE INDEX phone_unit_current ON phone_units(device_id) WHERE active=1;
CREATE TABLE device_cases(
  id TEXT PRIMARY KEY,unit_id TEXT NOT NULL REFERENCES phone_units(id),status TEXT NOT NULL DEFAULT '待处理',
  reason TEXT NOT NULL,owner TEXT NOT NULL DEFAULT '',followup_on TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',closure_reason TEXT NOT NULL DEFAULT '',created_at BIGINT NOT NULL,
  updated_at BIGINT NOT NULL,closed_at BIGINT NOT NULL DEFAULT 0,revision BIGINT NOT NULL DEFAULT 0,
  account_ids TEXT NOT NULL DEFAULT '[]');
CREATE INDEX device_case_unit ON device_cases(unit_id,status);
CREATE TABLE phone_card_placements(
  card_id BIGINT PRIMARY KEY REFERENCES phone_cards(id),unit_id TEXT REFERENCES phone_units(id),
  slot TEXT NOT NULL DEFAULT '',storage TEXT NOT NULL DEFAULT '',updated_at BIGINT NOT NULL,
  revision BIGINT NOT NULL DEFAULT 0,
  CHECK((unit_id IS NOT NULL AND slot IN ('1','2','待确认') AND storage='') OR
        (unit_id IS NULL AND slot='' AND storage<>'')));
CREATE UNIQUE INDEX phone_unit_card_slot ON phone_card_placements(unit_id,slot) WHERE unit_id IS NOT NULL AND slot IN ('1','2');
CREATE TABLE phone_account_access(
  account_id TEXT PRIMARY KEY REFERENCES accounts(id),last_unit_id TEXT REFERENCES phone_units(id),
  target_unit_id TEXT REFERENCES phone_units(id),login_state TEXT NOT NULL DEFAULT '待确认',
  recovery_state TEXT NOT NULL DEFAULT '待处理',reason TEXT NOT NULL DEFAULT '',owner TEXT NOT NULL DEFAULT '',
  followup_on TEXT NOT NULL DEFAULT '',case_id TEXT REFERENCES device_cases(id),
  confirmed_at BIGINT NOT NULL DEFAULT 0,revision BIGINT NOT NULL DEFAULT 0);
CREATE TABLE device_workflow_events(
  id TEXT PRIMARY KEY,case_id TEXT REFERENCES device_cases(id),unit_id TEXT REFERENCES phone_units(id),
  card_id BIGINT REFERENCES phone_cards(id),account_id TEXT REFERENCES accounts(id),
  actor_id BIGINT,actor_name TEXT NOT NULL,at BIGINT NOT NULL,action TEXT NOT NULL,detail TEXT NOT NULL);
CREATE INDEX device_event_unit ON device_workflow_events(unit_id,at,id);
CREATE INDEX device_event_case ON device_workflow_events(case_id,at,id);

-- Existing explicit phone-number assignments supply only a last-known identity.
-- They are never evidence of successful current login; card free text is untouched.
INSERT INTO phone_units(id,device_id,original_device_id,generation,status,note,created_at)
SELECT 'legacy-unit:'||id,id,id,1,state,'由原设备编号建立，实物资料待核对',EXTRACT(EPOCH FROM CURRENT_TIMESTAMP)::BIGINT
FROM phone_devices;
INSERT INTO phone_account_access(account_id,last_unit_id)
SELECT a.id,u.id FROM accounts a JOIN phone_units u ON u.device_id=a.phone_device_id AND u.active=1;

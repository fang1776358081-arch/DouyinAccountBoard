-- For a disposable loopback database only. Apply the legacy repository's
-- PostgreSQL migrations 0001–0007 to board_candidate first and record those
-- versions in board_candidate.schema_migrations. The fixture operator must
-- separately create board_app.environment(kind text) with exactly one row
-- whose kind is 'synthetic-only'. Use database synthetic_probe and role
-- synthetic_owner for the Go test. Start with empty accounts and auth_users.
-- Run with psql -X -v ON_ERROR_STOP=1 -f internal/daily/testdata/fixture.sql.
BEGIN;
SET LOCAL search_path = board_candidate, pg_catalog;

DO $fixture$
BEGIN
    IF left(current_database(), 10) <> 'synthetic_' OR
       (SELECT array_agg(kind) FROM board_app.environment) IS DISTINCT FROM ARRAY['synthetic-only']::text[] THEN
        RAISE EXCEPTION 'An isolated synthetic database is required';
    END IF;
    IF (SELECT array_agg(version ORDER BY version) FROM schema_migrations)
        IS DISTINCT FROM ARRAY[1,2,3,4,5,6,7]::bigint[] THEN
        RAISE EXCEPTION 'Expected migrations 0001–0007';
    END IF;
    IF EXISTS (SELECT 1 FROM accounts) OR EXISTS (SELECT 1 FROM auth_users) THEN
        RAISE EXCEPTION 'The synthetic accounts and users must start empty';
    END IF;
END;
$fixture$;

UPDATE runtime_control
SET mode = 'open', writer_epoch = 17, restore_generation = 'go-daily-synthetic-generation'
WHERE id = 1;

INSERT INTO collection_settings(id,auto_enabled,paused) VALUES (1,0,1);
INSERT INTO collection_worker(id) VALUES (1);
INSERT INTO offices(id,name,active) VALUES
    ('go-east','Synthetic East',1),
    ('go-west','Synthetic West',1),
    ('go-disabled','Synthetic Disabled',0);

-- No usable login password. Only generated test-cookie hashes are seeded.
INSERT INTO auth_users(id,username,name,role,permissions,password_hash,created_at) VALUES
    (9101,'go-daily-owner','Synthetic Owner','owner','[]','test-only',0),
    (9102,'go-daily-employee','Synthetic Employee','employee','["publish"]','test-only',0),
    (9103,'go-daily-viewer','Synthetic Viewer','viewer','[]','test-only',0);
INSERT INTO user_office_grants(user_id,office_id) VALUES
    (9102,'go-east'),(9102,'go-disabled'),(9103,'go-east');

INSERT INTO auth_sessions(token_hash,user_id,csrf,expires_at)
SELECT encode(sha256(convert_to('synthetic-go-daily-' || role || '-cookie','UTF8')),'hex'),
       id,'synthetic-go-daily-' || role || '-csrf',extract(epoch from now())::bigint+3600
FROM auth_users;

INSERT INTO computers(id,name) VALUES ('go-east-pc','Synthetic East PC'),('go-west-pc','Synthetic West PC');
INSERT INTO computer_offices(computer_id,office_id) VALUES ('go-east-pc','go-east'),('go-west-pc','go-west');
INSERT INTO accounts(id,computer_id,slot,name,status,douyin_id,note) VALUES
    ('go-east-account','go-east-pc',1,'合成账号','正常','0042','Latin <>& and '||chr(8232)||' U+2028 and '||chr(8233)||' U+2029'),
    ('go-west-account','go-west-pc',1,'West Account','正常','0099',''),
    ('go-east-missing','go-east-pc',2,'Absent Flag','正常','0010',''),
    ('go-unassigned',NULL,NULL,'Unassigned','正常','0033',''),
    ('go-inactive-office',NULL,NULL,'Inactive Office','正常','0023','');
INSERT INTO account_offices(account_id,office_id) VALUES
    ('go-east-account','go-east'),('go-west-account','go-west'),
    ('go-east-missing','go-east'),('go-inactive-office','go-disabled');
INSERT INTO daily(account_id,day,published) VALUES
    ('go-east-account','2026-11-09',0),('go-west-account','2026-11-09',1),('go-east-account','2026-11-08',1);

COMMIT;

-- Apply to a disposable copy of internal/daily/testdata/fixture.sql only.
BEGIN;
SET LOCAL search_path = board_candidate, pg_catalog;
DO $check$
BEGIN
    IF left(current_database(), 24) <> 'synthetic_go_standalone_' OR
       (SELECT array_agg(kind) FROM board_app.environment) IS DISTINCT FROM ARRAY['synthetic-only']::text[] THEN
        RAISE EXCEPTION 'This setup requires a disposable standalone fixture';
    END IF;
END
$check$;
INSERT INTO offices(id,name) VALUES ('three','Synthetic Office Three') ON CONFLICT DO NOTHING;
INSERT INTO computers(id,name) VALUES ('unassigned','Synthetic Unassigned'),('office3:71','Synthetic Office Three PC') ON CONFLICT DO NOTHING;
INSERT INTO computer_offices(computer_id,office_id) VALUES ('office3:71','three') ON CONFLICT DO NOTHING;
INSERT INTO accounts(id,computer_id,slot,name,status,douyin_id)
VALUES ('go-three-existing','office3:71',1,'昵称待补充','正常','006767') ON CONFLICT DO NOTHING;
INSERT INTO account_offices(account_id,office_id) VALUES ('go-three-existing','three') ON CONFLICT DO NOTHING;
INSERT INTO publishing_layout_state(id,batch) VALUES (1,'synthetic-go-layout') ON CONFLICT DO NOTHING;
INSERT INTO publishing_layout(batch,computer_id,slot,account_id,label,office,source_cell,source_url,reason) VALUES
    ('synthetic-go-layout','go-east-pc',1,NULL,'合成待关联','go-east','E1','','未匹配'),
    ('synthetic-go-layout','go-east-pc',2,NULL,'','go-east','E2','',''),
    ('synthetic-go-layout','go-east-pc',3,NULL,'另一个待关联','go-east','E3','','未匹配'),
    ('synthetic-go-layout','go-west-pc',1,'go-west-account','另一办公室','go-west','W1','','')
ON CONFLICT DO NOTHING;
UPDATE auth_users SET permissions='["publish","account_manage","account_create","account_status","account_progress","device_manage","device_create","card_manage","card_create","card_tasks","publishing_manage","data_import","data_export","history_view"]' WHERE id=9102;
COMMIT;

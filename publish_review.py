"""Scoped daily net-growth review. Reads never create tasks, samples or baselines.

Counts are cumulative public totals, not a count of published videos. Historical
ownership and targets come only from explicit dated plans, never today's layout.
"""
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import re
import uuid

import auth
import office_scope
import operations

BEIJING = timezone(timedelta(hours=8), 'Asia/Shanghai')
TABLES = ('publish_review_samples', 'publish_review_baselines', 'publish_review_tasks', 'publish_review_requests')
DDL = '''
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
 actor_id INTEGER NOT NULL,request_id TEXT NOT NULL,generation TEXT NOT NULL,fingerprint TEXT NOT NULL,
 result_json TEXT NOT NULL,PRIMARY KEY(actor_id,request_id));
'''


def now():
    return datetime.now(BEIJING)


def stamp(value=None):
    return (value or now()).astimezone(BEIJING).isoformat(timespec='microseconds')


def business_day(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('业务日期格式不正确')
    date.fromisoformat(value)
    return value


def available(db):
    if getattr(db, 'dialect', 'sqlite') == 'postgresql':
        import pg_schema
        expected = next((item for item in pg_schema.expected_versions() if item[0] == 6), None)
        row = db.execute('SELECT version,sha256 FROM schema_migrations WHERE version=6').fetchone()
        if not row or (row[0], row[1]) != expected:
            return False
        row = db.execute('SELECT '+','.join('to_regclass(?)' for _ in TABLES), TABLES).fetchone()
        return bool(row) and all(row[index] is not None for index in range(len(TABLES)))
    names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'publish_review_%'")}
    return set(TABLES) <= names


def migrate(db):
    if getattr(db, 'dialect', 'sqlite') != 'sqlite':
        raise ValueError('发布核对结构须通过受控PostgreSQL升级创建')
    db.executescript(DDL)


def current_actor(db, actor):
    auth.require(actor)
    if not getattr(db, 'scope_enabled', False):return actor
    refreshed=office_scope.current_user(db, actor)
    if 'csrf' in actor:refreshed['csrf']=actor['csrf']
    return refreshed


def authorize(db, actor, body, permission):
    actor = current_actor(db, actor)
    auth.require(actor, permission)
    ident = body.get('account_id')
    if not isinstance(ident, str) or not ident or len(ident) > 200:
        raise ValueError('请指定稳定的系统账号ID')
    if getattr(db, 'scope_enabled', False):
        office_scope.require_entity(db, actor, 'accounts', ident, write=True)
    if not db.execute('SELECT 1 FROM accounts WHERE id=?', (ident,)).fetchone():
        raise ValueError('账号不存在')
    return actor


def ingest(db, account_id, sample_id, sampled_at, total=None, *, failed=False, source='external'):
    """Called inside an admitted writer transaction after identity validation.

    One immutable observation, not an increment. The first attempt of a new day
    seals yesterday's last successful observation; late imports never rewrite it.
    """
    office_scope.admitted(db)
    if not isinstance(sampled_at, str):
        raise ValueError('缺少实际采样时间')
    try:
        instant = datetime.fromisoformat(sampled_at)
        if instant.tzinfo is None:
            raise ValueError()
    except ValueError:
        raise ValueError('采样时间须包含时区，不能用上传时间代替') from None
    if instant > now():
        raise ValueError('采样时间不能在未来')
    if type(failed) is not bool or (not failed and (type(total) is not int or not 0 <= total <= 10000000)):
        raise ValueError('成功采样须提供有效的累计作品总数')
    if not isinstance(sample_id, str) or not 1 <= len(sample_id) <= 200 or source not in ('external', 'collection'):
        raise ValueError('采样来源标识不正确')
    instant = instant.astimezone(BEIJING)
    day = instant.date().isoformat()
    identity=db.execute('SELECT douyin_id FROM accounts WHERE id=?',(account_id,)).fetchone()
    if not identity:raise ValueError('账号不存在')
    values = (sample_id, account_id, day, stamp(instant), None if failed else total, 'failed' if failed else 'ok', source, identity[0] or '')
    old = db.execute('SELECT id,account_id,day,sampled_at,total,result,source,account_identity FROM publish_review_samples WHERE id=?', (sample_id,)).fetchone()
    if old:
        if tuple(old[key] for key in ('id','account_id','day','sampled_at','total','result','source','account_identity')) != values:
            raise operations.ConflictError('同一采样标识的内容不同，未覆盖原采样')
        return dict(ok=True, duplicate=True)
    db.execute('INSERT INTO publish_review_samples(id,account_id,day,sampled_at,total,result,source,account_identity) VALUES(?,?,?,?,?,?,?,?)', values)
    previous = (instant.date()-timedelta(days=1)).isoformat()
    # No missing-date interpolation and no accounts.work_count backfill.
    prior = db.execute("SELECT id FROM publish_review_samples WHERE account_id=? AND day=? AND result='ok' ORDER BY sampled_at DESC,id DESC LIMIT 1", (account_id, previous)).fetchone()
    if prior:
        db.execute('INSERT INTO publish_review_baselines(account_id,day,sample_id,sealed_at) VALUES(?,?,?,?) ON CONFLICT(account_id,day) DO NOTHING', (account_id, day, prior[0], stamp()))
    return dict(ok=True, duplicate=False)


def save_task(db, actor, body):
    office_scope.admitted(db)
    actor = authorize(db, actor, body, 'publishing_manage')
    day = business_day(body.get('day'))
    if day < now().date().isoformat():
        raise ValueError('历史日期的目标和任务归属已冻结，不能直接改写')
    state = body.get('task_state')
    target = body.get('target')
    if state not in ('active', 'paused', 'none') or target is not None and (type(target) is not int or not 0 <= target <= 100000):
        raise ValueError('任务状态或目标不正确；未设置目标请留空')
    reason = body.get('reason', '')
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
        raise ValueError('请填写设置或修改目标的依据')
    deadline = body.get('deadline', '')
    if deadline and (not isinstance(deadline, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', deadline)):
        raise ValueError('截止时间须为北京时间HH:MM，未知可留空')
    deadline = day+'T'+deadline+':00.000000+08:00' if deadline else ''
    account = dict(db.execute('SELECT id,name,douyin_id,status,computer_id FROM accounts WHERE id=?', (body['account_id'],)).fetchone())
    computer_id = body.get('computer_id', account['computer_id'] or '')
    if not isinstance(computer_id, str):
        raise ValueError('任务电脑不正确')
    computer = db.execute('SELECT id,name,operator FROM computers WHERE id=?', (computer_id,)).fetchone() if computer_id else dict(name='任务电脑待核对',operator='')
    if computer_id and not computer:
        raise ValueError('请选择当日任务的实际电脑')
    office_id = ''
    if getattr(db, 'scope_enabled', False):
        account_scope = office_scope.entity_offices(db, 'accounts', account['id'])
        computer_scope = office_scope.require_entity(db, actor, 'computers', computer_id, write=True) if computer_id else account_scope
        if account_scope != computer_scope or len(account_scope) > 1:
            raise ValueError('账号与任务电脑的明确办公室须一致，请先核对归属')
        office_id = next(iter(account_scope), '')
    revision = body.get('expected_revision')
    if type(revision) is not int or revision < 0:
        raise ValueError('缺少当前任务版本，请刷新后重试')
    old = db.execute('SELECT * FROM publish_review_tasks WHERE account_id=? AND day=?', (account['id'], day)).fetchone()
    if old and getattr(db,'scope_enabled',False) and not office_scope.global_scope(actor):
        if not old['office_id'] or old['office_id'] not in office_scope._granted_offices(db,actor):
            raise auth.AccessError('无权修改任务原办公室的计划',403)
    if (old['revision'] if old else 0) != revision:
        raise operations.ConflictError('目标已被其他操作修改，请刷新后核对；未覆盖')
    fields = (target, state, deadline, computer_id, computer['name'] or '', computer['operator'] or '', office_id,
              account['name'] or '', account['douyin_id'] or '', account['status'] or '', revision+1, reason.strip(), stamp())
    if old:
        db.execute('UPDATE publish_review_tasks SET target=?,task_state=?,deadline=?,computer_id=?,computer_name=?,operator=?,office_id=?,account_name=?,douyin_id=?,account_status=?,revision=?,reason=?,updated_at=? WHERE account_id=? AND day=?', (*fields, account['id'], day))
    else:
        db.execute('INSERT INTO publish_review_tasks(target,task_state,deadline,computer_id,computer_name,operator,office_id,account_name,douyin_id,account_status,revision,reason,updated_at,account_id,day) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (*fields, account['id'], day))
    auth.audit(db, actor, '设置发布核对任务', json.dumps(dict(account_id=account['id'], day=day, before_target=old['target'] if old else None, target=target, task_state=state, reason=reason.strip()), ensure_ascii=False))
    return dict(ok=True, revision=revision+1)


def post(db, actor, path, body):
    office_scope.admitted(db)
    permission = 'publishing_manage' if path.endswith('/task') else 'collection_manage'
    actor = authorize(db, actor, body, permission)
    request_id = str(body.get('request_id', ''))
    try:
        if str(uuid.UUID(request_id)) != request_id:
            raise ValueError()
    except ValueError:
        raise ValueError('请求标识不正确，请刷新后重试') from None
    digest = hashlib.sha256(json.dumps(dict(path=path, body=body), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    generation = str(getattr(db, 'restore_generation', 'sqlite'))
    old = db.execute('SELECT generation,fingerprint,result_json FROM publish_review_requests WHERE actor_id=? AND request_id=?', (actor['id'], request_id)).fetchone()
    if old:
        if old['generation'] != generation or old['fingerprint'] != digest:
            raise operations.ConflictError('请求标识已用于其他内容或恢复代次，请先核对结果')
        return json.loads(old['result_json'])
    if path.endswith('/task'):
        result = save_task(db, actor, body)
    elif path.endswith('/sample'):
        # This is an authenticated data-ingestion interface, never a collector.
        identity=db.execute('SELECT douyin_id FROM accounts WHERE id=?',(body['account_id'],)).fetchone()[0]
        if identity and not body.get('failed') and body.get('matched_douyin_id')!=identity:
            raise ValueError('采样结果与账号当前真实抖音号不一致，未写入')
        result = ingest(db, body['account_id'], 'external:'+request_id, body.get('sampled_at'), body.get('total'), failed=body.get('failed', False))
    else:
        raise ValueError('发布核对接口不存在')
    db.execute('INSERT INTO publish_review_requests(actor_id,request_id,generation,fingerprint,result_json) VALUES(?,?,?,?,?)', (actor['id'], request_id, generation, digest, json.dumps(result)))
    return result


def _scope(db, actor):
    if not getattr(db, 'scope_enabled', False) or office_scope.global_scope(actor):
        return '1=1', []
    # Both today's entity access and the dated assignment must be authorized.
    return '''EXISTS(SELECT 1 FROM account_offices ao JOIN user_office_grants g ON g.office_id=ao.office_id
       JOIN offices o ON o.id=g.office_id WHERE ao.account_id=a.id AND g.user_id=? AND o.active=1)
       AND (t.account_id IS NULL OR EXISTS(SELECT 1 FROM user_office_grants hg JOIN offices ho ON ho.id=hg.office_id
          WHERE hg.office_id=t.office_id AND hg.user_id=? AND ho.active=1))''', [actor['id'], actor['id']]


def _cte(db, actor, day, office):
    current = now().date().isoformat()
    scope, parameters = _scope(db, actor)
    computer_guard=''
    computer_parameters=[]
    if getattr(db,'scope_enabled',False) and not office_scope.global_scope(actor):
        computer_guard=''' AND EXISTS(SELECT 1 FROM computer_offices co JOIN user_office_grants cg ON cg.office_id=co.office_id
          JOIN offices oc ON oc.id=cg.office_id WHERE co.computer_id=c.id AND cg.user_id=? AND oc.active=1)'''
        computer_parameters=[actor['id']]
    office_expr = "COALESCE(t.office_id,(SELECT office_id FROM account_offices WHERE account_id=a.id),'')" if getattr(db, 'scope_enabled', False) else "''"
    office_where = ''
    if office:
        office_where = ' AND '+office_expr+'=?'
        parameters.append(office)
    # Indexed per-account latest observations; never load histories or /api/board.
    display_name="COALESCE(a.name,'')" if day==current else "COALESCE(t.account_name,'')"
    display_identity="COALESCE(a.douyin_id,'')" if day==current else "COALESCE(t.douyin_id,'')"
    sql = '''WITH raw AS (
      SELECT a.id AS account_id,'''+display_name+' AS name,'+display_identity+''' AS douyin_id,
       COALESCE(t.computer_id,c.id,'') AS computer_id,COALESCE(t.computer_name,c.name,'未分配电脑') AS computer_name,
       COALESCE(t.operator,c.operator,'') AS operator,t.task_state,t.target,t.deadline,COALESCE(t.revision,0) AS revision,
       COALESCE(t.account_status,a.status,'') AS account_status,t.reason AS target_reason,
       s.total AS current_total,s.sampled_at AS current_at,s.day AS sample_day,s.account_identity AS sample_identity,
       bs.total AS baseline_total,bs.sampled_at AS baseline_at,bs.account_identity AS baseline_identity,b.sealed_at,
       attempt.result AS attempt_result,attempt.day AS attempt_day,attempt.sampled_at AS attempt_at,
       a.work_count AS legacy_total,a.last_collected AS legacy_at,a.collection_state AS legacy_state,a.last_attempted AS legacy_attempt_at,a.registered_on,
       CASE WHEN t.task_state IS NULL AND a.status IN ('停用','封禁','短期封禁','养号7天') THEN 1 ELSE 0 END AS paused_account
      FROM accounts a LEFT JOIN computers c ON c.id=a.computer_id'''+computer_guard+'''
      LEFT JOIN publish_review_tasks t ON t.account_id=a.id AND t.day=?
      LEFT JOIN publish_review_baselines b ON b.account_id=a.id AND b.day=?
      LEFT JOIN publish_review_samples bs ON bs.id=b.sample_id
      LEFT JOIN publish_review_samples s ON s.id=(SELECT id FROM publish_review_samples WHERE account_id=a.id AND day<=? AND result='ok' ORDER BY sampled_at DESC,id DESC LIMIT 1)
      LEFT JOIN publish_review_samples attempt ON attempt.id=(SELECT id FROM publish_review_samples WHERE account_id=a.id AND day<=? ORDER BY sampled_at DESC,id DESC LIMIT 1)
      WHERE (t.account_id IS NOT NULL OR ?=?) AND '''+scope+office_where+'''),
     calculated AS (SELECT raw.*,CASE WHEN sample_day=? AND sample_identity=baseline_identity AND sample_identity=douyin_id
       THEN current_total-baseline_total ELSE NULL END AS net FROM raw),
     judged AS (SELECT calculated.*,CASE
       WHEN task_state='paused' OR paused_account=1 THEN 'paused'
       WHEN task_state='none' THEN 'no_task'
       WHEN task_state IS NULL OR target IS NULL OR computer_id='' OR current_at IS NULL OR sample_day<>?
         OR (attempt_day=? AND attempt_result='failed')
         OR (substr(legacy_attempt_at,1,10)=? AND legacy_state IN ('采集失败','需要验证') AND (current_at IS NULL OR legacy_attempt_at>=current_at))
         OR baseline_total IS NULL OR net<0
         OR baseline_identity<>sample_identity OR sample_identity<>douyin_id THEN 'review'
       WHEN net>=target THEN 'met' ELSE 'pending' END AS state FROM calculated)
    '''
    return sql, [*computer_parameters, day, day, day, day, day, current, *parameters, day, day, day, day]


def _row(row, day):
    row = dict(row)
    if day < now().date().isoformat():
        row['legacy_total'] = row['legacy_at'] = None
    reasons = []
    if row['state'] == 'review':
        if row['task_state'] is None:reasons.append('未保留当日任务计划，请确认是否安排任务')
        elif row['target'] is None:reasons.append('已安排任务，但当日目标尚未设置')
        if not row['computer_id']:reasons.append('当日任务电脑尚未确认')
        if row['current_at'] is None:reasons.append('没有留存成功采样；旧版最新值不冒充每日快照')
        elif row['sample_day'] != day:reasons.append('采样已过期：本业务日没有成功采样')
        if (row['attempt_day'] == day and row['attempt_result'] == 'failed') or (str(row['legacy_attempt_at'] or '').startswith(day) and row['legacy_state'] in ('采集失败','需要验证') and (row['current_at'] is None or row['legacy_attempt_at']>=row['current_at'])):reasons.append('本日最近一次采样失败')
        if row['baseline_total'] is None:reasons.append('新账号缺少昨日基线' if row['registered_on'] == day else '缺少昨日收口基线')
        if row['net'] is not None and row['net'] < 0:reasons.append('累计作品数量减少，保留负差值，需核对删除或隐藏等变化')
        if row['baseline_identity'] is not None and row['sample_identity'] is not None and row['baseline_identity']!=row['sample_identity']:reasons.append('两次采样之间账号标识发生变化，不能直接相减')
        elif row['sample_identity'] is not None and row['sample_identity']!=row['douyin_id']:reasons.append('现有采样与当日账号标识不一致，需重新核验')
    elif row['state'] == 'paused':reasons.append('当日任务暂停或账号未安排任务且处于停用/封禁/养号状态，不计入待完成')
    elif row['state'] == 'no_task':reasons.append('已明确登记本日无发布任务，不计入待完成')
    overdue = bool(row['deadline'] and stamp() > row['deadline'])
    labels = dict(met='净增达标', pending='未达标' if overdue else '进行中' if row['deadline'] else '待完成', review='数据待核验', paused='暂停任务', no_task='无当日任务')
    row.update(label=labels[row['state']], reasons=reasons, remaining=max(0, row['target']-row['net']) if row['state'] in ('met','pending') else None,
               progress=min(100, max(0, row['net']*100/row['target'])) if row['state'] in ('met','pending') and row['target'] else 100 if row['state']=='met' else None)
    return row


def overview(db, actor, query):
    actor = current_actor(db, actor)
    day = business_day(query.get('date', now().date().isoformat()))
    if day > now().date().isoformat():raise ValueError('未来日期尚无作品统计，请先查看今天或历史日期')
    office = query.get('office', '')
    state = query.get('state', '')
    computer = query.get('computer', '')
    if state not in ('', 'pending', 'review'):raise ValueError('核对状态筛选不正确')
    try:page = max(1, int(query.get('page', 1)))
    except (ValueError, TypeError):raise ValueError('页码不正确') from None
    cte, parameters = _cte(db, actor, day, office)
    summary = dict(met=0, pending=0, review=0, paused=0, no_task=0)
    for row in db.execute(cte+' SELECT state,COUNT(*) AS n FROM judged GROUP BY state', parameters):summary[row['state']] = row['n']
    computers = [dict(row) for row in db.execute(cte+''' SELECT computer_id,MIN(computer_name) AS computer_name,
      CASE WHEN COUNT(DISTINCT operator)>1 THEN '多位负责人（见账号）' ELSE MIN(operator) END AS operator,COUNT(*) AS accounts,
      SUM(CASE WHEN task_state='active' THEN 1 ELSE 0 END) AS scheduled,
      SUM(CASE WHEN state='met' THEN 1 ELSE 0 END) AS met,SUM(CASE WHEN state='pending' THEN 1 ELSE 0 END) AS pending,
      SUM(CASE WHEN state='review' THEN 1 ELSE 0 END) AS review FROM judged GROUP BY computer_id ORDER BY computer_name,computer_id''', parameters)]
    where, selected = ['1=1'], []
    if state:where.append('state=?');selected.append(state)
    if computer:where.append('computer_id=?');selected.append('' if computer=='unassigned' else computer)
    condition = ' AND '.join(where)
    total = db.execute(cte+' SELECT COUNT(*) FROM judged WHERE '+condition, parameters+selected).fetchone()[0]
    pages = max(1, (total+49)//50);page=min(page,pages)
    rows = [_row(row,day) for row in db.execute(cte+' SELECT * FROM judged WHERE '+condition+' ORDER BY computer_name,account_id LIMIT 50 OFFSET ?', parameters+selected+[(page-1)*50])]
    offices = []
    if getattr(db, 'scope_enabled', False):
        grants = office_scope._granted_offices(db, actor)
        offices = [dict(id=row[0],name=row[1]) for row in db.execute('SELECT id,name FROM offices WHERE active=1 ORDER BY name') if row[0] in grants]
    return dict(date=day, timezone='Asia/Shanghai', user={key:actor[key] for key in ('id','name','role','permissions','csrf') if key in actor},
                summary=summary, computers=computers, rows=rows, total=total, page=page, pages=pages, page_size=50, offices=offices,
                freshness='沿用原采集每日一次的口径：只有所选北京时间业务日的成功采样有效，不代表实时数据。',
                historical=day<now().date().isoformat(), can_manage=actor['role']=='owner' or 'publishing_manage' in actor.get('permissions', []))

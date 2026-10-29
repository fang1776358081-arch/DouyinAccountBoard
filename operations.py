"""Business history and optimistic concurrency; authentication records are excluded."""
import hashlib
import json
import time
import uuid
from sql_dialect import history_account
from datetime import date,timedelta

TABLE_KEYS={'computers':['id'],'accounts':['id'],'phone_cards':['id'],'phone_devices':['id'],'daily':['account_id','day'],
            'publishing_layout':['batch','computer_id','slot'],'nurture_records':['account_id']}
PREFIX={'computers':'computer','accounts':'account','phone_cards':'card','phone_devices':'device','daily':'daily','publishing_layout':'layout','nurture_records':'nurture'}
WORKFLOW_TABLE_KEYS={'phone_units':['id'],'phone_card_placements':['card_id'],
                     'phone_account_access':['account_id'],'device_cases':['id'],'device_workflow_events':['id']}
PREFIX.update(phone_units='unit',phone_card_placements='placement',phone_account_access='access',
              device_cases='case',device_workflow_events='workflow_event')


class ConflictError(ValueError):pass


def encoded(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def token(value):return hashlib.sha256(encoded(value).encode()).hexdigest()
def row_key(table,row):return PREFIX[table]+':'+':'.join(str(row[key]) for key in {**TABLE_KEYS,**WORKFLOW_TABLE_KEYS}[table])


def migrate(db):
    db.execute('''CREATE TABLE IF NOT EXISTS operations_history(id INTEGER PRIMARY KEY,group_id TEXT NOT NULL,
      at INTEGER NOT NULL,actor_id INTEGER,actor_name TEXT NOT NULL,action TEXT NOT NULL,entity TEXT NOT NULL,
      before_json TEXT,after_json TEXT)''')
    db.execute('CREATE INDEX IF NOT EXISTS operations_history_entity ON operations_history(entity,id)')
    db.execute('''CREATE TABLE IF NOT EXISTS operations_previews(id TEXT PRIMARY KEY,actor_id INTEGER NOT NULL,
      expires_at INTEGER NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL)''')
    if 'nurture_started' not in {row[1] for row in db.execute('PRAGMA table_info(accounts)')}:
        db.execute("ALTER TABLE accounts ADD COLUMN nurture_started TEXT NOT NULL DEFAULT ''")


def snapshot(db,*,daily_day=None):
    result={}
    scoped=getattr(db,'scope_enabled',False)
    if scoped:
        import office_scope
        maps=office_scope.scope_maps(db)
    import device_workflow
    tables = {**TABLE_KEYS, **(WORKFLOW_TABLE_KEYS if device_workflow.available(db) else {})}
    for table in tables:
        rows=db.execute('SELECT * FROM daily WHERE day=?',(daily_day,)) if table=='daily' and daily_day is not None else db.execute('SELECT * FROM '+table)
        for row in rows:
            value=dict(row)
            if scoped:value['office_ids']=office_scope.record_offices(maps,table,value)
            result[row_key(table,value)]=value
    return result


def versions(db,context=None,*,daily_day=None):
    records=snapshot(db) if daily_day is None else snapshot(db,daily_day=daily_day)
    if context is not None:
        import office_scope
        tables={value:key for key,value in PREFIX.items()}
        records={key:row for key,row in records.items() if office_scope.record_visible(context,tables[key.split(':',1)[0]],row)}
    return {key:token(value) for key,value in records.items()}


def daily_versions(db,day,context=None):
    """Tokens for one date, including absent rows, in the caller's read snapshot."""
    if type(day) is not str:raise ValueError('日期格式不正确')
    date.fromisoformat(day)
    scoped=getattr(db,'scope_enabled',False)
    ids=context['ids']['accounts'] if context is not None else {row[0] for row in db.execute('SELECT id FROM accounts')}
    maps=None
    if scoped:
        if context is not None:maps=context['maps']
        else:
            accounts={}
            for row in db.execute('SELECT account_id,office_id FROM account_offices'):
                accounts.setdefault(row[0],[]).append(row[1])
            maps={'accounts':{ident:sorted(offices) for ident,offices in accounts.items()}}
    result={'daily:'+str(ident)+':'+day:None for ident in sorted(ids)}
    for row in db.execute('SELECT * FROM daily WHERE day=?',(day,)):
        if row['account_id'] not in ids:continue
        value=dict(row)
        if scoped:
            import office_scope
            value['office_ids']=office_scope.record_offices(maps,'daily',value)
        result[row_key('daily',value)]=token(value)
    return result


def version_identity(key):
    if type(key) is not str or not key or len(key)>300 or ':' not in key:raise ValueError('资料版本编号不正确')
    prefix,rest=key.split(':',1)
    table={value:name for name,value in PREFIX.items()}.get(prefix)
    if table is None or not rest:raise ValueError('资料版本编号不正确')
    if table=='daily':
        account_id,sep,day=rest.rpartition(':')
        if not sep or not account_id or not day:raise ValueError('资料版本编号不正确')
        return table,{'account_id':account_id,'day':day}
    if table=='publishing_layout':
        batch,sep,tail=rest.partition(':')
        computer_id,sep2,slot=tail.rpartition(':')
        if not sep or not sep2 or not batch or not computer_id or not slot:raise ValueError('资料版本编号不正确')
        try:slot=int(slot)
        except ValueError:raise ValueError('资料版本编号不正确') from None
        return table,{'batch':batch,'computer_id':computer_id,'slot':slot}
    return table,{{**TABLE_KEYS,**WORKFLOW_TABLE_KEYS}[table][0]:rest}


def versions_for(db,keys,context=None):
    if type(keys) is not list or len(keys)>80:raise ValueError('资料版本编号不正确')
    scoped=getattr(db,'scope_enabled',False)
    maps=None
    if scoped:
        import office_scope
        maps=context['maps'] if context else office_scope.scope_maps(db)
    result={}
    for key in keys:
        table,ident=version_identity(key)
        columns={**TABLE_KEYS,**WORKFLOW_TABLE_KEYS}[table]
        row=db.execute('SELECT * FROM '+table+' WHERE '+' AND '.join(column+'=?' for column in columns),tuple(ident[column] for column in columns)).fetchone()
        if row is None:
            result[key]=None
            continue
        value=dict(row)
        if row_key(table,value)!=key:raise ValueError('资料版本编号不正确')
        if scoped:
            import office_scope
            value['office_ids']=office_scope.record_offices(maps,table,value)
            if context is not None and not office_scope.record_visible(context,table,value):
                result[key]=None
                continue
        result[key]=token(value)
    return result


def targets(db,path,body):
    if path=='/api/device-workflow':
        import device_workflow
        return device_workflow.version_keys(db,body)
    if path=='/api/account/patch':
        changes=body.get('changes',{});ident=str(body.get('id',''))
        keys=targets(db,'/api/account',{**changes,'id':ident})
        old=db.execute('SELECT computer_id,parent_account_id FROM accounts WHERE id=?',(ident,)).fetchone()
        for field,index,prefix in [('computer_id',0,'computer:'),('parent_account_id',1,'account:')]:
            if (field in changes or field=='computer_id' and 'slot' in changes) and old:
                keys += [prefix+str(value) for value in (old[index],changes.get(field,old[index])) if value and value!='unassigned']
        return sorted(set(keys))
    if path=='/api/batch-edit':
        import batch_edit
        import device_workflow
        return sorted(set(batch_edit.keys(body) + device_workflow.legacy_version_keys(db,path,body)))
    keys=[];ident=str(body.get('id',''))
    if path.startswith('/api/workspace/nurture/'):keys+=['account:'+ident,'nurture:'+ident]
    if path in ['/api/account','/api/account/status','/api/account/card-readiness','/api/lifecycle-status','/api/account/phone-device','/api/account/nurture']:
        keys.append('account:'+ident)
    if path=='/api/bulk-status':keys+=['account:'+str(value) for value in body.get('ids',[])]
    if path=='/api/daily':keys+=['account:'+ident,'daily:'+ident+':'+str(body.get('date',''))]
    if path=='/api/phone-card' and body.get('id'):keys.append('card:'+ident)
    if path=='/api/phone-device':keys.append('device:'+ident)
    if path in ['/api/account','/api/account/create','/api/account/phone-device'] and 'phone_device_id' in body:
        old=db.execute('SELECT phone_device_id FROM accounts WHERE id=?',(ident,)).fetchone()
        keys+=['device:'+str(value) for value in [old[0] if old else None,body.get('phone_device_id')] if value]
    if path=='/api/publishing/assignment':
        for computer,slot in [(body.get('computer_id'),body.get('slot')),(body.get('target_computer'),body.get('target_slot'))]:
            if computer is not None and slot is not None:keys.append('layout:'+str(body.get('batch'))+':'+str(computer)+':'+str(slot))
    import device_workflow
    return sorted(set(keys + device_workflow.legacy_version_keys(db,path,body)))


class _DailyRecords(dict):
    """Private two-record snapshot for the handler that writes only daily."""
    def __init__(self,ident,day,maps):
        super().__init__()
        self.ident,self.day,self.maps=ident,day,maps


def _daily_snapshot(db,ident,day):
    offices=sorted(row[0] for row in db.execute('SELECT office_id FROM account_offices WHERE account_id=?',(ident,)))
    records=_DailyRecords(ident,day,{'accounts':{ident:offices}})
    for table,query,params in (
        ('accounts','SELECT * FROM accounts WHERE id=?',(ident,)),
        ('daily','SELECT * FROM daily WHERE account_id=? AND day=?',(ident,day))):
        row=db.execute(query,params).fetchone()
        if row is not None:
            value={**dict(row),'office_ids':offices}
            records[row_key(table,value)]=value
    return records


def before(db,path,body):
    if path=='/api/daily' and getattr(db,'scope_enabled',False):
        ident,day=str(body.get('id','')),str(body.get('date',''))
        records=_daily_snapshot(db,ident,day)
        keys=sorted(['account:'+ident,'daily:'+ident+':'+day])
    else:records=snapshot(db);keys=targets(db,path,body)
    expected=body.get('_expected_versions')
    if keys and not isinstance(expected,dict):raise ConflictError('页面版本过旧，请刷新后重新操作；未保存任何修改')
    for key in keys:
        if expected.get(key)!= (token(records[key]) if key in records else None):
            raise ConflictError('这条资料已被其他人修改，请刷新核对后再保存；未覆盖对方修改')
    return records,keys


def finish(handler,db,actor,path,state):
    import nurture
    from app import today
    scoped=getattr(db,'scope_enabled',False)
    old,checked=state
    if scoped and path=='/api/daily' and isinstance(old,_DailyRecords):
        import office_scope
        # /api/daily changes no account fields, so the full-path touched-account
        # set is empty. Do not rescan unrelated histories or nurture records.
        new=_daily_snapshot(db,old.ident,old.day)
        maps=new.maps
    elif scoped:
        interim=snapshot(db)
        touched={row['id'] for key,row in interim.items() if key.startswith('account:') and state[0].get(key)!=row}
        nurture.sync(db,today(),touched)
        import office_scope
        maps=office_scope.scope_maps(db)
        new=snapshot(db)
    else:
        nurture.sync(db,today())
        new=snapshot(db)
    changed=[key for key in old.keys()|new.keys() if old.get(key)!=new.get(key)]
    group=uuid.uuid4().hex;now=int(time.time())
    for key in changed:
        db.execute('INSERT INTO operations_history(group_id,at,actor_id,actor_name,action,entity,before_json,after_json) VALUES(?,?,?,?,?,?,?,?)',
          (group,now,actor['id'] if actor else None,actor['name'] if actor else '采集服务',path,key,
           encoded(old.get(key)),encoded(new.get(key))))
        if scoped:
            table={value:name for name,value in PREFIX.items()}[key.split(':',1)[0]]
            scopes=office_scope.history_scopes(maps,table,old.get(key),new.get(key))
            db.execute('UPDATE operations_history SET scope_offices=? WHERE group_id=? AND entity=?',(encoded(scopes) if scopes else None,group,key))
    handler._version_delta={key:token(new[key]) if key in new else None for key in set(changed)|set(checked)}


def history(db,entity='',before_id=0,actor=None):
    context=None
    if getattr(db,'scope_enabled',False):
        import office_scope
        import auth
        context=office_scope.read_context(db,actor)
        if entity and not context['global_access']:
            prefix,sep,ident=entity.partition(':')
            kind={'account':'accounts','card':'cards','device':'devices','computer':'computers','nurture':'accounts'}.get(prefix)
            if not sep or kind is None:raise auth.AccessError('无权访问此资料历史',403)
            office_scope.require_entity(db,actor,kind,ident)
    daily_prefix='daily:'+entity.split(':',1)[1]+':%' if entity.startswith('account:') else ''
    account_id=entity.split(':',1)[1] if entity.startswith('account:') else ''
    rows=db.execute("SELECT * FROM operations_history WHERE (?=? OR entity=? OR entity=? OR (?<>? AND entity LIKE ?) OR (?<>'' AND entity LIKE 'layout:%' AND ("+history_account(db,'before_json')+"=? OR "+history_account(db,'after_json')+"=?))) AND (?=0 OR id<?) ORDER BY id DESC LIMIT 100",
                    (entity,'',entity,'nurture:'+account_id if account_id else '',daily_prefix,'',daily_prefix,account_id,account_id,account_id,before_id,before_id)).fetchall()
    result=[]
    for row in rows:
        if str(row['entity']).startswith('sync:'):continue
        value=dict(row);old=json.loads(value.pop('before_json') or 'null');new=json.loads(value.pop('after_json') or 'null')
        if context is not None and not context['global_access']:
            try:scopes=json.loads(value.get('scope_offices') or 'null')
            except (ValueError,TypeError):continue
            if not isinstance(scopes,list) or not scopes or any(not isinstance(s,str) for s in scopes) or not set(scopes)<=context['grants']:continue
            table={prefix:name for name,prefix in PREFIX.items()}.get(value['entity'].split(':',1)[0])
            if table is None or not office_scope.record_visible(context,table,new or old or {}):continue
        value.pop('scope_offices',None)
        value['changes']=[dict(field=key,before=(old or {}).get(key),after=(new or {}).get(key)) for key in sorted((old or {}).keys()|(new or {}).keys()) if (old or {}).get(key)!=(new or {}).get(key)]
        result.append(value)
    return dict(entries=result,next_before=rows[-1]['id'] if len(rows)==100 else 0)


def preview(db,actor,kind,payload):
    ident=uuid.uuid4().hex;db.execute('DELETE FROM operations_previews WHERE expires_at<?',(int(time.time()),))
    db.execute('INSERT INTO operations_previews VALUES(?,?,?,?,?)',(ident,actor['id'],int(time.time())+1800,kind,encoded(payload)))
    return ident


def take_preview(db,actor,ident,kind):
    row=db.execute('SELECT * FROM operations_previews WHERE id=? AND actor_id=? AND kind=? AND expires_at>?',(ident,actor['id'],kind,int(time.time()))).fetchone()
    if not row:raise ValueError('预览已过期或不属于当前登录人员，请重新预览')
    return json.loads(row['payload'])


def assignments(db,body,actor_id):
    import publishing_layout
    state=db.execute('SELECT batch FROM publishing_layout_state WHERE id=1').fetchone()
    if not state or body.get('batch')!=state['batch']:raise ConflictError('发布安排批次已更新，请刷新')
    if type(body.get('slot')) is not int:raise ValueError('发布位置不正确')
    key=(state['batch'],str(body.get('computer_id','')),body['slot'])
    source=db.execute('SELECT * FROM publishing_layout WHERE batch=? AND computer_id=? AND slot=?',key).fetchone()
    if not source or not source['account_id']:raise ValueError('请选择已关联账号的发布位置')
    if body.get('action') not in ['move','remove']:raise ValueError('位置操作不正确')
    if body['action']=='move':
        if type(body.get('target_slot')) is not int:raise ValueError('目标位置不正确')
        target_key=(key[0],str(body.get('target_computer','')),body['target_slot'])
        target=db.execute('SELECT * FROM publishing_layout WHERE batch=? AND computer_id=? AND slot=?',target_key).fetchone()
        if not target or target['account_id'] or target['label'].strip():raise ConflictError('目标不是空白发布格，请选择空白位置')
        if key[1].startswith('office3:')!=target_key[1].startswith('office3:'):raise ValueError('不同设备批次不能混用发布电脑')
        db.execute('UPDATE publishing_layout SET account_id=?,label=?,office=?,reason=? WHERE batch=? AND computer_id=? AND slot=?',
                   (source['account_id'],source['label'],source['office'],'',*target_key))
        db.execute('INSERT INTO publishing_layout_links VALUES(?,?,?,?,?,?)',(*target_key,target['reason'],actor_id,int(time.time())))
    db.execute("UPDATE publishing_layout SET account_id=NULL,label='',reason='' WHERE batch=? AND computer_id=? AND slot=?",key)
    db.execute('DELETE FROM publishing_layout_links WHERE batch=? AND computer_id=? AND slot=?',key)

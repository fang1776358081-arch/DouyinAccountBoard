"""Physical phones, independent of SIM ownership and publishing computers."""
import re
import uuid
from sql_dialect import integer_order

STATES = ['待确认', '在用', '待清理', '可用闲置', '故障']
INACTIVE = {'封禁', '停用'}


def number(value):
    match=re.fullmatch(r'(\d{1,6})(?:号手机|多开(?:抖音)?)?',str(value).strip(),flags=re.ASCII)
    return str(int(match[1])) if match and int(match[1])>0 else None


def device_identity(value):
    raw=str(value).strip()
    namespaced=re.fullmatch(r'(phone:[a-f0-9]{32}:)([0-9]{1,6})',raw,flags=re.ASCII)
    if namespaced:
        normalized=number(namespaced[2])
        if normalized:return namespaced[1]+normalized
        raise ValueError('手机序号请填1至6位正整数')
    prefix='office3:' if raw.startswith('office3:') else ''
    digits=raw[len(prefix):]
    normalized=number(digits)
    if not normalized or not digits.isascii() or not digits.isdigit():
        raise ValueError('手机编号请填1至6位正整数')
    return prefix+normalized


def number_from_id(value):
    """Read only reviewed identity formats; never infer an office from a prefix."""
    try:return device_identity(value).rsplit(':',1)[-1]
    except ValueError:return None


def _number(value):
    raw=str(value).strip()
    if not re.fullmatch(r'[0-9]{1,6}',raw,flags=re.ASCII) or not int(raw):
        raise ValueError('手机序号请填1至6位正整数')
    return str(int(raw))


def new_identity(value):
    return 'phone:'+uuid.uuid4().hex+':'+_number(value)


def office_number_devices(db, office_id, value):
    """Find reservations by explicit mappings, including ambiguous legacy scopes."""
    serial=_number(value)
    return [dict(row) for row in db.execute('''SELECT d.* FROM phone_devices d
        JOIN phone_device_offices o ON o.device_id=d.id WHERE o.office_id=? ORDER BY d.id''',(office_id,))
        if number_from_id(row['id'])==serial]


def validate_device_scope(db, device_id, office_ids, *, newly_created=False):
    """Validate a scope change under the caller's admitted write transaction.

    Existing unassigned/multi-office records are preserved until explicitly
    changed. They are never split, merged, renumbered or silently assigned.
    """
    import office_scope
    office_scope.admitted(db)
    wanted=office_scope.validated_offices(db,office_ids)
    current={row[0] for row in db.execute('SELECT office_id FROM phone_device_offices WHERE device_id=?',(device_id,))}
    if not newly_created and current==wanted:return sorted(wanted)
    if len(wanted)!=1:raise ValueError('每台手机必须明确选择一个办公室，不能共享或清空归属')
    serial=number_from_id(device_id)
    if serial is None:raise ValueError('该旧设备的手机序号待核对，不能自动更改归属')
    if any(row['id']!=device_id for row in office_number_devices(db,next(iter(wanted)),serial)):
        raise ValueError('这个办公室已经有'+serial+'号手机，请选择其他序号；原资料未改动')
    return sorted(wanted)


def migrate(db):
    db.execute("""CREATE TABLE IF NOT EXISTS phone_devices(
        id TEXT PRIMARY KEY, name TEXT NOT NULL, state TEXT NOT NULL DEFAULT '待确认',
        state_source TEXT NOT NULL DEFAULT 'auto', owner TEXT NOT NULL DEFAULT '',
        location TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '')""")
    columns={r[1] for r in db.execute('PRAGMA table_info(accounts)')}
    for name, definition in {
        'phone_device_id':'TEXT REFERENCES phone_devices(id)',
        'phone_device_source':"TEXT NOT NULL DEFAULT ''",
        'phone_device_note':"TEXT NOT NULL DEFAULT ''",
        'office_group':"TEXT NOT NULL DEFAULT ''",
    }.items():
        if name not in columns:db.execute('ALTER TABLE accounts ADD COLUMN '+name+' '+definition)


def overview(db, accounts, allowed_ids=None):
    order=integer_order(db,"CASE WHEN id LIKE 'office3:%' THEN substr(id,9) ELSE id END")
    devices=[dict(r) for r in db.execute("SELECT * FROM phone_devices ORDER BY CASE WHEN id LIKE 'office3:%' THEN 1 ELSE 0 END,"+order+",id")]
    if allowed_ids is not None:devices=[row for row in devices if row['id'] in allowed_ids]
    mappings={}
    scoped=getattr(db,'scope_enabled',False)
    if scoped:
        for row in db.execute('SELECT device_id,office_id FROM phone_device_offices'):
            mappings.setdefault(row[0],[]).append(row[1])
    for device in devices:
        device['number']=number_from_id(device['id'])
        if scoped:
            scopes=sorted(mappings.get(device['id'],[]))
            device['office_ids']=scopes
            device['office_id']=scopes[0] if len(scopes)==1 else None
            device['identity_state']='已确认单办公室' if len(scopes)==1 and device['number'] else '归属或序号待核对'
    if scoped:devices.sort(key=lambda row:(row['office_id'] or '',int(row['number']) if row['number'] else 1000000,row['id']))
    by_id={d['id']:d for d in devices}
    by_device={}
    for a in accounts:
        device=by_id.get(a['phone_device_id'])
        a['phone_device_name']=device['name'] if device else ''
        by_device.setdefault(a['phone_device_id'],[]).append(a)
    for d in devices:
        own=by_device.get(d['id'],[])
        inactive=sum(a['status'] in INACTIVE for a in own)
        d['account_ids']=[a['id'] for a in own]
        d['account_count']=len(own)
        d['inactive_count']=inactive
        d['occupancy']='无当前账号' if not own else '仅封禁或停用' if inactive==len(own) else '有在用或待确认账号'
        d['suggested_state']='待确认' if not own else '待清理' if inactive==len(own) else '在用'
        if d['state_source']=='auto':d['state']=d['suggested_state']
        d['attention']='没有当前登录记录，需确认是否已清理' if not own and d['state']!='可用闲置' else '关联账号均封禁或停用，请核对清理' if own and inactive==len(own) else ''
    return dict(devices=devices,statuses=STATES,unassigned=sum(not a['phone_device_id'] for a in accounts),
                unresolved=sum('待核对' in a['phone_device_note'] and a['phone_device_source']!='manual' for a in accounts))


def save(db, body, create=False, actor=None, *, initialize_unit=True):
    scoped=getattr(db,'scope_enabled',False)
    if scoped:
        import office_scope
        office_scope.authorize_route(db,actor,'/api/phone-device/create' if create else '/api/phone-device',body)
    raw=str(body.get('id','')).strip()
    device_id=new_identity(body.get('number','')) if create and not raw else device_identity(raw)
    if 'number' in body and _number(body['number'])!=number_from_id(device_id):
        raise ValueError('手机序号与内部编号不一致；已有设备请通过重新编号流程处理')
    old=db.execute('SELECT * FROM phone_devices WHERE id=?',(device_id,)).fetchone()
    if create and old:raise ValueError('这个手机编号已经存在，请编辑已有设备')
    if not create and not old:raise ValueError('手机设备不存在')
    scopes=None
    if scoped:
        office_scope.admitted(db)
        if create or 'office_ids' in body:
            scopes=office_scope.requested_offices(db,actor,'devices',body)
            validate_device_scope(db,device_id,scopes,newly_created=create)
    state=body.get('state','待确认')
    state_source='auto' if state=='auto' else 'manual'
    if state=='auto':state='待确认'
    if state not in STATES:raise ValueError('手机设备状态不正确')
    if state=='可用闲置':
        import device_workflow
        if device_workflow.available(db):
            linked=db.execute('SELECT 1 FROM phone_account_access a JOIN phone_units u ON u.id=a.last_unit_id WHERE u.device_id=? AND u.active=1',(device_id,)).fetchone()
            has_card=db.execute('SELECT 1 FROM phone_card_placements p JOIN phone_units u ON u.id=p.unit_id WHERE u.device_id=? AND u.active=1',(device_id,)).fetchone()
        else:
            linked=db.execute('SELECT 1 FROM accounts WHERE phone_device_id=?',(device_id,)).fetchone();has_card=False
        if linked or has_card:raise ValueError('仍有当前账号或手机卡关联，请先移出并确认清理完成，再标记可用闲置')
    values=[]
    for field,limit in [('name',100),('owner',100),('location',200),('note',2000)]:
        value=str(body.get(field,'')).strip()
        if len(value)>limit:raise ValueError('手机资料内容过长')
        values.append(value)
    name,owner,location,note=values
    if not name:
        serial=number_from_id(device_id)
        if scoped:
            if scopes is None:scopes=sorted(office_scope.entity_offices(db,'devices',device_id))
            office=db.execute('SELECT name FROM offices WHERE id=?',(scopes[0],)).fetchone() if len(scopes)==1 else None
            name=(office[0]+' · ' if office else '')+serial+'号手机'
        else:
            name=('办公室三 · ' if device_id.startswith('office3:') else '')+serial+'号手机'
    if create:
        db.execute('INSERT INTO phone_devices(id,name,state,state_source,owner,location,note) VALUES(?,?,?,?,?,?,?)',(device_id,name,state,state_source,owner,location,note))
    else:
        db.execute('UPDATE phone_devices SET name=?,state=?,state_source=?,owner=?,location=?,note=? WHERE id=?',(name,state,state_source,owner,location,note,device_id))
    if scoped:office_scope.save_assignment(db,actor,'devices',device_id,body,create)
    import device_workflow
    if initialize_unit and device_workflow.available(db):
        unit=device_workflow.ensure_unit(db,device_id)
        if unit['status']!=state and (not old or state!=old['state'] or state_source!=old['state_source']):
            device_workflow._unit_status(db,actor or {'id':None,'name':'设备资料登记'},
                                        {'unit_id':unit['id'],'status':state,'note':unit['note']})
    return device_id


def assign(db, account_id, value, source='manual'):
    account=db.execute('SELECT phone_device_id,status FROM accounts WHERE id=?',(str(account_id),)).fetchone()
    if not account:raise ValueError('账号不存在')
    device_id=str(value or '').strip() or None
    if device_id:
        target=db.execute('SELECT * FROM phone_devices WHERE id=?',(device_id,)).fetchone()
        if not target:raise ValueError('请选择已有手机设备，或先新增手机')
        if target['state']=='故障' and account['phone_device_id']!=device_id:
            raise ValueError('该手机标记为故障，请先核对设备状态')
    if account['phone_device_id']==device_id:return
    import device_workflow
    device_workflow.sync_assignment(db,str(account_id),device_id)
    db.execute('UPDATE accounts SET phone_device_id=?,phone_device_source=? WHERE id=?',(device_id,source,str(account_id)))
    if device_id:
        db.execute('UPDATE accounts SET office_group=? WHERE id=?',('three' if device_id.startswith('office3:') else '',str(account_id)))
        # A newly assigned phone must no longer appear as available idle.
        db.execute("UPDATE phone_devices SET state_source='auto' WHERE id=? AND state<>'故障'",(device_id,))
    if account['phone_device_id']:
        db.execute("UPDATE phone_devices SET state='待确认',state_source='auto' WHERE id=? AND state='在用' AND NOT EXISTS(SELECT 1 FROM accounts WHERE phone_device_id=?)",(account['phone_device_id'],account['phone_device_id']))

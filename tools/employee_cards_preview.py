"""Interactive local fixture with the real service and a fresh synthetic DB.

Never imports the app before isolation. The login helper issues a newly minted
synthetic-only session through HTTP, never reads a browser or production cookie.
Legacy writes/board reads are unavailable; no schema or employee is copied.
"""
import argparse
import json
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from test_isolation import create_root, install


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    root = create_root(args.root)
    install(root)
    import app
    import auth
    import employee_cards
    import phone_cards

    today = '2026-11-02'
    app.today = lambda: today
    app.initialize()
    with app.connect() as db:
        setup = auth.mint_setup(db)
        owner_session = auth.setup_owner(db, dict(username='synthetic-owner', name='合成预览管理员',
            password='Synthetic-local-only-2026'), 'board_setup=' + setup)
        owner = auth.session(db, 'board_session=' + owner_session)
        viewer_id = auth.save_user(db, owner, dict(username='synthetic-viewer', name='合成只读人员',
            role='viewer', permissions=[], password='Synthetic-local-only-2026'))
        viewer_session = auth.new_session(db, viewer_id)
        # new_session returns the raw synthetic session, not a production token.
        if not isinstance(viewer_session, str):
            raise ValueError('Unexpected synthetic session contract')

    def save(action, data, employee_id=''):
        with app.connect() as db:
            meta = employee_cards.get(db, owner, {'employee_id': [employee_id]} if employee_id else {}, today)
            body = dict(action=action, data=data, expected_version=meta['version'])
            if employee_id:
                body['employee_id'] = employee_id
            preview = employee_cards.preview(db, owner, body, today)
            return employee_cards.apply(db, owner, dict(preview_id=preview['preview_id'],
                request_id=str(uuid4()), confirmation='确认保存'), today)

    employee_ids = []
    for i, (name, birth) in enumerate((('合成·明天满21岁', '2005-11-03'),
                                      ('合成·注销处理中', '1995-03-01'),
                                      ('合成·全口径已满', '1980-03-01')), 1):
        data = dict(name=name, employee_no='S00' + str(i), office_id='', birth_date=birth,
                    employment_status='active', consent_source='仅用于合成样板的授权记录', consent_on=today)
        if i == 3:
            data['auth_user_id'] = viewer_id
        employee_ids.append(save('employee', data)['employee_id'])
    with app.connect() as db:
        card_id = phone_cards.save(db, dict(phone='13800000101', carrier='民生电信', status='停用',
            label='合成卡', owner='合成实际使用人', keeper='合成保管人'), actor=owner)
    unit_id = save('card_link', dict(card_id=card_id, operator_id='minsheng', activated_on=today,
                                   source='合成实名关联，不由保管人推断'), employee_ids[1])['id']
    save('cancel_request', dict(unit_id=unit_id, requested_on=today, source='合成注销申请，尚未确认释放'), employee_ids[1])
    save('verification', dict(scope='all', scope_id='', period='stock', period_start=today,
        period_end=today, count=10, coverage='total', verified_on=today, valid_until=today,
        source='仅合成：涵盖全部基础运营商与虚商的名下核验', pending_unit_ids=[]), employee_ids[2])

    class FixtureHandler(app.Handler):
        def log_message(self, *_args):
            pass

        def get_response(self):
            parsed = urlparse(self.path)
            if parsed.path == '/__synthetic_start__':
                role = parse_qs(parsed.query).get('role', ['owner'])[0]
                session = viewer_session if role == 'viewer' else owner_session
                self.send_response(303)
                self.send_header('Set-Cookie', auth.cookie('board_session', session))
                self.send_header('Location', '/employee-cards.html?synthetic=1')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if parsed.path.startswith('/api/') and parsed.path != '/api/employee-cards':
                return self.reply({'error': '仅本机合成员工办卡接口可用'}, 404)
            return super().get_response()

        def do_POST(self):
            if self.path not in ('/api/employee-cards/preview', '/api/employee-cards/apply'):
                self.close_connection = True
                return self.reply({'error': '旧业务写入在此合成夹具中不可用'}, 405)
            return super().do_POST()

    server = app.ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
    base = 'http://127.0.0.1:' + str(server.server_address[1])
    meta = dict(kind='real-service-local-synthetic-preview', url=base + '/__synthetic_start__',
                viewer_url=base + '/__synthetic_start__?role=viewer', employees=3,
                production_connected=False, legacy_writes_disabled=True, root=str(root))
    (root / 'preview-metadata.json').write_text(json.dumps(meta, indent=2), encoding='utf-8')
    print(json.dumps(meta), flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()

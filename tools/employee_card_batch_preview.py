"""Loopback-only full card page fixture for the employee batch-link UI.

Always installs isolation before importing the application. All people, cards,
and the synthetic session are generated here. Legacy write routes are disabled.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from urllib.parse import urlparse
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

    day = '2026-11-03'
    app.today = lambda: day
    app.initialize()
    with app.connect() as db:
        setup = auth.mint_setup(db)
        session = auth.setup_owner(db, dict(username='synthetic-owner', name='合成批量关联预览',
                                           password='Synthetic-local-only-2026'), 'board_setup=' + setup)
        actor = auth.session(db, 'board_session=' + session)

    def save(action, data, employee_id=''):
        with app.connect() as db:
            meta = employee_cards.get(db, actor, {'employee_id': [employee_id]} if employee_id else {}, day)
            preview = employee_cards.preview(db, actor, dict(action=action, employee_id=employee_id,
                data=data, expected_version=meta['version']), day)
            return employee_cards.apply(db, actor, dict(preview_id=preview['preview_id'],
                request_id=str(uuid4()), confirmation='确认保存'), day)

    people = []
    for index, name in enumerate(('合成员工甲', '合成员工乙'), 1):
        people.append(save('employee', dict(name=name, employee_no='B00' + str(index),
            office_id='', employment_status='active'))['employee_id'])
    cards = []
    with app.connect() as db:
        for index, (carrier, status) in enumerate((('民生电信', '正常'), ('长城移动', '停用'), ('中国联通', '已注销')), 1):
            cards.append(phone_cards.save(db, dict(phone='1380000990' + str(index), carrier=carrier,
                status=status, label='合成卡' + str(index), owner='合成提供人文字', keeper='合成保管人',
                note='合成原备注：批量关联不得修改', code_state='无待办', return_state='不需归还'), actor=actor))
    save('card_link', dict(card_id=cards[-1], operator_id='liantong', activated_on='',
                           source='合成：已由员工乙确认实名'), people[-1])
    with app.connect() as db:
        original = [dict(row) for row in db.execute('SELECT * FROM phone_cards ORDER BY id')]
        fingerprint = hashlib.sha256(json.dumps(original, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    (root / 'original-card-fingerprint.json').write_text(json.dumps({'sha256': fingerprint}), encoding='utf-8')

    class FixtureHandler(app.Handler):
        def log_message(self, *_args):
            pass

        def get_response(self):
            if urlparse(self.path).path == '/__synthetic_start__':
                self.send_response(303)
                self.send_header('Set-Cookie', auth.cookie('board_session', session))
                self.send_header('Location', '/?page=cards&synthetic=1')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            return super().get_response()

        def do_POST(self):
            if self.path not in ('/api/employee-cards/preview', '/api/employee-cards/apply'):
                self.close_connection = True
                return self.reply({'error': '本机夹具禁用旧业务写入，仅验证合成实名关联'}, 405)
            return super().do_POST()

    server = app.ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
    metadata = dict(kind='synthetic-card-batch-real-service-preview', production_connected=False,
                    url='http://127.0.0.1:' + str(server.server_address[1]) + '/__synthetic_start__',
                    employees=2, cards=3, prelinked_cards=1, root=str(root), legacy_writes_disabled=True)
    (root / 'preview-metadata.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    print(json.dumps(metadata), flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()

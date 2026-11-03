"""Explicit source-only package. Never walks data, backups or browser profiles."""
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent
PYTHON_FILES = '''app.py web_server.py auth.py account_service.py account_lifecycle.py
backup.py backup_management.py batch_edit.py collection.py data_exchange.py
nurture.py operations.py phone_cards.py phone_devices.py publishing_layout.py
workspace_service.py release_policy.py sql_dialect.py office_scope.py idempotency.py device_workflow.py office_three_import.py board_performance.py json_transport.py account_transport.py office_three_nickname.py board_updates.py'''.split()
STATIC_FILES = '''app.js auth-ui.js auth.css batch-edit.css batch-edit.js board-fit.css
collection-panel.js console-theme.css edit-versions.js field-search.css field-search.js identity-labels.js
index.html lifecycle.js login.html login.js login-theme.css nurture.js offices.js phone-cards.js
phone-devices.js publishing-layout.js publishing-match.css publishing-match.js
publishing-pending.css publishing-pending.js styles.css users.css users.html users.js
verification.css verification.js scope-ui.js workspace-model.js workspace-shortcuts.js workspace.css
workspace.html workspace.js office-three-import.js device-workflow.js device-workflow.css
account-review.js account-workspace.js account-workspace.css account-inline.js account-inline.css card-filters.js card-filters.css card-page.js card-details.js card-console.css board-updates.js live-sync.js live-sync.css'''.split()
PYTHON_FILES += ['employee_cards.py','employee_card_rules.py','employee_card_ledger.py','publish_review.py','f2_worker.py']
STATIC_FILES += ['employee-cards.html','employee-cards.js','employee-cards.css','publish-review.html','publish-review.js','publish-review.css']
STATIC_FILES += ['employee-card-link.js','employee-card-link.css']
FILES = tuple(PYTHON_FILES + ['static/' + name for name in STATIC_FILES] + ['migrations/postgresql/0005_employee_cards.sql','migrations/postgresql/0006_publish_review.sql'])


def build(destination):
    destination = Path(destination)
    if destination.exists():
        raise ValueError('Refusing to overwrite an existing package')
    contents = {}
    for name in FILES:
        source = ROOT / name
        if source.resolve() != source or any(p.is_symlink() or p.is_junction() for p in (source, source.parent)):
            raise ValueError('Package input cannot be a link or junction')
        contents[name] = source.read_bytes()
    manifest = {name: hashlib.sha256(content).hexdigest() for name, content in contents.items()}
    with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in contents.items():
            archive.writestr(name, content)
        archive.writestr('SOURCE-MANIFEST.json', json.dumps(manifest, indent=2))
        archive.writestr('RELEASE-STATUS.txt', 'LOCAL SOURCE PACKAGE ONLY. NOT APPROVED FOR DEPLOYMENT.\n'
            'Database, migrations, Excel runtime, CloudBase and release acceptance remain pending.\n')
    return manifest

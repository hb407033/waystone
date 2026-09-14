"""Read-only dependency/backup/disk checks; writes local status for the external notifier."""
import json, os, shutil, time, urllib.request
from pathlib import Path
os.umask(0o077)
root=Path('/opt/waystone');checks={}
try:
    with urllib.request.urlopen('http://127.0.0.1:8900/ready',timeout=12) as r:
        checks['retrieval_ready']=r.status==200 and json.load(r).get('status')=='ready'
except Exception:checks['retrieval_ready']=False
try:
    backup=json.loads((root/'backups/latest.json').read_text())
    checks['backup_fresh']=time.time()-backup['created']<8*3600 and (root/'backups'/backup['archive']).exists()
except Exception:checks['backup_fresh']=False
try:checks['restore_recent']=time.time()-json.loads((root/'backups/restore-check.json').read_text())['checked']<35*86400
except Exception:checks['restore_recent']=False
checks['disk_available']=shutil.disk_usage(root).free>2*1024**3
status={'checked':time.time(),'ok':all(checks.values()),'checks':checks}
(root/'ops').mkdir(exist_ok=True,mode=0o700)
p=root/'ops/status.tmp';p.write_text(json.dumps(status));p.replace(root/'ops/status.json')
print(json.dumps(status))
raise SystemExit(0 if status['ok'] else 1)

"""Consistent local backups; deliberately excludes .env and plaintext service keys."""
import fcntl, hashlib, json, os, shutil, sqlite3, subprocess, tarfile, tempfile, time
from datetime import datetime, timezone
from pathlib import Path

def file_hash(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

os.umask(0o077)
root=Path('/opt/waystone');dest=root/'backups';dest.mkdir(exist_ok=True,mode=0o700)
with (dest/'backup.lock').open('w') as lock:
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    if shutil.disk_usage(dest).free<2*1024**3:raise RuntimeError('Insufficient disk space for backup')
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    with tempfile.TemporaryDirectory(prefix='snapshot-',dir=dest) as directory:
        work=Path(directory)
        with sqlite3.connect(root/'data/waystone.sqlite') as source, sqlite3.connect(work/'waystone.sqlite') as snapshot:
            source.backup(snapshot)
            assert snapshot.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            snapshot.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        snapshot.close();source.close()
        for database,filename in (('postgres','mem0.dump'),('mem0_app','mem0_app.dump')):
            with (work/filename).open('wb') as out:
                subprocess.run(['docker','exec','mem0-postgres-1','pg_dump','-U','postgres','-d',database,'-Fc'],stdout=out,check=True,timeout=300)
            with (work/filename).open('rb') as data:
                subprocess.run(['docker','exec','-i','mem0-postgres-1','pg_restore','--list'],stdin=data,stdout=subprocess.DEVNULL,check=True,timeout=60)
        history='/tmp/waystone-backup-history.sqlite'
        try:
            subprocess.run(['docker','exec','mem0-mem0-1','python','-c',"import sqlite3; s=sqlite3.connect('/app/history/history.db'); d=sqlite3.connect('/tmp/waystone-backup-history.sqlite'); s.backup(d); d.close(); s.close()"],check=True,timeout=60)
            subprocess.run(['docker','cp','mem0-mem0-1:'+history,str(work/'history.sqlite')],check=True,stdout=subprocess.DEVNULL)
        finally:subprocess.run(['docker','exec','mem0-mem0-1','python','-c',"from pathlib import Path; Path('/tmp/waystone-backup-history.sqlite').unlink(missing_ok=True)"],check=True)
        shutil.copy2(root/'compose.yaml',work/'compose.yaml')
        shutil.copy2(Path('/opt/mem0/server/compose.deploy.yaml'),work/'mem0-compose.yaml')
        names=('waystone.sqlite','mem0.dump','mem0_app.dump','history.sqlite','compose.yaml','mem0-compose.yaml')
        manifest={'created':time.time(),'format':2,'files':{name:file_hash(work/name) for name in names},'plaintext_env_files_included':False}
        (work/'manifest.json').write_text(json.dumps(manifest))
        archive=dest/('snapshot-'+stamp+'.tar.gz');partial=archive.with_suffix('.partial')
        with tarfile.open(partial,'w:gz') as tar:
            for filename in (*manifest['files'],'manifest.json'):tar.add(work/filename,arcname=filename)
        partial.replace(archive)
        checksum=file_hash(archive)
        archive.with_suffix(archive.suffix+'.sha256').write_text(checksum+'  '+archive.name+'\n')
        latest=dest/'latest.tmp';latest.write_text(json.dumps({'created':manifest['created'],'archive':archive.name,'sha256':checksum}))
        latest.replace(dest/'latest.json')
    # Only this script's completed snapshots, retained for 28 days.
    for p in dest.glob('snapshot-????????T??????Z.tar.gz'):
        if p.stat().st_mtime<time.time()-28*86400:
            p.unlink();p.with_suffix(p.suffix+'.sha256').unlink(missing_ok=True)
    print('Backup verified:',archive.name)

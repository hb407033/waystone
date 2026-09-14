"""Restore the latest snapshot into temporary SQLite/PostgreSQL; never touches live volumes."""
import argparse,hashlib,json,os,shutil,sqlite3,subprocess,tarfile,tempfile,time,uuid
from pathlib import Path
def file_hash(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

os.umask(0o077)
parser=argparse.ArgumentParser(description='Restore a snapshot into isolated temporary databases')
parser.add_argument('--root',default='/opt/waystone',help='Directory containing backups/latest.json and its archive')
root=Path(parser.parse_args().root);info=json.loads((root/'backups/latest.json').read_text());archive=root/'backups'/info['archive']
assert file_hash(archive)==info['sha256']
name='pm-restore-check-'+uuid.uuid4().hex[:8]
with tempfile.TemporaryDirectory(prefix='pm-restore-') as directory:
    work=Path(directory)
    with tarfile.open(archive) as tar:
        for filename in ('waystone.sqlite','mem0.dump','mem0_app.dump','history.sqlite','compose.yaml','mem0-compose.yaml','manifest.json'):
            with tar.extractfile(filename) as source, (work/filename).open('wb') as target:shutil.copyfileobj(source,target)
    manifest=json.loads((work/'manifest.json').read_text())
    for filename,digest in manifest['files'].items():
        assert filename in {'waystone.sqlite','mem0.dump','mem0_app.dump','history.sqlite','compose.yaml','mem0-compose.yaml'}
        assert file_hash(work/filename)==digest
    for filename in ('waystone.sqlite','history.sqlite'):
        with sqlite3.connect(work/filename) as db:
            assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            assert not db.execute('PRAGMA foreign_key_check').fetchall()
        db.close()
    try:
        subprocess.run(['docker','run','-d','--name',name,'--network','none','--memory','384m','--tmpfs','/var/lib/postgresql/data:rw,size=512m','-e','POSTGRES_HOST_AUTH_METHOD=trust','pgvector/pgvector:pg17'],check=True,stdout=subprocess.DEVNULL)
        for _ in range(30):
            if subprocess.run(['docker','exec',name,'pg_isready','-U','postgres'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0:break
            time.sleep(1)
        else:raise RuntimeError('Restore container did not become ready')
        subprocess.run(['docker','exec',name,'createdb','-U','postgres','mem0_app'],check=True)
        for database,filename in (('postgres','mem0.dump'),('mem0_app','mem0_app.dump')):
            with (work/filename).open('rb') as data:
                subprocess.run(['docker','exec','-i',name,'pg_restore','--exit-on-error','--no-owner','--no-privileges','-U','postgres','-d',database],stdin=data,check=True,timeout=300)
            subprocess.run(['docker','exec',name,'psql','-U','postgres','-d',database,'-c',"SELECT count(*) AS restored_tables FROM pg_tables WHERE schemaname='public'"],check=True)
        result={'checked':time.time(),'ok':True,'archive':archive.name,'sqlite_integrity':True,'postgres_restored':True}
        (root/'backups/restore-check.json').write_text(json.dumps(result))
        print(json.dumps(result))
    finally:subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

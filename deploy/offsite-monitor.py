"""Independent host checks and verified backup pull. No Mac runtime dependency.

Events remain in a private durable outbox until the separately configured SMTP notifier delivers them.
"""
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

ARCHIVE = re.compile(r"snapshot-\d{8}T\d{6}Z\.tar\.gz")


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    temporary.replace(path)


class Remote:
    def __init__(self, config):
        self.ssh = ["/usr/bin/ssh", "-T", "-o", "BatchMode=yes",
                    "-o", "StrictHostKeyChecking=yes", "-o", "IdentitiesOnly=yes",
                    "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
                    "-o", "ServerAliveCountMax=2", "-o", "UserKnownHostsFile=" + config["known_hosts"],
                    "-i", config["identity_file"], config["source"]]

    def metadata(self, command):
        result = subprocess.run(self.ssh + [command], check=True, capture_output=True,
                                timeout=25)
        if len(result.stdout) > 16384:
            raise ValueError("Metadata too large")
        return json.loads(result.stdout)

    def download(self, archive, target):
        with target.open("wb") as stream:
            subprocess.run(self.ssh + ["archive " + archive], check=True,
                           stdout=stream, stderr=subprocess.PIPE, timeout=300)
            stream.flush()
            os.fsync(stream.fileno())


def ready(url):
    # Monitoring must not inherit a developer proxy configuration.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers={"User-Agent": "ProjectMemory-Monitor/0.4"})
    with opener.open(request, timeout=15) as response:
        return response.status == 200 and json.loads(response.read(4096)).get("status") == "ready"


def run(config, remote, probe=ready, now=None):
    os.umask(0o077)
    now = time.time() if now is None else now
    root = Path(config["directory"])
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "monitor.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return check(config, remote, probe, now, root)


def check(config, remote, probe, now, root):
    errors = []
    previous = None
    try:
        if (root / "status.json").exists():
            previous = json.loads((root / "status.json").read_text())
    except Exception:
        errors.append("offsite_state_invalid")
    try:
        if not probe(config["ready_url"]):
            errors.append("public_not_ready")
    except Exception:
        errors.append("public_not_ready")
    try:
        status = remote.metadata("status")
        if not -60 <= now - status["checked"] <= 12 * 60:
            errors.append("source_monitor_stale")
        required = {"retrieval_ready", "backup_fresh", "restore_recent", "disk_available"}
        for name in sorted(required):
            if status["checks"].get(name) is not True:
                errors.append("source_" + name)
    except Exception:
        errors.append("source_unreachable")

    backup = None
    try:
        info = remote.metadata("latest")
        archive, sha256, size = info["archive"], info["sha256"], info["size"]
        if not ARCHIVE.fullmatch(archive) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("Unexpected backup metadata")
        if not isinstance(size, int) or size <= 0:
            raise ValueError("Unexpected backup size")
        if not -60 <= now - info["created"] <= 8 * 3600:
            errors.append("source_backup_stale")
        final = root / archive
        if final.is_symlink():
            raise ValueError("Unexpected backup path")
        if not final.exists() or final.stat().st_size != size or digest(final) != sha256:
            if shutil.disk_usage(root).free < size + 2 * 1024 ** 3:
                raise ValueError("Insufficient backup disk space")
            partial = root / (archive + ".partial")
            if partial.is_symlink():
                raise ValueError("Unexpected temporary path")
            try:
                remote.download(archive, partial)
                if partial.stat().st_size != size or digest(partial) != sha256:
                    raise ValueError("Backup integrity mismatch")
                partial.chmod(0o600)
                partial.replace(final)
            finally:
                partial.unlink(missing_ok=True)
        # Only publish successful verification; retain the last good copy on failure.
        write_json(root / "latest.json", dict(info, verified=now))
        backup = archive
        for path in root.glob("snapshot-????????T??????Z.tar.gz"):
            if path.name != archive and not path.is_symlink() and path.stat().st_mtime < now - 28 * 86400:
                path.unlink()
    except Exception:
        errors.append("backup_pull_failed")
    if shutil.disk_usage(root).free < 2 * 1024 ** 3:
        errors.append("offsite_disk_low")
    errors = sorted(set(errors))
    notification_configured = False
    email_config = Path(config.get("email_config", "/etc/waystone-offsite/email.json"))
    try:
        email = json.loads(email_config.read_text())
        notification_configured = email.get("enabled") is True and Path(email["password_file"]).is_file()
    except Exception:
        pass
    result = {"checked": now, "ok": not errors, "errors": errors, "backup": backup,
              "notification_configured": notification_configured}
    if errors != (previous or {}).get("errors", []):
        outbox = root / "outbox"
        outbox.mkdir(mode=0o700, exist_ok=True)
        write_json(outbox / (uuid.uuid4().hex + ".json"),
                   dict(result, event="fault" if errors else "recovery"))
    write_json(root / "status.json", result)
    return result


if __name__ == "__main__":
    try:
        config = json.loads(Path("/etc/waystone-offsite/config.json").read_text())
        result = run(config, Remote(config))
        print(json.dumps(result, ensure_ascii=False))
        raise SystemExit(0 if result["ok"] else 1)
    except Exception:
        print('{"ok":false,"errors":["offsite_monitor_execution_failed"]}')
        raise SystemExit(1)

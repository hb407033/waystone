import hashlib
import importlib.util
import io
import json
from pathlib import Path

import pytest


def test_probe_identifies_monitor_and_keeps_tls_validation(monkeypatch):
    module = load("offsite-monitor")
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size): return b'{"status":"ready"}'
    class Opener:
        def open(self, request, timeout):
            assert request.full_url == "https://example.invalid/ready"
            assert request.get_header("User-agent") == "ProjectMemory-Monitor/0.4"
            assert timeout == 15
            return Response()
    monkeypatch.setattr(module.urllib.request, "build_opener", lambda handler: Opener())
    assert module.ready("https://example.invalid/ready")


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path("deploy") / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def setup(tmp_path):
    module = load("offsite-monitor")
    payload = b"private isolated backup"
    archive = "snapshot-20260913T080000Z.tar.gz"

    class Remote:
        broken = False
        corrupt = False
        downloads = 0

        def metadata(self, command):
            if self.broken:
                raise ConnectionError()
            if command == "status":
                return {"checked": 1000, "checks": dict.fromkeys(
                    ("retrieval_ready", "backup_fresh", "restore_recent", "disk_available"), True)}
            return {"archive": archive, "sha256": hashlib.sha256(payload).hexdigest(),
                    "size": len(payload), "created": 1000}

        def download(self, name, target):
            assert name == archive
            self.downloads += 1
            target.write_bytes(b"bad" if self.corrupt else payload)

    config = {"directory": str(tmp_path), "ready_url": "https://example.invalid/ready"}
    return module, config, Remote(), payload, archive


def test_independent_monitor_preserves_copy_through_host_outage(setup):
    module, config, remote, payload, archive = setup
    root = Path(config["directory"])
    first = module.run(config, remote, probe=lambda _: True, now=1000)
    assert first["ok"] and not first["notification_configured"]
    assert (root / archive).read_bytes() == payload
    assert (root / archive).stat().st_mode & 0o777 == 0o600
    assert not (root / "outbox").exists()

    remote.broken = True
    for now in (1100, 1200):
        result = module.run(config, remote, probe=lambda _: False, now=now)
        assert not result["ok"] and "source_unreachable" in result["errors"]
        assert (root / archive).read_bytes() == payload
    assert len(list((root / "outbox").glob("*.json"))) == 1

    remote.broken = False
    assert module.run(config, remote, probe=lambda _: True, now=1300)["ok"]
    events = [json.loads(p.read_text()) for p in (root / "outbox").glob("*.json")]
    assert sorted(e["event"] for e in events) == ["fault", "recovery"]
    assert remote.downloads == 1


def test_corrupt_transfer_is_not_published(setup):
    module, config, remote, _, archive = setup
    remote.corrupt = True
    result = module.run(config, remote, probe=lambda _: True, now=1000)
    root = Path(config["directory"])
    assert "backup_pull_failed" in result["errors"]
    assert not (root / archive).exists()
    assert not (root / "latest.json").exists()
    assert not list(root.glob("*.partial"))
    remote.corrupt = False
    assert module.run(config, remote, probe=lambda _: True, now=1100)["ok"]


def test_old_backup_and_missing_source_checks_raise_faults(setup):
    module, config, remote, _, _ = setup
    result = module.run(config, remote, probe=lambda _: True, now=31000)
    assert {"source_backup_stale", "source_monitor_stale"} <= set(result["errors"])


@pytest.mark.parametrize("command", ["", "id", "status; id", "latest && id",
                                      "archive ../../secrets/mem0_key", "archive snapshot-X.tar.gz",
                                      "archive snapshot-20260913T080000Z.tar.gz; id"])
def test_exporter_rejects_shell_and_path_injection(tmp_path, command):
    with pytest.raises(ValueError):
        load("backup-export").export(command, tmp_path, io.BytesIO())


def test_exporter_reads_only_allowed_files_and_rejects_symlink(tmp_path):
    module = load("backup-export")
    backups = tmp_path / "backups"
    backups.mkdir()
    archive = "snapshot-20260913T080000Z.tar.gz"
    (backups / archive).write_bytes(b"snapshot")
    (backups / "latest.json").write_text(json.dumps({"archive": archive, "created": 1, "sha256": "a" * 64}))
    output = io.BytesIO()
    module.export("latest", tmp_path, output)
    assert json.loads(output.getvalue())["size"] == 8
    output = io.BytesIO()
    module.export("archive " + archive, tmp_path, output)
    assert output.getvalue() == b"snapshot"
    (backups / archive).unlink()
    (backups / archive).symlink_to(backups / "latest.json")
    with pytest.raises(ValueError):
        module.export("archive " + archive, tmp_path, io.BytesIO())

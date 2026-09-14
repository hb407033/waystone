"""Read-only forced SSH command for the offsite node; never invokes a shell."""
import json
import os
import re
import shutil
import sys
from pathlib import Path

ARCHIVE = re.compile(r"snapshot-\d{8}T\d{6}Z\.tar\.gz")


def export(command, root, output):
    if command == "status":
        path = root / "ops/status.json"
    elif command == "latest":
        path = root / "backups/latest.json"
    elif command.startswith("archive ") and ARCHIVE.fullmatch(command[8:]):
        path = root / "backups" / command[8:]
    else:
        raise ValueError("Unsupported backup read")
    if path.is_symlink() or not path.is_file():
        raise ValueError("Backup file unavailable")
    if path.suffix == ".json":
        if path.stat().st_size > 16384:
            raise ValueError("Metadata too large")
        data = path.read_bytes()
        metadata = json.loads(data)
        if command == "latest":
            archive = metadata["archive"]
            if not ARCHIVE.fullmatch(archive):
                raise ValueError("Unexpected archive name")
            backup = root / "backups" / archive
            if backup.is_symlink() or not backup.is_file():
                raise ValueError("Backup file unavailable")
            metadata["size"] = backup.stat().st_size
            data = json.dumps(metadata).encode()
        output.write(data)
    else:
        with path.open("rb") as stream:
            shutil.copyfileobj(stream, output)


if __name__ == "__main__":
    try:
        export(os.environ.get("SSH_ORIGINAL_COMMAND", ""),
               Path("/opt/waystone"), sys.stdout.buffer)
    except Exception:
        print("Backup read denied or unavailable", file=sys.stderr)
        sys.exit(1)

"""Deliver queued monitoring events through explicitly configured TLS SMTP.

Credentials are read only at runtime on the backup host and never logged.
"""
import fcntl
import importlib.util
import json
import os
import smtplib
import ssl
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

LABELS = {
    "public_not_ready": "公网服务不可达或检索未就绪",
    "source_unreachable": "无法读取主服务器监测状态",
    "source_monitor_stale": "主服务器监测状态过期",
    "source_backup_stale": "主服务器备份超过八小时未更新",
    "source_backup_fresh": "主服务器备份检查失败",
    "source_retrieval_ready": "主服务器检索检查失败",
    "source_restore_recent": "恢复验证已过期或失败",
    "source_disk_available": "主服务器剩余磁盘不足",
    "backup_pull_failed": "异地备份拉取或完整性验证失败",
    "offsite_disk_low": "备份机剩余磁盘不足",
    "offsite_state_invalid": "备份机历史监测状态无法读取",
}


def message(config, event, event_id):
    kind = {"fault": "故障", "recovery": "恢复", "test": "通知测试"}[event["event"]]
    msg = EmailMessage()
    msg["Subject"] = "[Waystone] " + kind
    msg["From"] = config["sender"]
    msg["To"] = config["recipient"]
    msg["Message-ID"] = "<waystone-" + event_id + "@" + config["sender"].rsplit("@", 1)[1] + ">"
    msg["Date"] = datetime.now(timezone.utc)
    detail = "；".join(LABELS.get(code, "监测异常") for code in event["errors"])
    if not detail:
        detail = "邮件通知通道测试。" if event["event"] == "test" else "服务和异地备份检查恢复正常。"
    occurred = datetime.fromtimestamp(event["checked"], timezone.utc).isoformat()
    msg.set_content("项目记忆服务：" + kind + "\n" + detail + "\n发生时间（UTC）：" + occurred +
                    "\n" +
                    "本通知由云端节点发送，不依赖个人电脑。邮件不包含记忆内容或备份附件。\n")
    return msg


def send(config, event, event_id):
    password = Path(config["password_file"]).read_text().strip()
    if not password:
        raise ValueError("SMTP credential is not configured")
    with smtplib.SMTP_SSL(config["host"], config.get("port", 465),
                          timeout=20, context=ssl.create_default_context()) as smtp:
        smtp.login(config["username"], password)
        if smtp.send_message(message(config, event, event_id)):
            raise RuntimeError("Recipient rejected")


def deliver(config, root, sender=send, now=None):
    os.umask(0o077)
    now = time.time() if now is None else now
    if not config.get("enabled"):
        return {"configured": False, "sent": 0, "ok": False}
    sent = 0
    with (root / "notify.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        history = root / "notifications"
        history.mkdir(mode=0o700, exist_ok=True)
        events = [(p, json.loads(p.read_text())) for p in (root / "outbox").glob("*.json")]
        for path, event in sorted(events, key=lambda item: (item[1]["checked"], item[0].name))[:10]:
            if event["checked"] < config["enabled_after"]:
                # Keep pre-activation events for audit without mailing setup noise.
                path.replace(history / ("before-activation-" + path.name))
                continue
            try:
                sender(config, event, path.stem)
            except Exception:
                return {"configured": True, "sent": sent, "ok": False,
                        "error": "smtp_delivery_failed", "checked": now}
            path.replace(history / path.name)
            sent += 1
        for path in history.glob("*.json"):
            if path.stat().st_mtime < now - 28 * 86400:
                path.unlink()
    return {"configured": True, "sent": sent, "ok": True, "checked": now}


if __name__ == "__main__":
    try:
        config_file = Path("/etc/waystone-offsite/email.json")
        config = json.loads(config_file.read_text()) if config_file.exists() else {"enabled": False}
        root = Path("/var/lib/waystone-offsite")
        result = deliver(config, root)
        spec = importlib.util.spec_from_file_location("offsite_monitor", Path(__file__).with_name("offsite-monitor.py"))
        monitor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(monitor)
        monitor.write_json(root / "notification-status.json", result)
        print(json.dumps(result))
        raise SystemExit(0 if result.get("ok") or not result.get("configured") else 1)
    except Exception:
        print('{"ok":false,"error":"notification_execution_failed"}')
        raise SystemExit(1)

import importlib.util
import json
from pathlib import Path


def module():
    spec = importlib.util.spec_from_file_location("notify", Path("deploy/offsite-notify.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_email_failure_retains_event_then_delivers_in_order(tmp_path):
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    for name, checked, event in (("z", 100, "fault"), ("a", 110, "recovery")):
        (outbox / (name + ".json")).write_text(json.dumps({"checked": checked, "event": event, "errors": []}))
    config = {"enabled": True, "enabled_after": 90}
    def fail(*args): raise ValueError("secret must not escape")
    result = module().deliver(config, tmp_path, sender=fail, now=120)
    assert result["error"] == "smtp_delivery_failed" and result["sent"] == 0
    assert len(list(outbox.glob("*.json"))) == 2
    sent = []
    result = module().deliver(config, tmp_path, sender=lambda c, e, i: sent.append(i), now=130)
    assert result["ok"] and result["sent"] == 2 and sent == ["z", "a"]
    assert not list(outbox.glob("*.json"))


def test_disabled_and_pre_activation_do_not_send(tmp_path):
    def forbidden(*args): raise AssertionError("must not send")
    assert not module().deliver({"enabled": False}, tmp_path, sender=forbidden)["configured"]
    (tmp_path / "outbox").mkdir()
    (tmp_path / "outbox/old.json").write_text(json.dumps({"checked": 10, "event": "fault", "errors": []}))
    result = module().deliver({"enabled": True, "enabled_after": 20}, tmp_path, sender=forbidden, now=30)
    assert result["ok"] and result["sent"] == 0
    assert (tmp_path / "notifications/before-activation-old.json").exists()


def test_message_has_no_memory_content_or_credentials():
    config = {"sender": "sender@example.invalid", "recipient": "recipient@example.invalid", "password_file": "/private/password"}
    event = {"checked": 100, "event": "fault", "errors": ["backup_pull_failed"], "content": "private-memory"}
    msg = module().message(config, event, "test-event")
    body = msg.get_content()
    assert "异地备份拉取" in body
    assert "private-memory" not in body and "/private/password" not in body
    assert msg["To"] == config["recipient"]
    assert msg["Message-ID"] == "<waystone-test-event@example.invalid>"

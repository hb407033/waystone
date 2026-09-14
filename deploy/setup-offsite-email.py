"""Run interactively on the backup host; never place the SMTP code in chat or argv."""
import argparse
import getpass
import json
import os
import pwd
import smtplib
import ssl
import subprocess
import time
import uuid
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="配置 Waystone 邮件告警，并发送一封测试邮件")
    parser.add_argument("--host", required=True, help="SMTP 服务器地址，只支持 SSL 端口")
    parser.add_argument("--port", type=int, default=465)
    parser.add_argument("--sender", required=True)
    parser.add_argument("--recipient", required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError("请使用管理员身份运行")
    for address in (args.sender, args.recipient):
        if address.count("@") != 1 or any(c.isspace() for c in address):
            raise ValueError("邮箱地址格式不正确")
    print("发件邮箱：" + args.sender + "；收件邮箱：" + args.recipient)
    print("配置后将发送一封测试邮件，随后仅发送故障变化和恢复通知。")
    if input("确认启用并发送测试邮件？输入 YES：").strip() != "YES":
        print("未启用邮件通知。")
        return
    password = getpass.getpass("SMTP 授权码（隐藏输入，非网页登录密码）：").strip()
    if not password:
        raise ValueError("授权码不能为空")
    # Verify authentication before replacing an existing configuration.
    with smtplib.SMTP_SSL(args.host, args.port, timeout=20, context=ssl.create_default_context()) as smtp:
        smtp.login(args.sender, password)
    account = pwd.getpwnam("waystone-offsite")
    os.umask(0o077)
    config_root = Path("/etc/waystone-offsite")
    root = Path("/var/lib/waystone-offsite")
    password_file = config_root / "smtp-password"
    temporary = config_root / "smtp-password.tmp"
    temporary.write_text(password)
    temporary.chmod(0o600)
    os.chown(temporary, account.pw_uid, account.pw_gid)
    temporary.replace(password_file)
    enabled = time.time()
    config = {"enabled": True, "enabled_after": enabled, "host": args.host, "port": args.port,
              "sender": args.sender, "username": args.sender, "recipient": args.recipient,
              "password_file": str(password_file)}
    temporary = config_root / "email.tmp"
    temporary.write_text(json.dumps(config))
    temporary.chmod(0o640)
    os.chown(temporary, 0, account.pw_gid)
    temporary.replace(config_root / "email.json")
    outbox = root / "outbox"
    outbox.mkdir(mode=0o700, exist_ok=True)
    os.chown(outbox, account.pw_uid, account.pw_gid)
    event = outbox / (uuid.uuid4().hex + ".json")
    event.write_text(json.dumps({"checked": time.time(), "event": "test", "errors": []}))
    os.chown(event, account.pw_uid, account.pw_gid)
    event.chmod(0o600)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", "waystone-notify.timer"], check=True)
    subprocess.run(["systemctl", "start", "waystone-notify.service"], check=True)
    print("邮件告警已启用，请在收件箱或垃圾邮件中确认测试邮件。")


if __name__ == "__main__":
    try:
        main()
    except (Exception, KeyboardInterrupt):
        print("配置或测试发送未完成。请核对 SMTP 服务、授权码与网络；程序未输出授权码。")
        raise SystemExit(1)

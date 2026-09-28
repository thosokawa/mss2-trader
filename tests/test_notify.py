from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app import notify
from app.config import NotifyCfg


def _cfg(**kw):
    return SimpleNamespace(notify=NotifyCfg(**kw))


EMAIL = {
    "email_to": "me@example.com, other@example.com",
    "smtp_user": "sender@gmail.com",
    "smtp_password": "app-pass",
}


def test_dry_run_sends_nothing():
    with patch.object(notify, "get_config", return_value=_cfg(dry_run=True, **EMAIL)), \
            patch("smtplib.SMTP") as smtp:
        assert notify.send("hello")
    smtp.assert_not_called()


def test_email_sent_via_starttls_when_configured():
    text = notify.format_signal("MACD", "9984", "ソフトバンクグループ", "BUY", 6277.0, "MACD上抜け")
    with patch.object(notify, "get_config", return_value=_cfg(dry_run=False, **EMAIL)), \
            patch("smtplib.SMTP") as smtp, patch("httpx.post") as post:
        conn = smtp.return_value.__enter__.return_value
        assert notify.send(text)

    post.assert_not_called()  # Slack 未設定
    smtp.assert_called_once_with("smtp.gmail.com", 587, timeout=15)
    conn.starttls.assert_called_once()
    conn.login.assert_called_once_with("sender@gmail.com", "app-pass")
    msg = conn.send_message.call_args.args[0]
    assert msg["Subject"] == "[mss2] 🟢 買い 9984 ソフトバンクグループ  @6,277.0"
    assert msg["To"] == "me@example.com, other@example.com"
    assert "*" not in msg.get_content()
    assert "根拠: MACD上抜け" in msg.get_content()


def test_email_skipped_when_password_missing():
    cfg = _cfg(dry_run=False, email_to="me@example.com", smtp_user="sender@gmail.com")
    with patch.object(notify, "get_config", return_value=cfg), patch("smtplib.SMTP") as smtp:
        assert notify.send("hello")  # 何も設定されていなければログだけ
    smtp.assert_not_called()


def test_email_failure_returns_false():
    with patch.object(notify, "get_config", return_value=_cfg(dry_run=False, **EMAIL)), \
            patch("smtplib.SMTP", side_effect=OSError("connection refused")):
        assert not notify.send("hello")


def test_slack_and_email_both_sent():
    cfg = _cfg(dry_run=False, slack_webhook_url="https://hooks.example/x", **EMAIL)
    with patch.object(notify, "get_config", return_value=cfg), \
            patch("smtplib.SMTP") as smtp, patch("httpx.post", return_value=MagicMock()) as post:
        assert notify.send("hello")
    post.assert_called_once()
    smtp.return_value.__enter__.return_value.send_message.assert_called_once()

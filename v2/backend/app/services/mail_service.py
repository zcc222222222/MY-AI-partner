"""TLS SMTP transport. Never logs recipient, credentials, message or verification code."""
from email.message import EmailMessage
import smtplib
import ssl
from fastapi import HTTPException
from ..config import settings


def require_mail():
    if not settings.mail_ready:
        raise HTTPException(status_code=503, detail="邮件服务尚未配置，请联系管理员")


def send_registration_code(email: str, code: str):
    require_mail()
    message = EmailMessage()
    message["Subject"] = "爱弥斯 AI 助手 · 注册验证码"
    message["From"] = settings.smtp_from
    message["To"] = email
    message.set_content(f"你的注册验证码是：{code}\n\n验证码10分钟内有效，仅可使用一次。请勿将验证码告诉他人。\n如非本人操作，请忽略此邮件。")
    context = ssl.create_default_context()
    try:
        if settings.smtp_security == "ssl":
            client = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=10, context=context)
        else:
            client = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10)
        with client:
            if settings.smtp_security == "starttls":
                client.ehlo()
                client.starttls(context=context)
                client.ehlo()
            client.login(settings.smtp_username, settings.smtp_password)
            if client.send_message(message):
                raise smtplib.SMTPException("Recipient refused")
    except (OSError, smtplib.SMTPException):
        raise HTTPException(status_code=503, detail="邮件发送失败，请稍后重试或联系管理员") from None

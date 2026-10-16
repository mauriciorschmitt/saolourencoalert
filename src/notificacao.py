"""
notificacao.py
==============
Envio de avisos por Telegram, e-mail e WhatsApp (CallMeBot).

Cada canal é opcional e se ativa sozinho quando seus secrets existem. Uma
falha de envio é registrada no log e não interrompe a execução: perder um
aviso é ruim, mas perder os dados da passagem porque o SMTP caiu é pior.

Secrets:
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID       (vários IDs separados por vírgula)
    EMAIL_ADDRESS, EMAIL_APP_PASSWORD, ALERT_EMAIL_TO   (vários destinatários
        separados por vírgula)
    EMAIL_SMTP_HOST, EMAIL_SMTP_PORT           (em branco: Gmail, porta 465)
    WHATSAPP_PHONE, WHATSAPP_APIKEY

Gmail:   EMAIL_SMTP_HOST=smtp.gmail.com       EMAIL_SMTP_PORT=465  (SSL)
Outlook: EMAIL_SMTP_HOST=smtp.office365.com   EMAIL_SMTP_PORT=587  (STARTTLS)
"""

from __future__ import annotations

import logging
import os
import smtplib
import time
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Optional

import requests

log = logging.getLogger(__name__)

MAX_RETRIES = 4
BACKOFF_BASE = 2


def _lista(nome: str) -> List[str]:
    return [v.strip() for v in (os.environ.get(nome) or "").split(",") if v.strip()]


def _post(url: str, **kwargs) -> requests.Response:
    """POST com nova tentativa em 429, 5xx e erro de rede."""
    ultimo = None
    for tentativa in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, **kwargs)
            if resp.status_code == 429:
                espera = int(resp.headers.get("Retry-After", BACKOFF_BASE * tentativa))
                log.warning("Limite de requisições (429); aguardando %ds.", espera)
                time.sleep(espera)
                continue
            if resp.status_code >= 500:
                log.warning("Servidor %d na tentativa %d/%d.",
                            resp.status_code, tentativa, MAX_RETRIES)
                time.sleep(BACKOFF_BASE ** tentativa)
                continue
            return resp
        except (requests.ConnectionError, requests.Timeout) as exc:
            ultimo = exc
            time.sleep(BACKOFF_BASE ** tentativa)
    raise RuntimeError(f"Todas as {MAX_RETRIES} tentativas falharam: {ultimo}")


# ── canais ─────────────────────────────────────────────────────────────────

def canais_ativos() -> List[str]:
    ativos = []
    if os.environ.get("TELEGRAM_BOT_TOKEN") and _lista("TELEGRAM_CHAT_ID"):
        ativos.append("Telegram")
    if (os.environ.get("EMAIL_ADDRESS") and os.environ.get("EMAIL_APP_PASSWORD")
            and _lista("ALERT_EMAIL_TO")):
        ativos.append("E-mail")
    if os.environ.get("WHATSAPP_PHONE") and os.environ.get("WHATSAPP_APIKEY"):
        ativos.append("WhatsApp")
    return ativos


def telegram(mensagem: str, imagem: Optional[bytes] = None) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chats = _lista("TELEGRAM_CHAT_ID")
    if not token or not chats:
        log.info("Telegram não configurado; pulando.")
        return
    for chat_id in chats:
        try:
            # Legenda de foto aceita no máximo 1024 caracteres. Com alerta das
            # macrófitas e da água juntos a mensagem pode passar disso, e cortar
            # perderia justamente o fim — o bloco da água. Nesse caso a foto
            # vai sozinha e o texto segue em mensagem própria.
            cabe = len(mensagem) <= 1024
            if imagem:
                resp = _post(
                    f"https://api.telegram.org/bot{token}/sendPhoto",
                    data={"chat_id": chat_id, **({"caption": mensagem} if cabe else {})},
                    files={"photo": ("panel.png", imagem, "image/png")},
                    timeout=60,
                )
                if not resp.ok:
                    log.error("Telegram (foto) falhou para %s: %s", chat_id, resp.text[:300])
            if not imagem or not cabe:
                resp = _post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    data={"chat_id": chat_id, "text": mensagem[:4096]},
                    timeout=30,
                )
            if resp.ok:
                log.info("Telegram enviado para %s.", chat_id)
            else:
                log.error("Telegram falhou para %s: %s", chat_id, resp.text[:300])
        except Exception as exc:
            log.error("Erro Telegram %s: %s", chat_id, exc)


def whatsapp(mensagem: str) -> None:
    """Apenas texto: a imagem chega pelo Telegram e pelo e-mail."""
    phone = os.environ.get("WHATSAPP_PHONE")
    apikey = os.environ.get("WHATSAPP_APIKEY")
    if not phone or not apikey:
        log.info("WhatsApp não configurado; pulando.")
        return
    try:
        resp = requests.get(
            "https://api.callmebot.com/whatsapp.php",
            params={"phone": phone, "text": mensagem, "apikey": apikey},
            timeout=30,
        )
        if resp.ok:
            log.info("WhatsApp enviado para %s.", phone)
        else:
            log.error("WhatsApp falhou: %d %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        log.error("Erro WhatsApp: %s", exc)


def email(assunto: str, mensagem: str, imagem: Optional[bytes] = None) -> None:
    addr = os.environ.get("EMAIL_ADDRESS")
    senha = os.environ.get("EMAIL_APP_PASSWORD")
    destinos = _lista("ALERT_EMAIL_TO")
    if not addr or not senha or not destinos:
        log.info("E-mail não configurado; pulando.")
        return
    host = os.environ.get("EMAIL_SMTP_HOST") or "smtp.gmail.com"
    porta = int(os.environ.get("EMAIL_SMTP_PORT") or "465")

    if imagem:
        msg = MIMEMultipart()
        msg.attach(MIMEText(mensagem))
        anexo = MIMEImage(imagem, _subtype="png")
        anexo.add_header("Content-Disposition", "attachment",
                         filename="reservatorio_panel.png")
        msg.attach(anexo)
    else:
        msg = MIMEText(mensagem)
    msg["Subject"] = assunto
    msg["From"] = addr
    msg["To"] = ", ".join(destinos)

    try:
        if porta == 587:
            with smtplib.SMTP(host, porta, timeout=30) as srv:
                srv.starttls()
                srv.login(addr, senha)
                srv.sendmail(addr, destinos, msg.as_string())
        else:
            with smtplib.SMTP_SSL(host, porta, timeout=30) as srv:
                srv.login(addr, senha)
                srv.sendmail(addr, destinos, msg.as_string())
        log.info("E-mail enviado para: %s", ", ".join(destinos))
    except Exception as exc:
        log.error("Erro e-mail: %s", exc)


def suprimido() -> bool:
    """NOTIFICAR=0 suprime o envio — para executar manualmente, ou testar uma
    alteração, sem disparar avisos para os destinatários."""
    return os.environ.get("NOTIFICAR", "1").strip() == "0"


def todos(assunto: str, mensagem: str, imagem: Optional[bytes] = None) -> None:
    """Envia pelos canais configurados, a menos que suprimido()."""
    if suprimido():
        log.info("NOTIFICAR=0: avisos suprimidos nesta execução.")
        return
    telegram(mensagem, imagem)
    email(assunto, mensagem, imagem)
    whatsapp(mensagem)

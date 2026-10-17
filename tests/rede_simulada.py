"""
Rede simulada para os testes: Sentinel Hub, Open-Meteo, Telegram e SMTP.

Nenhum teste toca a rede de verdade. `instalar()` substitui requests.post,
requests.get e smtplib, e registra cada chamada em CHAMADAS para os testes
conferirem o que teria sido enviado.

A primeira consulta estatística devolve 401, para exercitar a renovação do
token em todo teste de ponta a ponta.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import smtplib

import requests
from PIL import Image

CHAMADAS: dict = {}


def zerar() -> None:
    CHAMADAS.clear()
    CHAMADAS.update({"token": 0, "stats": 0, "stats401": 0, "process": 0,
                     "open_meteo": 0, "telegram": [], "email": []})


class Resp:
    def __init__(self, status=200, js=None, content=b""):
        self.status_code, self._js, self.content = status, js, content
        self.ok = status < 400
        self.text = json.dumps(js) if js is not None else ""
        self.headers = {}

    def json(self):
        return self._js


def _png() -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (64, 32), (10, 120, 200)).save(b, "PNG")
    return b.getvalue()


def _stats(payload: dict) -> dict:
    """Uma passagem a cada 5 dias, alternando cena limpa, parcial e nublada.
    Serve para óptico e radar: devolve estatística para cada saída pedida."""
    tr = payload["aggregation"]["timeRange"]
    d = dt.date.fromisoformat(tr["from"][:10])
    fim = dt.date.fromisoformat(tr["to"][:10])
    saidas = list(payload["calculations"])
    dados, k = [], 0
    while d <= fim:
        if (d - dt.date(2016, 1, 1)).days % 5 == 0:
            validos = [7334, 7000, 3000, 400][k % 4]
            k += 1
            outs = {}
            for s in saidas:
                media = 0.5 if s.startswith("cls_") else (-25.0 if s.endswith("_db") else 0.06)
                outs[s] = {"bands": {"B0": {"stats": {
                    "mean": media, "sampleCount": 20000, "noDataCount": 20000 - validos,
                    "percentiles": {"50.0": media}}}}}
            dados.append({"interval": {"from": f"{d}T00:00:00Z"}, "outputs": outs})
        d += dt.timedelta(days=1)
    return {"data": dados}


def _post(url, **kw):
    if "openid-connect/token" in url:
        CHAMADAS["token"] += 1
        return Resp(js={"access_token": f"tok{CHAMADAS['token']}"})
    if url.endswith("/statistics"):
        CHAMADAS["stats"] += 1
        if CHAMADAS["stats"] == 1:
            CHAMADAS["stats401"] += 1
            return Resp(401, js={"error": "token expirado"})
        return Resp(js=_stats(kw["json"]))
    if url.endswith("/process"):
        CHAMADAS["process"] += 1
        return Resp(content=_png())
    if "api.telegram.org" in url:
        dados = kw.get("data", {})
        CHAMADAS["telegram"].append({"chat": dados.get("chat_id"), "foto": "files" in kw,
                                     "legenda": dados.get("caption"), "texto": dados.get("text")})
        return Resp(js={"ok": True})
    raise AssertionError(f"POST inesperado: {url}")


def _get(url, params=None, **kw):
    if "open-meteo.com" not in url:
        raise AssertionError(f"GET inesperado: {url}")
    CHAMADAS["open_meteo"] += 1
    params = params or {}
    if "start_date" in params:
        ini = dt.date.fromisoformat(params["start_date"])
        fim = dt.date.fromisoformat(params["end_date"])
    else:
        hoje = dt.date.today()
        ini = hoje - dt.timedelta(days=int(params.get("past_days", 0)))
        fim = hoje + dt.timedelta(days=int(params.get("forecast_days", 0)))
    dias = [(ini + dt.timedelta(days=i)).isoformat() for i in range((fim - ini).days + 1)]
    return Resp(js={"daily": {"time": dias,
                              "temperature_2m_mean": [18.0 + (i % 7) * 0.3 for i in range(len(dias))],
                              "precipitation_sum": [float(i % 4) for i in range(len(dias))]}})


class _SMTP:
    def __init__(self, host, porta, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def starttls(self):
        pass

    def login(self, *a):
        pass

    def sendmail(self, de, para, msg):
        CHAMADAS["email"].append({"para": para, "anexo": "Content-Disposition: attachment" in msg})


def instalar() -> None:
    zerar()
    requests.post = _post
    requests.get = _get
    smtplib.SMTP_SSL = _SMTP
    smtplib.SMTP = _SMTP

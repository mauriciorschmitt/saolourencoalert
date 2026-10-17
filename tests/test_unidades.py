"""Peças isoladas: evalscript, token, notificações e configuração."""
import requests

import rede_simulada as rede
from src import evalscripts, notificacao, sentinelhub as sh
from src.monitor import carregar_config

CFG = carregar_config()


# ── evalscript ─────────────────────────────────────────────────────────────

def _linhas_cls(js):
    return {l.split(":")[0].strip(): l.strip() for l in js.splitlines() if l.strip().startswith("cls_")}


def test_classificacao_calibrada_pelo_radar():
    js = evalscripts.render(list(CFG["indices"]), CFG["classificacao"],
                            CFG["qualidade"]["scl_descartadas"])
    cls = _linhas_cls(js)
    assert "((ndvi) > 0.2) && ((fai) > 0.02)" in cls["cls_cobertura_flutuante"]
    assert "((ndvi) < 0.1)" in cls["cls_agua"]


def test_condicao_unica_continua_funcionando():
    js = evalscripts.render(["ndci"], {"x": {"indice": "ndci", "limiar": 0.1, "operador": "maior"}}, [3])
    assert "(((ndci) > 0.1)) ? 1 : 0" in _linhas_cls(js)["cls_x"]


def test_indice_usado_so_na_classificacao_e_calculado():
    js = evalscripts.render(["ndvi"], {"x": {"indice": "fai", "limiar": 0.0, "operador": "maior"}}, [3])
    assert "const fai =" in js


# ── token ──────────────────────────────────────────────────────────────────

def test_credencial_renova_em_401(monkeypatch):
    rede.instalar()
    monkeypatch.setattr(sh.requests, "post", requests.post)
    monkeypatch.setenv("SH_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.setenv("SH_CLIENT_SECRET", "s")
    cred = sh.Credencial()
    payload = {"aggregation": {"timeRange": {"from": "2026-10-01T00:00:00Z",
                                             "to": "2026-10-10T00:00:00Z"}},
               "calculations": {"ndvi": {}}}
    dados = sh._post_com_retry(sh.STATS_URL, cred, payload)
    assert dados["data"] and rede.CHAMADAS["stats401"] == 1 and rede.CHAMADAS["token"] == 2


def test_credencial_renova_por_idade(monkeypatch):
    chamadas = []
    monkeypatch.setattr(sh, "get_token", lambda: chamadas.append(1) or f"t{len(chamadas)}")
    cred = sh.Credencial()
    cred.valor()
    cred._obtido_em -= sh.Credencial.VIDA_UTIL_S + 1
    assert cred.valor() == "t2"


# ── notificações ───────────────────────────────────────────────────────────

def _telegram(monkeypatch):
    rede.instalar()
    monkeypatch.setattr(notificacao.requests, "post", requests.post)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "b")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1, 2")


def test_telegram_vai_para_todos_os_chats(monkeypatch):
    _telegram(monkeypatch)
    notificacao.telegram("oi", b"png")
    assert [c["chat"] for c in rede.CHAMADAS["telegram"]] == ["1", "2"]


def test_mensagem_longa_nao_e_cortada(monkeypatch):
    # legenda de foto aceita 1024 caracteres: a foto vai sem legenda e o
    # texto inteiro segue em mensagem própria
    _telegram(monkeypatch)
    notificacao.telegram("x" * 1500, b"png")
    foto, texto = rede.CHAMADAS["telegram"][:2]
    assert foto["foto"] and foto["legenda"] is None and len(texto["texto"]) == 1500


def test_notificar_zero_suprime(monkeypatch):
    _telegram(monkeypatch)
    monkeypatch.setenv("NOTIFICAR", "0")
    notificacao.todos("a", "b")
    assert rede.CHAMADAS["telegram"] == []


# ── configuração ───────────────────────────────────────────────────────────

def test_mesmo_recuo_em_todos_os_modulos():
    assert CFG["macrofitas"]["recuo_borda_m"] == CFG["aquisicao"]["recuo_borda_m"] == CFG["radar"]["recuo_borda_m"]

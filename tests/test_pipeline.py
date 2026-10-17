"""Ponta a ponta: o workflow agendado inteiro, com a rede simulada."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def resultado(tmp_path_factory):
    copia = tmp_path_factory.mktemp("repo") / "repo"
    shutil.copytree(RAIZ, copia, ignore=shutil.ignore_patterns(
        ".git", "__pycache__", ".pytest_cache", "archive"))
    # UTF-8 explícito: no Windows o subprocesso escreveria em cp1252 e as
    # mensagens (com acentos e emoji) não seriam lidas de volta.
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run([sys.executable, "tests/executar_pipeline.py"], cwd=copia, env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=600)
    linha = next((l for l in proc.stdout.splitlines() if l.startswith("RESULTADO ")), None)
    assert proc.returncode == 0 and linha, proc.stdout[-3000:] + proc.stderr[-3000:]
    return json.loads(linha[len("RESULTADO "):])


def test_token_renovado_apos_401(resultado):
    assert resultado["chamadas"]["stats401"] == 1
    assert resultado["chamadas"]["token"] >= 2


def test_todos_os_modulos_publicam(resultado):
    for arq in ("represa.csv", "radar.csv", "clima.json", "historico.csv",
                "ultimo.json", "alerta.json"):
        assert arq in resultado["arquivos"]


def test_so_o_alerta_envia_avisos(resultado):
    assert resultado["avisos_antes_do_alerta"] == 0


def test_politica_de_avisos(resultado):
    # dois chats: 1ª execução avisa, 2ª com nível estável não, manual força
    assert resultado["telegram_1a"] == 2
    assert resultado["telegram_2a"] == 2
    assert resultado["telegram_forcado"] == 4


def test_aviso_leva_painel_e_vai_para_todos(resultado):
    assert {t["chat"] for t in resultado["telegram"]} == {"111", "222"}
    assert all(t["foto"] for t in resultado["telegram"])
    assert resultado["email"][0]["para"] == ["b@x", "c@x"]
    assert resultado["email"][0]["anexo"]


def test_alerta_json_tem_o_que_a_pagina_usa(resultado):
    a = resultado["alerta"]
    for chave in ("nivel", "desde", "o_que_fazer", "fontes", "atualizado_em", "historico"):
        assert chave in a
    assert {f["id"] for f in a["fontes"]} == {"radar", "agua", "macrofitas"}

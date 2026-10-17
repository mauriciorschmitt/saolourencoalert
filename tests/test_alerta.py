"""Nível único: histerese, política de avisos e cruzamento entre fontes."""
import datetime as dt

import pytest

from src import alerta as A
from src.monitor import carregar_config

CFG = carregar_config()
SEXTA, SEGUNDA = dt.date(2026, 10, 16), dt.date(2026, 10, 19)


def fonte(id_, nivel, valida=True, texto="x"):
    return {"id": id_, "nome": id_, "nivel": nivel, "valida": valida, "texto": texto}


# ── histerese ──────────────────────────────────────────────────────────────

def test_sobe_na_hora():
    assert A.aplicar_histerese({"nivel_calculado": "ALERTA"}, {"nivel": "NORMAL"})[0] == "ALERTA"


def test_desce_so_na_terceira_execucao():
    ant, seq = {"nivel": "ALERTA"}, []
    for _ in range(3):
        n, p = A.aplicar_histerese({"nivel_calculado": "NORMAL"}, ant, 3)
        seq.append(n)
        ant = {"nivel": n, "pendente": p}
    assert seq == ["ALERTA", "ALERTA", "NORMAL"]


def test_sinal_que_volta_zera_a_contagem():
    n, p = A.aplicar_histerese({"nivel_calculado": "NORMAL"}, {"nivel": "ALERTA"}, 3)
    n, p = A.aplicar_histerese({"nivel_calculado": "ALERTA"}, {"nivel": n, "pendente": p}, 3)
    assert (n, p) == ("ALERTA", None)


def test_nao_pula_de_alerta_para_normal_se_houve_vigilancia_no_caminho():
    ant = {"nivel": "ALERTA"}
    for calc in ("VIGILANCIA", "NORMAL", "NORMAL"):
        n, p = A.aplicar_histerese({"nivel_calculado": calc}, ant, 3)
        ant = {"nivel": n, "pendente": p}
    assert n == "VIGILANCIA"


# ── política de avisos ─────────────────────────────────────────────────────

ANT = {"nivel": "NORMAL", "ultimo_aviso_em": "2026-10-12T10:00"}


@pytest.mark.parametrize("estado,dia,anterior,esperado", [
    ({"nivel": "NORMAL"}, SEXTA, ANT, None),
    ({"nivel": "NORMAL"}, SEGUNDA, ANT, "resumo semanal"),
    ({"nivel": "NORMAL"}, SEGUNDA, {"nivel": "NORMAL", "ultimo_aviso_em": "2026-10-19T09:30"}, None),
    ({"nivel": "VIGILANCIA", "mudou_de": "NORMAL"}, SEXTA, ANT, "mudança de nível"),
    ({"nivel": "NORMAL", "mudou_de": "VIGILANCIA"}, SEXTA, ANT, "mudança de nível"),
    ({"nivel": "ALERTA"}, SEXTA, {"nivel": "ALERTA"}, "lembrete: segue em alerta"),
    ({"nivel": "SEM_DADOS", "mudou_de": "NORMAL"}, SEXTA, ANT, None),
    ({"nivel": "NORMAL", "mudou_de": "SEM_DADOS"}, SEXTA, ANT, None),
    ({"nivel": "NORMAL"}, SEXTA, None, "primeira execução do nível único"),
])
def test_politica(estado, dia, anterior, esperado):
    assert A.motivo_aviso(estado, anterior, CFG, dia, False) == esperado


def test_manual_forca():
    assert A.motivo_aviso({"nivel": "NORMAL"}, ANT, CFG, SEXTA, True) == "execução manual"


# ── combinação das fontes ──────────────────────────────────────────────────

def test_ndvi_confirmado_com_radar_normal_vira_vigilancia():
    # os dois alertas falsos da série (mar/2018, jan/2020) foram assim
    assert A.combinar([fonte("radar", "NORMAL"), fonte("macrofitas", "ALERTA")], None)[0] == "VIGILANCIA"


def test_ndvi_confirmado_sem_radar_valido_decide_sozinho():
    assert A.combinar([fonte("radar", "NORMAL", valida=False),
                       fonte("macrofitas", "ALERTA")], None)[0] == "ALERTA"


def test_sem_fonte_valida():
    assert A.combinar([fonte("radar", "ALERTA", valida=False)], None)[0] == "SEM_DADOS"


def test_crescimento_acelerado_eleva_para_vigilancia():
    nivel, motivos = A.combinar([fonte("radar", "NORMAL")], {"acelerado": True, "ha_semana": 2.5})
    assert nivel == "VIGILANCIA" and any("2,5 ha/semana" in m for m in motivos)


# ── fontes ─────────────────────────────────────────────────────────────────

def test_vigilancia_da_clorofila_dura_60_dias():
    linhas = [{"data": "2025-02-12", "confianca": "ALTA", "nivel": "VIGILANCIA"},
              {"data": "2025-04-08", "confianca": "ALTA", "nivel": "NORMAL"}]
    assert A.fonte_agua(linhas, 30, dt.date(2025, 4, 10), 60)["nivel"] == "VIGILANCIA"
    assert A.fonte_agua(linhas, 30, dt.date(2025, 4, 20), 60)["nivel"] == "NORMAL"


def test_macrofitas_le_ultima_cena_qualificada_nao_a_de_menos_nuvem():
    # 29/03/2025: alerta confirmado numa cena com 38% de nuvem
    hist = [{"date": "2025-03-09", "confianca": "ALTA", "alerta_confirmado": "NAO", "status": "ATENÇÃO"},
            {"date": "2025-03-26", "confianca": "BAIXA", "alerta_confirmado": "NAO", "status": "ALERTA"},
            {"date": "2025-03-29", "confianca": "MEDIA", "alerta_confirmado": "SIM", "status": "ALERTA"}]
    f = A.fonte_macrofitas(hist, 30, dt.date(2025, 3, 31))
    assert (f["data"], f["nivel"]) == ("2025-03-29", "ALERTA")


def test_radar_atencao_so_conta_em_duas_passagens():
    um = [{"data": "2026-10-05", "frac_vegetacao_status": "NORMAL"},
          {"data": "2026-10-12", "frac_vegetacao_status": "ATENCAO"}]
    dois = [{"data": "2026-10-05", "frac_vegetacao_status": "ATENCAO"},
            {"data": "2026-10-12", "frac_vegetacao_status": "ATENCAO"}]
    hoje = dt.date(2026, 10, 14)
    assert A.fonte_radar(um, 21, hoje)["nivel"] == "NORMAL"
    assert A.fonte_radar(dois, 21, hoje)["nivel"] == "VIGILANCIA"


def test_mensagem_usa_virgula_decimal():
    estado = {"nivel": "NORMAL", "motivos": [], "crescimento": None, "clima": None,
              "fontes": [{"id": "radar", "nome": "Radar", "data": "2026-10-12", "nivel": "NORMAL",
                          "valida": True, "texto": "normal", "area_ha": 8.12}]}
    assert "8,1 ha" in A.compor(estado, CFG)

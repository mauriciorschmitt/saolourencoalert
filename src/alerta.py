#!/usr/bin/env python3
"""
alerta.py
=========
Nível único da represa (semáforo) e envio dos avisos.

    python -m src.alerta                 # aplica a política de avisos
    python -m src.alerta --forcar-aviso  # envia de qualquer jeito

Roda por último no workflow, depois dos módulos que geram dados, e só lê o
que eles publicaram em docs/data/:

    ultimo.json, passagens_30d.json   macrófitas (NDVI, Sentinel-2)
    represa.csv                       qualidade da água (Sentinel-2)
    radar.csv                         radar (Sentinel-1)
    clima.json                        condições ambientais (Open-Meteo)

Grava docs/data/alerta.json, que a página mostra no topo, e decide se avisa
por Telegram/e-mail/WhatsApp segundo a seção `avisos` de config.yaml. O
nível anterior é lido desse mesmo arquivo antes de ser sobrescrito.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import statistics
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import notificacao  # noqa: E402
from src.monitor import carregar_config  # noqa: E402

RAIZ = Path(__file__).resolve().parent.parent
DADOS = RAIZ / "docs" / "data"
SAIDA = DADOS / "alerta.json"
PAINEL = RAIZ / "docs" / "images" / "latest" / "panel.png"
PAINEL_URL = "mauriciorschmitt.github.io/saolourencoalert"

NIVEIS = ("NORMAL", "VIGILANCIA", "ALERTA")
RANK = {n: i for i, n in enumerate(NIVEIS)}
ROTULO = {"NORMAL": "NORMAL", "VIGILANCIA": "VIGILÂNCIA", "ALERTA": "ALERTA",
          "SEM_DADOS": "SEM LEITURA RECENTE"}
EMOJI = {"NORMAL": "🟢", "VIGILANCIA": "🟡", "ALERTA": "🔴", "SEM_DADOS": "⚪"}
O_QUE_FAZER = {
    "NORMAL": "Nenhuma ação necessária. O monitoramento segue automático.",
    "VIGILANCIA": "Sinal ainda não confirmado. Vale uma visita de campo nas "
                  "próximas semanas e acompanhar as próximas passagens.",
    "ALERTA": "Alteração confirmada por satélite. Recomenda-se verificação de "
              "campo com registro fotográfico e comunicar o responsável pela "
              "represa.",
    "SEM_DADOS": "Nenhum módulo tem leitura recente e confiável. Verifique a "
                 "execução do monitoramento na aba Actions.",
}

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("alerta")


# ── leitura ────────────────────────────────────────────────────────────────

def _json(nome: str) -> Optional[dict]:
    try:
        return json.loads((DADOS / nome).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _csv(nome: str) -> List[dict]:
    try:
        with open(DADOS / nome, encoding="utf-8", newline="") as f:
            return [r for r in csv.DictReader(f) if r.get("data")]
    except OSError:
        return []


def _num(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _idade(data: Optional[str], hoje: dt.date) -> Optional[int]:
    try:
        return (hoje - dt.date.fromisoformat(data)).days
    except (TypeError, ValueError):
        return None


# ── fontes ─────────────────────────────────────────────────────────────────
# Cada fonte devolve: data, nivel que ela sustenta, texto curto e se está
# dentro da validade. Uma fonte vencida aparece na página, mas não pesa.

def fonte_radar(linhas: List[dict], validade: int, hoje: dt.date) -> dict:
    if not linhas:
        return {"id": "radar", "nome": "Radar", "valida": False, "nivel": "NORMAL",
                "texto": "sem série"}
    u = linhas[-1]
    st = u.get("frac_vegetacao_status") or "SEM_REFERENCIA"
    anterior = linhas[-2].get("frac_vegetacao_status") if len(linhas) > 1 else None
    if u.get("frac_vegetacao_confirmado") == "SIM":
        nivel, motivo = "ALERTA", "alerta confirmado em duas passagens"
    elif st == "ALERTA":
        nivel, motivo = "VIGILANCIA", "alerta em uma passagem, aguardando a próxima"
    elif st == "ATENCAO" and anterior in ("ATENCAO", "ALERTA"):
        nivel, motivo = "VIGILANCIA", "atenção em duas passagens seguidas"
    else:
        nivel, motivo = "NORMAL", {"ATENCAO": "atenção em uma passagem"}.get(st, "normal")
    idade = _idade(u.get("data"), hoje)
    return {"id": "radar", "nome": "Radar", "data": u.get("data"), "nivel": nivel,
            "valida": idade is not None and idade <= validade,
            "texto": motivo, "area_ha": _num(u.get("area_vegetacao_ha")),
            "z": _num(u.get("frac_vegetacao_z"))}


def fonte_agua(linhas: List[dict], validade: int, hoje: dt.date,
               memoria_dias: int = 60) -> dict:
    # Os níveis da água só são atribuídos em cena de confiança ALTA: a
    # leitura que vale é a última limpa, não a última qualquer.
    limpas = [r for r in linhas if r.get("confianca") == "ALTA"]
    if not limpas:
        return {"id": "agua", "nome": "Qualidade da água", "valida": False,
                "nivel": "NORMAL", "texto": "sem cena limpa"}
    u = limpas[-1]
    n = u.get("nivel") or "NORMAL"
    # A vigilância da clorofila antecipou o evento de 2025 em ~65 dias, e por
    # definição some quando a cobertura passa de 30 ha — exatamente quando a
    # infestação aparece, mas antes de os índices a confirmarem. Por isso ela
    # vale por `memoria_dias` depois do último disparo, não só na cena dele.
    recente_vig = [r for r in limpas if r.get("nivel") == "VIGILANCIA"
                   and 0 <= (_idade(r.get("data"), hoje) or 10**6) <= memoria_dias]
    if n == "CONFIRMADO" and u.get("nivel_confirmado") == "SIM":
        nivel, motivo = "ALERTA", "alerta confirmado (índices concordantes)"
    elif n == "CONFIRMADO":
        nivel, motivo = "VIGILANCIA", "índices concordantes, aguardando confirmação"
    elif n == "VIGILANCIA":
        nivel, motivo = "VIGILANCIA", "clorofila acima do padrão (sinal precoce)"
    elif recente_vig:
        nivel = "VIGILANCIA"
        motivo = (f"clorofila acima do padrão em {_data_br(recente_vig[-1]['data'])} "
                  f"(sinal precoce; vigilância mantida por {memoria_dias} dias)")
    else:
        nivel, motivo = "NORMAL", "normal"
    idade = _idade(u.get("data"), hoje)
    return {"id": "agua", "nome": "Qualidade da água", "data": u.get("data"),
            "nivel": nivel, "valida": idade is not None and idade <= validade,
            "texto": motivo, "indices": u.get("indices_concordantes") or ""}


def fonte_macrofitas(ultimo: Optional[dict], validade: int, hoje: dt.date) -> dict:
    if not ultimo:
        return {"id": "macrofitas", "nome": "Macrófitas (NDVI)", "valida": False,
                "nivel": "NORMAL", "texto": "sem leitura"}
    conf = ultimo.get("confianca") or "BAIXA"
    # Alerta isolado do NDVI é ruidoso (31 disparos antes do surto): só o
    # confirmado por persistência pesa no nível.
    if ultimo.get("alerta_confirmado") == "SIM":
        nivel, motivo = "ALERTA", "NDVI acima do padrão em duas passagens"
    else:
        nivel = "NORMAL"
        motivo = {"ALERTA": "NDVI alto em uma passagem, não confirmado",
                  "ATENÇÃO": "NDVI levemente acima do padrão"}.get(ultimo.get("status"), "normal")
    idade = _idade(ultimo.get("date"), hoje)
    return {"id": "macrofitas", "nome": "Macrófitas (NDVI)", "data": ultimo.get("date"),
            "nivel": nivel, "texto": motivo, "confianca": conf,
            "valida": idade is not None and idade <= validade and conf != "BAIXA",
            "z": _num(ultimo.get("zscore")), "ndvi": _num(ultimo.get("ndvi")),
            "nuvens_pct": _num(ultimo.get("cloud_pct"))}


def crescimento(linhas: List[dict], cfg: dict) -> Optional[dict]:
    """Inclinação (ha/semana) da área no radar nas últimas passagens."""
    n, janela = cfg["passagens"], cfg["janela_dias"]
    pts = [(dt.date.fromisoformat(r["data"]), _num(r.get("area_vegetacao_ha")))
           for r in linhas[-n:]]
    pts = [(d, a) for d, a in pts if a is not None]
    if len(pts) < n or (pts[-1][0] - pts[0][0]).days > janela:
        return None
    xs = [(d - pts[0][0]).days / 7 for d, _ in pts]
    ys = [a for _, a in pts]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if not den:
        return None
    incl = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
    z = _num(linhas[-1].get("frac_vegetacao_z"))
    acelerado = incl >= cfg["limiar_ha_semana"] and z is not None and z >= cfg["z_minimo"]
    return {"ha_semana": round(incl, 2), "desde": pts[0][0].isoformat(),
            "ate": pts[-1][0].isoformat(), "passagens": len(pts),
            "acelerado": acelerado}


# ── nível ──────────────────────────────────────────────────────────────────

def combinar(fontes: List[dict], cresc: Optional[dict]) -> Tuple[str, List[str]]:
    """Nível do conjunto e motivos, a partir das fontes já avaliadas.

    Separado de calcular() para que o mesmo critério possa ser reproduzido
    sobre a série histórica (backtest), data a data.
    """
    por_id = {f["id"]: f for f in fontes}
    radar, macro = por_id.get("radar"), por_id.get("macrofitas")
    # NDVI confirmado com o radar normal na mesma época: na série 2016–2024
    # foram dois alertas falsos (mar/2018, fev/2020), ambos com o radar
    # normal. Sem radar válido, o NDVI decide sozinho.
    if (macro and macro["valida"] and macro["nivel"] == "ALERTA"
            and radar and radar["valida"] and radar["nivel"] == "NORMAL"):
        macro["nivel"] = "VIGILANCIA"
        macro["texto"] = "NDVI alto em duas passagens, mas o radar segue normal"

    validas = [f for f in fontes if f["valida"]]
    if not validas:
        return "SEM_DADOS", []
    nivel = max((f["nivel"] for f in validas), key=RANK.get)
    motivos = [f"{f['nome']}: {f['texto']}" for f in validas if f["nivel"] != "NORMAL"]
    if cresc and cresc["acelerado"]:
        motivos.append(f"Radar: área crescendo {_br(cresc['ha_semana'], 1)} ha/semana")
        if RANK[nivel] < RANK["VIGILANCIA"]:
            nivel = "VIGILANCIA"
    return nivel, motivos


def calcular(cfg: dict, hoje: dt.date) -> dict:
    au = cfg["alerta_unico"]
    val = au["validade_dias"]
    radar = _csv("radar.csv")
    fontes = [
        fonte_radar(radar, val["radar"], hoje),
        fonte_agua(_csv("represa.csv"), val["agua"], hoje,
                   au.get("memoria_vigilancia_dias", 60)),
        fonte_macrofitas(_json("ultimo.json"), val["macrofitas"], hoje),
    ]
    cresc = crescimento(radar, au["crescimento"]) if radar else None
    nivel, motivos = combinar(fontes, cresc)
    return {"nivel_calculado": nivel, "fontes": fontes, "crescimento": cresc,
            "motivos": motivos, "clima": _json("clima.json")}


def aplicar_histerese(calc: dict, anterior: Optional[dict],
                      execucoes: int = 3) -> Tuple[str, Optional[dict]]:
    """Sobe na hora; desce só depois de `execucoes` execuções seguidas abaixo
    do nível vigente (com duas por semana, 3 ≈ 10 dias). No meio do evento de
    2025 o sinal do radar oscilou por uma semana, e o semáforo desceria e
    subiria de novo, com dois avisos sem nada ter mudado na represa.

    Devolve (nível vigente, rebaixamento pendente {nivel, execucoes} ou None).
    """
    novo = calc["nivel_calculado"]
    vigente = (anterior or {}).get("nivel")
    if vigente not in RANK or novo not in RANK or RANK[novo] >= RANK[vigente]:
        return novo, None
    pend = (anterior or {}).get("pendente") or {}
    # Conta execuções abaixo do vigente; o destino é o mais alto visto na
    # sequência, para não pular de ALERTA direto a NORMAL por uma leitura.
    n = (pend.get("execucoes", 0) if isinstance(pend, dict) else 0) + 1
    alvo = novo
    if isinstance(pend, dict) and pend.get("nivel") in RANK and RANK[pend["nivel"]] > RANK[novo]:
        alvo = pend["nivel"]
    if n >= execucoes:
        return alvo, None
    return vigente, {"nivel": alvo, "execucoes": n}


# ── mensagem ───────────────────────────────────────────────────────────────

def _br(v: float, casas: int, sinal: bool = False) -> str:
    """Número com vírgula decimal, como se escreve em português."""
    return f"{v:{'+' if sinal else ''}.{casas}f}".replace(".", ",")


def _data_br(d: Optional[str]) -> str:
    try:
        return dt.date.fromisoformat(d).strftime("%d/%m")
    except (TypeError, ValueError):
        return "?"


def compor(estado: dict, cfg: dict) -> str:
    nivel = estado["nivel"]
    linhas = [f"{EMOJI[nivel]} Represa São Lourenço — {ROTULO[nivel]}"]
    if estado.get("mudou_de"):
        linhas[0] += f" (antes: {ROTULO.get(estado['mudou_de'], estado['mudou_de'])})"
    linhas.append(O_QUE_FAZER[nivel])
    if estado["motivos"]:
        linhas += ["", "Por quê:"] + [f"• {m}" for m in estado["motivos"]]

    linhas += ["", "Leituras:"]
    for f in estado["fontes"]:
        extra = ""
        if f["id"] == "radar" and f.get("area_ha") is not None:
            extra = f" · {_br(f['area_ha'], 1)} ha com assinatura de vegetação"
        if f["id"] == "macrofitas" and f.get("z") is not None and f.get("ndvi") is not None:
            extra = (f" · NDVI {_br(f['ndvi'], 3)} (z {_br(f['z'], 1, True)}), "
                     f"{_br(f.get('nuvens_pct') or 0, 0)}% de nuvens, "
                     f"confiança {f.get('confianca', '?').lower()}")
        validade = "" if f["valida"] else " (fora da validade, não pesa)"
        linhas.append(f"• {f['nome']} {_data_br(f.get('data'))}: {f['texto']}{extra}{validade}")

    c = estado.get("crescimento")
    if c:
        linhas.append(f"• Ritmo no radar: {_br(c['ha_semana'], 1, True)} ha/semana "
                      f"({_data_br(c['desde'])} a {_data_br(c['ate'])})")

    cl = estado.get("clima") or {}
    if cl.get("chuva") and cl.get("temperatura"):
        ch, te = cl["chuva"], cl["temperatura"]
        cond = {"MUITO_FAVORAVEL": "muito favoráveis", "FAVORAVEL": "favoráveis",
                "NEUTRA": "neutras"}.get(cl.get("condicao"), "?")
        linhas += ["", f"🌦️ Condições para macrófitas: {cond}"]
        if ch.get("mm") is not None:
            linhas.append(f"• Chuva {ch['dias']} dias: {_br(ch['mm'], 0)} mm "
                          f"({_br(ch.get('pct_normal') or 0, 0)}% do normal)")
        if te.get("media_c") is not None:
            linhas.append(f"• Temperatura {te['dias']} dias: {_br(te['media_c'], 1)} °C "
                          f"(normal {_br(te.get('normal_c') or 0, 1)} °C)")

    linhas += ["", f"Painel: {PAINEL_URL}"]
    return "\n".join(linhas)


def motivo_aviso(estado: dict, anterior: Optional[dict], cfg: dict,
                 hoje: dt.date, forcar: bool) -> Optional[str]:
    av = cfg.get("avisos") or {}
    if forcar:
        return "execução manual"
    if anterior is None:
        return "primeira execução do nível único"
    # Entrar ou sair de "sem leitura recente" não muda nada na represa:
    # aparece na página e no resumo semanal, sem aviso próprio.
    de, para = estado.get("mudou_de"), estado["nivel"]
    if (av.get("ao_mudar_nivel", True) and de
            and "SEM_DADOS" not in (de, para)):
        return "mudança de nível"
    if av.get("lembrete_enquanto_alerta", True) and estado["nivel"] == "ALERTA":
        return "lembrete: segue em alerta"
    dia = av.get("resumo_semanal")
    if dia is not None and hoje.weekday() == int(dia):
        ultimo = (anterior or {}).get("ultimo_aviso_em", "")[:10]
        if ultimo != hoje.isoformat():
            return "resumo semanal"
    return None


# ── principal ──────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--forcar-aviso", action="store_true",
                    help="envia o aviso mesmo sem mudança de nível")
    args = ap.parse_args()

    cfg = carregar_config()
    hoje = dt.datetime.now(dt.timezone(dt.timedelta(hours=-3))).date()
    anterior = _json("alerta.json")

    calc = calcular(cfg, hoje)
    nivel, pendente = aplicar_histerese(
        calc, anterior, cfg["alerta_unico"].get("rebaixar_apos_execucoes", 3))
    nivel_ant = (anterior or {}).get("nivel")
    mudou_de = nivel_ant if (nivel_ant and nivel_ant != nivel) else None

    historico = list((anterior or {}).get("historico") or [])
    if mudou_de or not historico:
        historico.append({"data": hoje.isoformat(), "nivel": nivel})

    estado = {
        **calc,
        "nivel": nivel,
        "pendente": pendente,
        "mudou_de": mudou_de,
        "desde": historico[-1]["data"],
        "o_que_fazer": O_QUE_FAZER[nivel],
        "historico": historico[-50:],
        "atualizado_em": dt.datetime.now(dt.timezone.utc).isoformat(),
        "ultimo_aviso_em": (anterior or {}).get("ultimo_aviso_em"),
    }
    log.info("Nível %s (calculado %s%s)%s", nivel, calc["nivel_calculado"],
             f", rebaixamento para {pendente['nivel']} em "
             f"{pendente['execucoes']} execução(ões)" if pendente else "",
             f" | mudou de {mudou_de}" if mudou_de else "")
    for m in calc["motivos"]:
        log.info("  %s", m)

    motivo = motivo_aviso(estado, anterior, cfg, hoje, args.forcar_aviso)
    mensagem = compor(estado, cfg)
    log.info("Mensagem:\n%s", mensagem)
    if motivo:
        log.info("Aviso: %s", motivo)
        if not notificacao.suprimido():
            if not notificacao.canais_ativos():
                log.error("Nenhum canal de aviso configurado nos secrets.")
                return 1
            imagem = PAINEL.read_bytes() if PAINEL.exists() else None
            assunto = f"São Lourenço — {ROTULO[nivel]}"
            if mudou_de:
                assunto += f" (antes {ROTULO.get(mudou_de, mudou_de)})"
            notificacao.todos(assunto, mensagem, imagem)
            estado["ultimo_aviso_em"] = estado["atualizado_em"]
        else:
            notificacao.todos("", "")   # registra no log que foi suprimido
    else:
        log.info("Sem aviso nesta execução (nível estável, fora do dia do resumo).")

    SAIDA.parent.mkdir(parents=True, exist_ok=True)
    SAIDA.write_text(json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

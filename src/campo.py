"""
campo.py
========
Cruza as observações de campo registradas em data/campo.csv com a série de
satélite, produzindo a validação independente que o sistema não consegue
gerar sozinho.

Sem isto, tudo que o sistema afirma é verificado contra a própria série —
circularidade que nenhuma quantidade de dado orbital resolve. Uma única
observação de campo bem datada quebra esse círculo.

A rotina é chamada ao fim de cada monitoramento e não exige ação do
observador além de acrescentar uma linha ao CSV.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

# Ordem crescente de cobertura observada em campo.
CATEGORIAS = ["AUSENTE", "ISOLADA", "MODERADA", "EXTENSA"]

# Tradução entre o que se vê da margem e o que o sistema deveria ter dito.
# AUSENTE e ISOLADA são situações normais: macrófita em pouca quantidade é
# parte do funcionamento do reservatório, não anomalia.
ESPERADO = {
    "AUSENTE":  "NORMAL",
    "ISOLADA":  "NORMAL",
    "MODERADA": "CONFIRMADO",
    "EXTENSA":  "CONFIRMADO",
}

# Distância máxima, em dias, entre a observação e a passagem comparável.
JANELA_DIAS = 7


def ler_observacoes(caminho: Path) -> List[dict]:
    if not caminho.exists():
        return []
    with open(caminho, encoding="utf-8", newline="") as f:
        linhas = [r for r in csv.DictReader(f) if (r.get("data") or "").strip()]
    validas = []
    for r in linhas:
        cat = (r.get("cobertura") or "").strip().upper()
        if cat not in CATEGORIAS:
            log.warning("Observação de %s ignorada: cobertura '%s' não é uma "
                        "das categorias %s", r.get("data"), cat, CATEGORIAS)
            continue
        r["cobertura"] = cat
        validas.append(r)
    return validas


def _dias(a: str, b: str) -> int:
    return abs((dt.date.fromisoformat(a) - dt.date.fromisoformat(b)).days)


def passagem_mais_proxima(serie: List[dict], data: str,
                          so_confiavel: bool = True) -> Optional[dict]:
    """Passagem de satélite comparável à data da observação.

    Prefere cenas de alta confiança: comparar uma observação de campo a uma
    cena 80% encoberta não informa nada sobre o acerto do sistema.
    """
    candidatas = [r for r in serie if r.get("data")]
    if so_confiavel:
        boas = [r for r in candidatas if r.get("confianca") == "ALTA"]
        candidatas = boas or candidatas
    if not candidatas:
        return None
    melhor = min(candidatas, key=lambda r: _dias(r["data"], data))
    return melhor if _dias(melhor["data"], data) <= JANELA_DIAS else None


def validar(serie: List[dict], observacoes: List[dict]) -> dict:
    """Compara campo e satélite e resume a concordância."""
    pares, sem_par = [], []

    for obs in observacoes:
        cena = passagem_mais_proxima(serie, obs["data"])
        if not cena:
            sem_par.append({"data": obs["data"], "cobertura": obs["cobertura"],
                            "motivo": f"nenhuma passagem a até {JANELA_DIAS} dias"})
            continue
        esperado = ESPERADO[obs["cobertura"]]
        obtido = cena.get("nivel") or "NORMAL"
        # Vigilância é um aviso, não uma afirmação de cobertura: para fins de
        # concordância conta como normal, e seu desfecho é avaliado à parte.
        obtido_bin = "CONFIRMADO" if obtido == "CONFIRMADO" else "NORMAL"
        pares.append({
            "data_campo": obs["data"],
            "data_cena": cena["data"],
            "defasagem_dias": _dias(cena["data"], obs["data"]),
            "ponto": obs.get("ponto", ""),
            "cobertura_campo": obs["cobertura"],
            "nivel_sistema": obtido,
            "confianca_cena": cena.get("confianca", ""),
            "area_estimada_ha": cena.get("area_cobertura_flutuante_ha", ""),
            "concorda": esperado == obtido_bin,
            "tipo": _tipo(esperado, obtido_bin),
        })

    matriz = {"acerto_positivo": 0, "acerto_negativo": 0,
              "falso_positivo": 0, "falso_negativo": 0}
    for p in pares:
        matriz[p["tipo"]] += 1

    n = len(pares)
    resumo = {
        "observacoes_registradas": len(observacoes),
        "observacoes_comparadas": n,
        "observacoes_sem_passagem": sem_par,
        "matriz": matriz,
        "concordancia_pct": round(100 * sum(1 for p in pares if p["concorda"]) / n, 1) if n else None,
        "pares": sorted(pares, key=lambda p: p["data_campo"], reverse=True),
        "desfecho_avisos": _desfecho_avisos(serie, observacoes),
        "atualizado_em": dt.datetime.now(dt.timezone.utc).isoformat(),
        "nota": (
            "Observação de margem enxerga uma fração do reservatório, enquanto "
            "o satélite integra toda a área monitorada. Discordância não é "
            "necessariamente erro do sistema: a mancha pode estar fora do "
            "campo de visão do observador."
        ),
    }
    return resumo


def _tipo(esperado: str, obtido: str) -> str:
    if esperado == obtido:
        return "acerto_positivo" if esperado == "CONFIRMADO" else "acerto_negativo"
    return "falso_positivo" if obtido == "CONFIRMADO" else "falso_negativo"


def _desfecho_avisos(serie: List[dict], observacoes: List[dict]) -> List[dict]:
    """Para cada vigilância e alerta, o que foi encontrado em campo depois.

    É esta tabela que, acumulada ao longo de anos, converte o valor
    preditivo da vigilância de hipótese em medida. Hoje ele repousa sobre
    um único evento.
    """
    saida = []
    for cena in serie:
        nivel = cena.get("nivel")
        if nivel not in ("VIGILANCIA", "CONFIRMADO"):
            continue
        d0 = cena["data"]
        # Observações nos 90 dias seguintes ao aviso.
        seguintes = [o for o in observacoes
                     if 0 <= (dt.date.fromisoformat(o["data"])
                              - dt.date.fromisoformat(d0)).days <= 90]
        if seguintes:
            pior = max(seguintes, key=lambda o: CATEGORIAS.index(o["cobertura"]))
            desfecho = pior["cobertura"]
            verificado = True
        else:
            desfecho, verificado = "", False
        saida.append({
            "data": d0, "nivel": nivel,
            "verificado_em_campo": verificado,
            "maior_cobertura_em_90d": desfecho,
            "n_observacoes": len(seguintes),
        })
    return sorted(saida, key=lambda x: x["data"], reverse=True)


def executar(raiz: Path, serie: List[dict]) -> Optional[dict]:
    """Lê campo.csv, valida contra a série e publica docs/data/validacao.json."""
    obs = ler_observacoes(raiz / "data" / "campo.csv")
    if not obs:
        log.info("  nenhuma observação de campo registrada ainda")
        return None
    resumo = validar(serie, obs)
    destino = raiz / "docs" / "data" / "validacao.json"
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(json.dumps(resumo, ensure_ascii=False, indent=2),
                       encoding="utf-8")
    m = resumo["matriz"]
    log.info("  campo: %d observações, %d comparáveis | acertos %d, "
             "falsos positivos %d, falsos negativos %d",
             resumo["observacoes_registradas"], resumo["observacoes_comparadas"],
             m["acerto_positivo"] + m["acerto_negativo"],
             m["falso_positivo"], m["falso_negativo"])
    return resumo

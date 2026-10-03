"""
analise.py
==========
Climatologia mensal, escore padronizado, rótulo de confiança e confirmação
de alerta por persistência.

Três decisões metodológicas estão codificadas aqui, e cada uma corrige uma
fragilidade observada na versão anterior do sistema:

1. A climatologia é calculada SOMENTE sobre observações de alta confiança.
   Cenas parcialmente encobertas inflam a média e o desvio de referência,
   deslocando a régua contra a qual tudo é medido.

2. O período de referência exclui deliberadamente intervalos de evento
   conhecido. Incorporá-los eleva μ e σ e cega progressivamente o sistema
   para o fenômeno que ele existe para detectar.

3. O alerta exige persistência em duas observações qualificadas
   consecutivas. Pico de cena única é assinatura típica de nuvem residual,
   não de alteração ambiental.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Sequence

CONFIANCAS = ("BAIXA", "MEDIA", "ALTA")
_RANK = {c: i for i, c in enumerate(CONFIANCAS)}


# ── confiança ──────────────────────────────────────────────────────────────

def confianca(fracao_valida: float, lim_media: float, lim_alta: float) -> str:
    f = float(fracao_valida or 0.0)
    if f >= lim_alta:
        return "ALTA"
    if f >= lim_media:
        return "MEDIA"
    return "BAIXA"


def atende(nivel: str, minimo: str) -> bool:
    return _RANK.get(nivel, 0) >= _RANK.get(minimo, 0)


# ── climatologia ───────────────────────────────────────────────────────────

class Climatologia:
    """Média e desvio por mês, para um índice, em um setor."""

    def __init__(self, por_mes: Dict[int, dict], estimador: str = "media_dp"):
        self.por_mes = por_mes
        self.estimador = estimador

    @classmethod
    def construir(cls, registros: Iterable[dict], indice: str,
                  inicio: str, fim: str, conf_minima: str,
                  n_minimo: int = 5, estimador: str = "media_dp") -> "Climatologia":
        baldes: Dict[int, List[float]] = defaultdict(list)
        for r in registros:
            data = r.get("data")
            if not data or not (inicio <= data <= fim):
                continue
            if not atende(r.get("confianca", "BAIXA"), conf_minima):
                continue
            v = r.get(indice)
            if v is None or v == "":
                continue
            try:
                baldes[int(data[5:7])].append(float(v))
            except (TypeError, ValueError):
                continue

        por_mes = {}
        for mes, vals in baldes.items():
            if len(vals) < 2:
                continue
            if estimador == "mediana_mad":
                centro = statistics.median(vals)
                # 1.4826 torna o MAD comparável ao desvio-padrão sob normalidade
                disp = 1.4826 * statistics.median([abs(v - centro) for v in vals])
            else:
                centro = statistics.fmean(vals)
                disp = statistics.stdev(vals)
            por_mes[mes] = {
                "n": len(vals),
                "centro": centro,
                "dispersao": disp,
                "confiavel": len(vals) >= n_minimo,
            }
        return cls(por_mes, estimador)

    def z(self, data: str, valor: Optional[float]) -> Optional[float]:
        if valor is None:
            return None
        ref = self.por_mes.get(int(data[5:7]))
        if not ref or not ref["dispersao"]:
            return None
        return (float(valor) - ref["centro"]) / ref["dispersao"]

    def resumo(self) -> List[dict]:
        return [
            {"mes": m, **v} for m, v in sorted(self.por_mes.items())
        ]


# ── classificação de status ────────────────────────────────────────────────

def status(z: Optional[float], direcao: str,
           z_atencao: float, z_alerta: float) -> str:
    if z is None:
        return "SEM_REFERENCIA"
    v = abs(z) if direcao == "ambas" else (z if direcao == "alta" else -z)
    if v >= z_alerta:
        return "ALERTA"
    if v >= z_atencao:
        return "ATENCAO"
    return "NORMAL"


# ── persistência ───────────────────────────────────────────────────────────

def confirmar_persistencia(serie: Sequence[dict], indice: str,
                           conf_minima: str, exigir: bool = True) -> None:
    """Marca `<indice>_confirmado` em cada registro, in place.

    Percorre a série em ordem cronológica. Observações abaixo da confiança
    mínima são ignoradas na verificação — não confirmam nem interrompem uma
    sequência — mas permanecem no histórico.
    """
    anterior_em_alerta = False
    for reg in sorted(serie, key=lambda r: r.get("data", "")):
        chave = f"{indice}_confirmado"
        if not atende(reg.get("confianca", "BAIXA"), conf_minima):
            reg[chave] = ""
            continue
        em_alerta = reg.get(f"{indice}_status") == "ALERTA"
        reg[chave] = "SIM" if (em_alerta and (not exigir or anterior_em_alerta)) else "NAO"
        anterior_em_alerta = em_alerta


# ── combinações ────────────────────────────────────────────────────────────

def avaliar_combinacoes(reg: dict, combinacoes: List[dict]) -> List[str]:
    """Rótulos das combinações de índices que dispararam na mesma data.

    Dois índices independentes apontando na mesma direção é evidência
    substancialmente mais forte que um isolado — e é o que separa alteração
    real de artefato de uma banda específica.
    """
    achados = []
    for c in combinacoes or []:
        if all(reg.get(f"{i}_status") in ("ALERTA", "ATENCAO")
               for i in c["indices"]):
            achados.append(c["rotulo"])
    return achados


# ── contraste entre setores ────────────────────────────────────────────────

def serie_contraste(serie_a: Sequence[dict], serie_b: Sequence[dict],
                    indices: Sequence[str]) -> List[dict]:
    """Série da diferença entre dois setores, data a data.

    Sazonalidade, condição atmosférica e variação de nível afetam os dois
    setores de modo semelhante e são canceladas pela subtração. O que
    sobrevive tende a ser local — que é exatamente o que interessa quando se
    investiga um ponto de lançamento específico.
    """
    por_data_b = {r["data"]: r for r in serie_b}
    saida = []
    for ra in serie_a:
        rb = por_data_b.get(ra.get("data"))
        if not rb:
            continue
        linha = {
            "data": ra["data"],
            "confianca": min(ra.get("confianca", "BAIXA"),
                             rb.get("confianca", "BAIXA"), key=lambda c: _RANK[c]),
        }
        for i in indices:
            va, vb = ra.get(i), rb.get(i)
            linha[i] = (float(va) - float(vb)) if va not in (None, "") and vb not in (None, "") else None
        saida.append(linha)
    return saida

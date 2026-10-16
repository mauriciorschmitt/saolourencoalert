#!/usr/bin/env python3
"""
clima.py
========
Condições ambientais na represa: chuva acumulada e temperatura, comparadas
com o normal da época do ano. Dados do Open-Meteo, sem chave de acesso.

    python -m src.clima

Grava docs/data/clima.json. É contexto para o semáforo, não dispara alerta:
chuva e calor mudam a chance de uma infestação, não indicam que ela existe.

Duas fontes, cada uma no que faz melhor:
  - normais: reanálise ERA5 (archive-api), 2016–2024, consistente no tempo;
  - situação atual e previsão: modelo de previsão (api), que cobre até hoje
    e os próximos 7 dias, enquanto o ERA5 chega com ~5 dias de atraso.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import statistics
import sys
from pathlib import Path
from typing import Dict, List, Optional

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.monitor import carregar_config  # noqa: E402

RAIZ = Path(__file__).resolve().parent.parent
SAIDA = RAIZ / "docs" / "data" / "clima.json"
ARQUIVO = "https://archive-api.open-meteo.com/v1/archive"
PREVISAO = "https://api.open-meteo.com/v1/forecast"
VARIAVEIS = "temperature_2m_mean,precipitation_sum"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("clima")


def baixar(url: str, **params) -> Dict[str, list]:
    for tentativa in range(1, 4):
        try:
            r = requests.get(url, params={**params, "daily": VARIAVEIS,
                                          "timezone": "America/Sao_Paulo"}, timeout=60)
            if r.ok:
                return r.json()["daily"]
            log.warning("Open-Meteo HTTP %d: %s", r.status_code, r.text[:200])
        except requests.RequestException as exc:
            log.warning("Open-Meteo, tentativa %d: %s", tentativa, exc)
    raise RuntimeError(f"Open-Meteo indisponível: {url}")


def acumulado(datas: List[str], vals: List[Optional[float]], fim: int,
              dias: int, media: bool) -> Optional[float]:
    """Soma (ou média) dos `dias` terminando no índice `fim`, inclusive."""
    janela = [v for v in vals[max(0, fim - dias + 1):fim + 1] if v is not None]
    if len(janela) < dias * 0.9:
        return None
    return statistics.fmean(janela) if media else sum(janela)


def normal(datas: List[str], vals: List[Optional[float]], alvo: dt.date,
           dias: int, media: bool) -> Optional[dict]:
    """Média e desvio do acumulado de `dias` terminando na mesma época do
    ano (±7 dias) em cada ano do período de referência."""
    amostras = []
    for i, d in enumerate(datas):
        dd = dt.date.fromisoformat(d)
        delta = abs((dd.replace(year=2000) - alvo.replace(year=2000)).days) \
            if not (dd.month == 2 and dd.day == 29) else 99
        if min(delta, 366 - delta) <= 7:
            v = acumulado(datas, vals, i, dias, media)
            if v is not None:
                amostras.append(v)
    if len(amostras) < 10:
        return None
    return {"media": statistics.fmean(amostras), "dp": statistics.stdev(amostras),
            "n": len(amostras)}


def zscore(v: Optional[float], ref: Optional[dict]) -> Optional[float]:
    if v is None or not ref or not ref["dp"]:
        return None
    return (v - ref["media"]) / ref["dp"]


def main() -> int:
    cfg = carregar_config()
    cc = cfg.get("clima") or {}
    if not cc.get("ativo"):
        log.info("Clima desativado em config.yaml.")
        return 0
    lat, lon = cc["latitude"], cc["longitude"]
    jc, jt = cc["janela_chuva_dias"], cc["janela_temp_dias"]

    hoje = dt.date.today()
    hist = baixar(ARQUIVO, latitude=lat, longitude=lon,
                  start_date=cc["normais_inicio"], end_date=cc["normais_fim"])
    # O modelo de previsão só guarda uns 60 dias para trás e o ERA5 chega com
    # ~5 dias de atraso: a janela recente junta os dois, ERA5 onde existe.
    inicio = hoje - dt.timedelta(days=max(jc, jt) + 5)
    era5 = baixar(ARQUIVO, latitude=lat, longitude=lon,
                  start_date=inicio.isoformat(), end_date=hoje.isoformat())
    atual = baixar(PREVISAO, latitude=lat, longitude=lon,
                   past_days=30, forecast_days=7)

    serie: Dict[str, Dict[str, Optional[float]]] = {}
    for fonte in (atual, era5):           # ERA5 por último: prevalece
        for d, p, t in zip(fonte["time"], fonte["precipitation_sum"],
                           fonte["temperature_2m_mean"]):
            atual_d = serie.setdefault(d, {"p": None, "t": None})
            if p is not None:
                atual_d["p"] = p
            if t is not None:
                atual_d["t"] = t
    datas = sorted(serie)
    chuva = [serie[d]["p"] for d in datas]
    temp = [serie[d]["t"] for d in datas]
    i_hoje = max(i for i, d in enumerate(datas) if d <= hoje.isoformat())

    chuva_ac = acumulado(datas, chuva, i_hoje, jc, media=False)
    temp_med = acumulado(datas, temp, i_hoje, jt, media=True)
    prev = [t for t in temp[i_hoje + 1:i_hoje + 8] if t is not None]
    temp_prev = statistics.fmean(prev) if prev else None

    n_chuva = normal(hist["time"], hist["precipitation_sum"], hoje, jc, media=False)
    n_temp = normal(hist["time"], hist["temperature_2m_mean"], hoje, jt, media=True)
    n_prev = normal(hist["time"], hist["temperature_2m_mean"],
                    hoje + dt.timedelta(days=7), 7, media=True)

    z_chuva, z_temp = zscore(chuva_ac, n_chuva), zscore(temp_med, n_temp)
    lim = cc.get("z_favoravel", 1.0)
    fav = [nome for nome, z in (("chuva", z_chuva), ("calor", z_temp))
           if z is not None and z >= lim]
    condicao = ("MUITO_FAVORAVEL" if len(fav) == 2
                else "FAVORAVEL" if fav else "NEUTRA")

    r = lambda v, d=1: round(v, d) if v is not None else None  # noqa: E731
    saida = {
        "atualizado_em": dt.datetime.now(dt.timezone.utc).isoformat(),
        "data": hoje.isoformat(),
        "condicao": condicao,
        "fatores": fav,
        "chuva": {"dias": jc, "mm": r(chuva_ac, 0),
                  "normal_mm": r(n_chuva and n_chuva["media"], 0),
                  "pct_normal": r(100 * chuva_ac / n_chuva["media"], 0)
                                if chuva_ac is not None and n_chuva else None,
                  "z": r(z_chuva, 2)},
        "temperatura": {"dias": jt, "media_c": r(temp_med),
                        "normal_c": r(n_temp and n_temp["media"]),
                        "z": r(z_temp, 2)},
        "previsao_7d": {"media_c": r(temp_prev),
                        "normal_c": r(n_prev and n_prev["media"])},
        "fonte": "Open-Meteo: previsão (situação atual) e reanálise ERA5 "
                 f"{cc['normais_inicio'][:4]}–{cc['normais_fim'][:4]} (normais)",
        "nota": "Contexto, não alerta. Chuva forte leva nutrientes à represa e "
                "calor acelera o crescimento das macrófitas; a relação com "
                "infestações foi observada em um único evento (2025).",
    }
    SAIDA.parent.mkdir(parents=True, exist_ok=True)
    SAIDA.write_text(json.dumps(saida, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Chuva %d dias: %s mm (normal %s, z %s) | temperatura %d dias: %s °C "
             "(normal %s, z %s) | condição %s", jc, saida["chuva"]["mm"],
             saida["chuva"]["normal_mm"], saida["chuva"]["z"], jt,
             saida["temperatura"]["media_c"], saida["temperatura"]["normal_c"],
             saida["temperatura"]["z"], condicao)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

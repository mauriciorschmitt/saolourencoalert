#!/usr/bin/env python3
"""
radar.py
========
Detecção de vegetação flutuante por radar Sentinel-1, que atravessa nuvens.

    python -m src.radar                          # janela padrão (30 dias)
    python -m src.radar --desde 2016-07-19       # série completa

Produz docs/data/radar.csv e docs/data/radar_ultimo.json. Parâmetros e
calibração em config.yaml, seção `radar`.

O princípio físico é simples: água parada reflete a onda do radar para longe
do satélite, como um espelho, e aparece escura. Uma camada de macrófitas
espalha a onda em todas as direções e devolve parte dela, aparecendo clara.
A polarização cruzada VH é a mais sensível a esse espalhamento pela
vegetação e a menos sensível ao vento sobre a água, que é a principal fonte
de falso positivo.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import sys
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import sentinelhub as sh
from src.analise import Climatologia, confirmar_persistencia, status
from src.monitor import blocos, carregar_config, carregar_geometria, mesclar

RAIZ = Path(__file__).resolve().parent.parent
SAIDA = RAIZ / "docs" / "data"
CSV = SAIDA / "radar.csv"
ULTIMO = SAIDA / "radar_ultimo.json"
INDICE = "frac_vegetacao"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("radar")

CAMPOS = ["data", "pixels_validos", "fracao_valida", "confianca",
          "vh_db_mediana", "vv_db_mediana", "frac_vegetacao",
          "area_vegetacao_ha", "frac_vegetacao_z", "frac_vegetacao_status",
          "frac_vegetacao_confirmado"]


def evalscript(limiar_db: float) -> str:
    # Pixels com VH ou VV não positivos (sombra de radar, borda de faixa)
    # saem pela máscara em vez de virarem −∞ dB e puxarem a estatística.
    return f"""//VERSION=3
function setup() {{
  return {{
    input: [{{ bands: ["VV", "VH", "dataMask"] }}],
    output: [
      {{ id: "vh_db", bands: 1, sampleType: "FLOAT32" }},
      {{ id: "vv_db", bands: 1, sampleType: "FLOAT32" }},
      {{ id: "vegetacao", bands: 1, sampleType: "UINT8" }},
      {{ id: "dataMask", bands: 1 }}
    ]
  }};
}}
function db(x) {{ return 10 * Math.log(x) / Math.LN10; }}
function evaluatePixel(s) {{
  let ok = s.dataMask == 1 && s.VV > 0 && s.VH > 0;
  let vh = ok ? db(s.VH) : 0;
  return {{
    vh_db: [vh],
    vv_db: [ok ? db(s.VV) : 0],
    vegetacao: [ok && vh > {limiar_db} ? 1 : 0],
    dataMask: [ok ? 1 : 0]
  }};
}}
"""


def coletar(cred: sh.Credencial, cfg: dict, inicio: dt.date, fim: dt.date) -> List[dict]:
    rc = cfg["radar"]
    setor = next(s for s in cfg["setores"] if s["id"] == rc.get("setor", "represa"))
    geom_utm, epsg = sh.reprojetar_utm(carregar_geometria(setor["arquivo"]),
                                       rc.get("recuo_borda_m", 0))
    dado = {
        "type": rc["colecao"],
        "dataFilter": {"acquisitionMode": "IW", "polarization": "DV",
                       "resolution": "HIGH", "orbitDirection": rc["orbita"]},
        # Gamma0 com correção de terreno torna as passagens comparáveis entre
        # si; o filtro de speckle reduz o granulado que, pixel a pixel, faria
        # água cruzar o limiar por acaso.
        "processing": {"backCoeff": "GAMMA0_TERRAIN", "orthorectify": True,
                       "demInstance": "COPERNICUS"},
    }
    if rc.get("filtro_speckle"):
        dado["processing"]["speckleFilter"] = rc["filtro_speckle"]

    mediana = {"statistics": {"default": {"percentiles": {"k": [50]}}}}
    registros, falhas = [], []
    for ini, fi in blocos(inicio, fim, rc.get("chunk_dias", 180)):
        log.info("  radar: %s a %s", ini, fi)
        payload = {
            "input": {"bounds": {"geometry": geom_utm, "properties": {
                          "crs": f"http://www.opengis.net/def/crs/EPSG/0/{epsg}"}},
                      "data": [dado]},
            "aggregation": {
                "timeRange": {"from": f"{ini}T00:00:00Z", "to": f"{fi}T23:59:59Z"},
                "aggregationInterval": {"of": "P1D"},
                "resx": 10, "resy": 10,
                "evalscript": evalscript(rc["limiar_vh_db"]),
            },
            "calculations": {"vh_db": mediana, "vv_db": mediana,
                             "vegetacao": {"statistics": {"default": {}}}},
        }
        try:
            dados = sh._post_com_retry(sh.STATS_URL, cred, payload).get("data", [])
        except sh.SentinelHubError as exc:
            log.warning("    bloco falhou: %s", exc)
            falhas.append((ini, fi))
            continue
        for item in dados:
            vh = sh.extrair(item, "vh_db") or {}
            vv = sh.extrair(item, "vv_db") or {}
            veg = sh.extrair(item, "vegetacao") or {}
            validos = vh.get("sampleCount", 0) - vh.get("noDataCount", 0)
            if validos <= 0 or veg.get("mean") is None:
                continue
            registros.append({
                "data": item["interval"]["from"][:10],
                "pixels_validos": validos,
                "vh_db_mediana": round(vh.get("percentiles", {}).get("50.0", vh.get("mean")), 3),
                "vv_db_mediana": round(vv.get("percentiles", {}).get("50.0", vv.get("mean")), 3),
                "frac_vegetacao": round(veg["mean"], 4),
                "area_vegetacao_ha": round(veg["mean"] * validos * sh.area_pixel_ha(10), 2),
            })
    if falhas:
        log.warning("  %d bloco(s) sem resposta; reexecute:", len(falhas))
        for a, b in falhas:
            log.warning("    python -m src.radar --desde %s --ate %s", a, b)
    return registros


def analisar(cfg: dict, registros: List[dict]) -> None:
    """Cobertura, climatologia mensal, z-score, status e persistência, in place."""
    rc, clim_cfg = cfg["radar"], cfg["climatologia"]

    # Fração da represa coberta pela passagem, contra a maior contagem já
    # vista. Diferente do óptico, o radar não tem nuvem: cena parcial aqui é
    # borda de faixa de imageamento.
    ref = max((int(r["pixels_validos"]) for r in registros), default=1)
    for r in list(registros):
        f = int(r["pixels_validos"]) / ref
        if f < rc.get("fracao_minima", 0.5):
            registros.remove(r)
            continue
        r["fracao_valida"] = round(f, 4)
        r["confianca"] = "ALTA" if f >= 0.9 else "MEDIA"

    # Mesmo período de referência do módulo óptico, que exclui o evento de
    # 2025. Na prática o radar só tem passagens de 2016 a 2021 nesse período.
    clim = Climatologia.construir(registros, INDICE, clim_cfg["referencia_inicio"],
                                  clim_cfg["referencia_fim"], "ALTA",
                                  clim_cfg["n_minimo_por_mes"], clim_cfg["estimador"])
    for r in registros:
        z = clim.z(r["data"], r.get(INDICE))
        r[f"{INDICE}_z"] = round(z, 3) if z is not None else None
        r[f"{INDICE}_status"] = status(z, "alta", rc["z_atencao"], rc["z_alerta"])
    confirmar_persistencia(registros, INDICE, "MEDIA", rc.get("exigir_persistencia", True))

    n_alerta = sum(1 for r in registros if r.get(f"{INDICE}_status") == "ALERTA")
    n_conf = sum(1 for r in registros if r.get(f"{INDICE}_confirmado") == "SIM")
    log.info("  radar: %d passagens | %d em alerta | %d confirmadas",
             len(registros), n_alerta, n_conf)


def ler_existente() -> List[dict]:
    if not CSV.exists():
        return []
    with open(CSV, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def gravar(registros: List[dict], cfg: dict) -> None:
    SAIDA.mkdir(parents=True, exist_ok=True)
    with open(CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS, extrasaction="ignore")
        w.writeheader()
        w.writerows(registros)
    log.info("  %s: %d linhas", CSV.relative_to(RAIZ), len(registros))
    if registros:
        rc = cfg["radar"]
        ULTIMO.write_text(json.dumps({
            "observacao": {k: registros[-1].get(k) for k in CAMPOS},
            "atualizado_em": dt.datetime.now(dt.timezone.utc).isoformat(),
            "limiar_vh_db": rc["limiar_vh_db"],
            "z_atencao": rc["z_atencao"], "z_alerta": rc["z_alerta"],
            "orbita": rc["orbita"],
        }, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--desde", help="data inicial AAAA-MM-DD")
    ap.add_argument("--ate", help="data final AAAA-MM-DD")
    args = ap.parse_args()

    cfg = carregar_config()
    rc = cfg.get("radar") or {}
    if not rc.get("ativo"):
        log.info("Radar desativado em config.yaml.")
        return 0

    fim = dt.date.fromisoformat(args.ate) if args.ate else dt.date.today()
    inicio = (dt.date.fromisoformat(args.desde) if args.desde
              else fim - dt.timedelta(days=rc.get("janela_dias", 30)))
    log.info("Radar Sentinel-1 | %s a %s", inicio, fim)

    cred = sh.Credencial()
    cred.renovar()
    novos = coletar(cred, cfg, inicio, fim)
    anteriores = ler_existente()
    if not novos and not anteriores:
        log.error("Nenhuma passagem de radar obtida e não há série anterior.")
        return 2
    if not novos:
        log.warning("  nenhuma passagem nova no período; série mantida")

    # O CSV relido traz tudo como texto; as colunas derivadas são
    # recalculadas a cada execução sobre a série inteira.
    completa = mesclar(anteriores, novos)
    analisar(cfg, completa)
    gravar(completa, cfg)
    log.info("Concluído.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

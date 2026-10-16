#!/usr/bin/env python3
"""
explorar_radar.py — TEMPORÁRIO, só para a exploração inicial do Sentinel-1.

Responde, antes de construir o módulo:
  1. quantas passagens Sentinel-1 existem sobre a represa, por órbita e ano;
  2. qual o retroespalhamento (VV, VH em dB) da lâmina d'água em período
     normal e durante o evento de macrófitas de 2025;
  3. que limiar de VH separa água livre de vegetação flutuante.

Grava radar_exploracao.csv e imprime um resumo no log.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import math
import statistics as st
import sys
from collections import Counter, defaultdict
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ))
from src import sentinelhub as sh  # noqa: E402

LIMIARES = [-24, -22, -20, -18, -16]

EVALSCRIPT = """//VERSION=3
function setup() {
  return {
    input: [{ bands: ["VV", "VH", "dataMask"] }],
    output: [
      { id: "vv_db", bands: 1, sampleType: "FLOAT32" },
      { id: "vh_db", bands: 1, sampleType: "FLOAT32" },
      %OUT%
      { id: "dataMask", bands: 1 }
    ]
  };
}
function db(x) { return 10 * Math.log(x) / Math.LN10; }
function evaluatePixel(s) {
  let ok = s.dataMask == 1 && s.VV > 0 && s.VH > 0;
  let vh = ok ? db(s.VH) : -99;
  return {
    vv_db: [ok ? db(s.VV) : 0],
    vh_db: [ok ? vh : 0],
    %RET%
    dataMask: [ok ? 1 : 0]
  };
}
"""
EVALSCRIPT = EVALSCRIPT.replace(
    "%OUT%", "\n      ".join(f'{{ id: "t{-t}", bands: 1, sampleType: "UINT8" }},' for t in LIMIARES)
).replace(
    "%RET%", "\n    ".join(f"t{-t}: [vh > {t} ? 1 : 0]," for t in LIMIARES)
)


def consultar(cred, geom_utm, epsg, ini, fim, orbita, filtro):
    dado = {
        "type": "sentinel-1-grd",
        "dataFilter": {"acquisitionMode": "IW", "polarization": "DV",
                       "resolution": "HIGH", "orbitDirection": orbita},
        "processing": {"backCoeff": "GAMMA0_TERRAIN", "orthorectify": True,
                       "demInstance": "COPERNICUS"},
    }
    if filtro:
        dado["processing"]["speckleFilter"] = {"type": "LEE", "windowSizeX": 3, "windowSizeY": 3}
    calc = {"vv_db": {"statistics": {"default": {"percentiles": {"k": [10, 50, 90]}}}},
            "vh_db": {"statistics": {"default": {"percentiles": {"k": [10, 50, 90]}}}}}
    calc.update({f"t{-t}": {"statistics": {"default": {}}} for t in LIMIARES})
    payload = {
        "input": {"bounds": {"geometry": geom_utm,
                             "properties": {"crs": f"http://www.opengis.net/def/crs/EPSG/0/{epsg}"}},
                  "data": [dado]},
        "aggregation": {"timeRange": {"from": f"{ini}T00:00:00Z", "to": f"{fim}T23:59:59Z"},
                        "aggregationInterval": {"of": "P1D"},
                        "resx": 10, "resy": 10, "evalscript": EVALSCRIPT},
        "calculations": calc,
    }
    return sh._post_com_retry(sh.STATS_URL, cred, payload).get("data", [])


def main():
    gj = json.loads((RAIZ / "data/setores/represa.geojson").read_text(encoding="utf-8"))
    geom = gj["features"][0]["geometry"] if gj.get("type") == "FeatureCollection" else gj
    geom_utm, epsg = sh.reprojetar_utm(geom, 10)
    cred = sh.Credencial()
    cred.renovar()

    # speckle filter é aceito na Statistical API? testa num bloco curto
    filtro = True
    try:
        consultar(cred, geom_utm, epsg, "2025-08-01", "2025-08-31", "DESCENDING", True)
        print("Filtro de speckle LEE: aceito")
    except sh.SentinelHubError as exc:
        print("Filtro de speckle LEE recusado; seguindo sem filtro:", str(exc)[:200])
        filtro = False

    linhas = []
    hoje = dt.date.today()
    for orbita in ("ASCENDING", "DESCENDING"):
        ini = dt.date(2016, 7, 1)
        while ini <= hoje:
            fim = min(ini + dt.timedelta(days=179), hoje)
            try:
                dados = consultar(cred, geom_utm, epsg, ini, fim, orbita, filtro)
            except sh.SentinelHubError as exc:
                print(f"  {orbita} {ini}..{fim} FALHOU: {str(exc)[:200]}")
                dados = []
            for item in dados:
                o = item.get("outputs", {})
                def s(nome):
                    return o.get(nome, {}).get("bands", {}).get("B0", {}).get("stats", {})
                vh, vv = s("vh_db"), s("vv_db")
                n = (vh.get("sampleCount", 0) - vh.get("noDataCount", 0))
                if n <= 0:
                    continue
                pv = vh.get("percentiles", {})
                linha = {"data": item["interval"]["from"][:10], "orbita": orbita[:3], "pixels": n,
                         "vh_media": round(vh.get("mean"), 3), "vh_p10": round(pv.get("10.0", math.nan), 3),
                         "vh_p50": round(pv.get("50.0", math.nan), 3), "vh_p90": round(pv.get("90.0", math.nan), 3),
                         "vv_media": round(vv.get("mean"), 3),
                         "vv_p50": round(vv.get("percentiles", {}).get("50.0", math.nan), 3)}
                for t in LIMIARES:
                    linha[f"frac_vh_gt{-t}"] = round(s(f"t{-t}").get("mean", math.nan), 4)
                linhas.append(linha)
            print(f"  {orbita} {ini}..{fim}: {len(dados)} passagens")
            ini = fim + dt.timedelta(days=1)

    linhas.sort(key=lambda r: (r["data"], r["orbita"]))
    with open(RAIZ / "radar_exploracao.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(linhas[0]))
        w.writeheader()
        w.writerows(linhas)

    print("\n== Passagens por ano e órbita ==")
    c = Counter((r["data"][:4], r["orbita"]) for r in linhas)
    for ano in sorted({a for a, _ in c}):
        print(ano, " ASC", c[(ano, "ASC")], " DES", c[(ano, "DES")])
    print("pixels por cena (mediana):", st.median(r["pixels"] for r in linhas))

    print("\n== VH mediano (dB) e fração VH>-20 por mês, por órbita ==")
    g = defaultdict(list)
    for r in linhas:
        g[(r["data"][:7], r["orbita"])].append(r)
    for (mes, orb), rs in sorted(g.items()):
        if mes < "2024-06":
            continue
        print(mes, orb, "n=%d" % len(rs), "vh_p50=%.1f" % st.median(x["vh_p50"] for x in rs),
              "vv_p50=%.1f" % st.median(x["vv_p50"] for x in rs),
              "f>-22=%.2f f>-20=%.2f f>-18=%.2f" % tuple(st.median(x[k] for x in rs)
                                                       for k in ("frac_vh_gt22", "frac_vh_gt20", "frac_vh_gt18")))

    # comparação com a cobertura óptica (cenas ALTA, ±3 dias)
    opt = [r for r in csv.DictReader(open(RAIZ / "docs/data/represa.csv", encoding="utf-8"))
           if r.get("confianca") == "ALTA"]
    pares = []
    for r in linhas:
        d = dt.date.fromisoformat(r["data"])
        perto = min(opt, key=lambda o: abs((dt.date.fromisoformat(o["data"]) - d).days))
        if abs((dt.date.fromisoformat(perto["data"]) - d).days) <= 3:
            pares.append((r, float(perto["ndvi"]), float(perto["area_cobertura_flutuante_ha"])))
    print("\n== Correlação com o óptico (pares ±3 dias, cenas ALTA): n =", len(pares), "==")
    for orb in ("ASC", "DES"):
        ps = [p for p in pares if p[0]["orbita"] == orb]
        if len(ps) < 5:
            continue
        for k in ("vh_p50", "vh_media", "vv_p50", "frac_vh_gt22", "frac_vh_gt20", "frac_vh_gt18", "frac_vh_gt16"):
            xs = [p[0][k] for p in ps]
            print(orb, k.ljust(13), "r(ndvi)=%.2f  r(cobertura_ha)=%.2f" % (
                st.correlation(xs, [p[1] for p in ps]), st.correlation(xs, [p[2] for p in ps])))
    print("\n== Últimas passagens ==")
    for r in linhas[-12:]:
        print(r)


if __name__ == "__main__":
    main()

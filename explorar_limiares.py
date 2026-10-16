#!/usr/bin/env python3
"""
explorar_limiares.py — TEMPORÁRIO, para recalibrar a classificação óptica.

Busca a série 2016–2026 da represa (mesmo polígono, recuo e máscara de
nuvem do módulo de qualidade da água) com várias regras candidatas para
"cobertura flutuante" e "lâmina d'água", e grava limiares_exploracao.csv
para comparação com o radar.
"""
import copy
import csv
import datetime as dt
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ))
from src import sentinelhub as sh  # noqa: E402
from src.monitor import calibrar_cobertura, carregar_config, coletar_setor  # noqa: E402


def c(indice, limiar, op="maior"):
    return {"indice": indice, "limiar": limiar, "operador": op}


CANDIDATAS = {
    # vegetação flutuante
    "v_fai02": c("fai", 0.02), "v_fai04": c("fai", 0.04), "v_fai06": c("fai", 0.06),
    "v_fai08": c("fai", 0.08), "v_fai10": c("fai", 0.10),
    "v_ndvi0": c("ndvi", 0.0), "v_ndvi1": c("ndvi", 0.1), "v_ndvi2": c("ndvi", 0.2),
    "v_ndvi3": c("ndvi", 0.3), "v_ndvi4": c("ndvi", 0.4),
    "v_n1f02": {"condicoes": [c("ndvi", 0.1), c("fai", 0.02)]},
    "v_n2f02": {"condicoes": [c("ndvi", 0.2), c("fai", 0.02)]},
    "v_n2f04": {"condicoes": [c("ndvi", 0.2), c("fai", 0.04)]},
    "v_n3f04": {"condicoes": [c("ndvi", 0.3), c("fai", 0.04)]},
    # lâmina d'água
    "a_ndwi0": c("ndwi", 0.0), "a_ndwim1": c("ndwi", -0.1), "a_ndwim2": c("ndwi", -0.2),
    "a_ndvi0": c("ndvi", 0.0, "menor"), "a_ndvi1": c("ndvi", 0.1, "menor"),
}


def main():
    cfg = carregar_config()
    cfg_x = copy.deepcopy(cfg)
    cfg_x["classificacao"] = CANDIDATAS
    cfg_x["indices"] = {k: cfg["indices"][k] for k in ("ndvi", "fai", "ndwi", "ndti")}
    setor = next(s for s in cfg["setores"] if s["id"] == "represa")
    cred = sh.Credencial()
    cred.renovar()
    regs = coletar_setor(cred, cfg_x, setor, dt.date(2016, 7, 19), dt.date.today())
    regs.sort(key=lambda r: r["data"])
    calibrar_cobertura(cfg, regs)
    campos = list(dict.fromkeys(k for r in regs for k in r))
    with open(RAIZ / "limiares_exploracao.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campos)
        w.writeheader()
        w.writerows(regs)
    print(f"{len(regs)} passagens gravadas em limiares_exploracao.csv")


if __name__ == "__main__":
    main()

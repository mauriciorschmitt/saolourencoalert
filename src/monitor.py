#!/usr/bin/env python3
"""
monitor.py
==========
Execução do monitoramento. Roda periodicamente (agendado) ou sobre um
intervalo arbitrário (construção retroativa da série).

    python -m src.monitor                          # janela padrão
    python -m src.monitor --desde 2016-07-19       # série completa
    python -m src.monitor --setor jusante          # apenas um setor

Uma execução produz, por setor: um CSV de série temporal em
docs/data/<setor>.csv e um JSON com a observação mais recente.
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

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import campo, evalscripts, sentinelhub as sh
from src.analise import (
    Climatologia, atende, avaliar_combinacoes, avaliar_niveis, confianca,
    confirmar_persistencia, consolidar_persistencia_nivel, serie_contraste,
    status,
)

RAIZ = Path(__file__).resolve().parent.parent
SAIDA = RAIZ / "docs" / "data"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("monitor")


# ── configuração ───────────────────────────────────────────────────────────

def carregar_config(caminho: Path = RAIZ / "config.yaml") -> dict:
    with open(caminho, encoding="utf-8") as f:
        return yaml.safe_load(f)


def carregar_geometria(rel: str) -> dict:
    with open(RAIZ / rel, encoding="utf-8") as f:
        gj = json.load(f)
    if gj.get("type") == "FeatureCollection":
        return gj["features"][0]["geometry"]
    if gj.get("type") == "Feature":
        return gj["geometry"]
    return gj


# ── aquisição ──────────────────────────────────────────────────────────────

def blocos(inicio: dt.date, fim: dt.date, dias: int):
    atual = inicio
    while atual <= fim:
        termino = min(atual + dt.timedelta(days=dias - 1), fim)
        yield atual, termino
        atual = termino + dt.timedelta(days=1)


def coletar_setor(token: str, cfg: dict, setor: dict,
                  inicio: dt.date, fim: dt.date) -> List[dict]:
    """Série bruta de um setor, já rotulada por confiança."""
    ind_cfg = cfg["indices"]
    classif = cfg["classificacao"]
    qual = cfg["qualidade"]
    aq = cfg["aquisicao"]

    indices = list(ind_cfg.keys())
    evalscript = evalscripts.render(indices, classif, qual["scl_descartadas"])
    # dataMask NÃO entra em calculations: ele define quais pixels contam,
    # não é uma grandeza a resumir. Pedir estatística dele faz a API recusar
    # a requisição inteira — e como a falha é por bloco, o resultado é um
    # CSV vazio sem erro aparente.
    saidas = indices + [f"cls_{n}" for n in classif]

    geom = carregar_geometria(setor["arquivo"])
    area_px = sh.area_pixel_ha(aq["resolucao_m"])

    registros: List[dict] = []
    falhas: List[tuple] = []

    for ini, fi in blocos(inicio, fim, aq["chunk_dias"]):
        log.info("  %s: %s a %s", setor["id"], ini, fi)
        try:
            bruto = sh.consultar(token, geom, evalscript, ini, fi, saidas,
                                 aq["colecao"], aq["resolucao_m"],
                                 aq.get("recuo_borda_m", 0))
        except sh.SentinelHubError as exc:
            # Um bloco ruim não deve interromper dez anos de série.
            log.warning("    bloco falhou: %s", exc)
            falhas.append((ini, fi))
            continue

        for item in bruto:
            ndvi_stats = sh.extrair(item, "ndvi")
            if not ndvi_stats:
                continue
            amostras = ndvi_stats.get("sampleCount", 0)
            if amostras <= 0:
                continue
            # sampleCount conta os pixels do retângulo envolvente, não os do
            # polígono. Num reservatório estreito e alongado o retângulo é
            # várias vezes maior que a área monitorada, então dividir por ele
            # subestimaria a cobertura de forma grosseira — e nenhuma cena
            # jamais seria classificada como confiável. A fração é calculada
            # depois, contra a maior contagem já observada na série.
            validos = amostras - ndvi_stats.get("noDataCount", 0)
            if validos <= 0:
                continue

            data = item["interval"]["from"][:10]
            reg: Dict[str, object] = {
                "data": data,
                "pixels_validos": validos,
            }
            for nome in indices:
                st = sh.extrair(item, nome)
                reg[nome] = round(st["mean"], 6) if st and st.get("mean") is not None else None

            # Máscaras binárias: a média é a fração de pixels válidos que
            # cruzam o limiar; multiplicada pela área do pixel, vira hectares.
            for nome in classif:
                st = sh.extrair(item, f"cls_{nome}")
                frac = st.get("mean") if st else None
                if frac is None:
                    reg[f"area_{nome}_ha"] = None
                    reg[f"frac_{nome}"] = None
                else:
                    reg[f"frac_{nome}"] = round(frac, 4)
                    # frac é a proporção dos pixels VÁLIDOS que cruzam o
                    # limiar; multiplicada pela contagem e pela área do pixel,
                    # dá hectares diretamente.
                    reg[f"area_{nome}_ha"] = round(frac * validos * area_px, 2)
            registros.append(reg)

    if falhas:
        log.warning("  %d bloco(s) sem resposta; reexecute os intervalos:", len(falhas))
        for a, b in falhas:
            log.warning("    --desde %s --ate %s", a, b)

    return registros


# ── análise ────────────────────────────────────────────────────────────────

def calibrar_cobertura(cfg: dict, registros: List[dict]) -> int:
    """Define a fração observada de cada cena e o rótulo de confiança.

    A referência é a maior contagem de pixels válidos já registrada na série
    — na prática, uma passagem de céu limpo sobre o reservatório inteiro.
    Isso dispensa constante fixa e se autocalibra: trocar o polígono ou a
    resolução ajusta a referência sozinho, sem editar código.
    """
    qual = cfg["qualidade"]
    validos = [int(r["pixels_validos"]) for r in registros
               if str(r.get("pixels_validos", "")).strip().isdigit()]
    ref = max(validos) if validos else 1

    descartados = 0
    for r in list(registros):
        try:
            v = int(r["pixels_validos"])
        except (KeyError, TypeError, ValueError):
            continue
        f = v / ref
        if f < qual["fracao_minima_ingestao"]:
            registros.remove(r)
            descartados += 1
            continue
        r["fracao_valida"] = round(f, 4)
        r["nuvem_pct"] = round((1 - f) * 100, 1)
        r["confianca"] = confianca(f, qual["fracao_confianca_media"],
                                   qual["fracao_confianca_alta"])
    log.info("  cobertura de referência: %d pixels válidos%s",
             ref, f" | {descartados} cena(s) descartada(s)" if descartados else "")
    return ref


def analisar(cfg: dict, registros: List[dict]) -> List[dict]:
    clim_cfg = cfg["climatologia"]
    qual = cfg["qualidade"]
    alerta_cfg = cfg["alerta"]

    for nome, icfg in cfg["indices"].items():
        clim = Climatologia.construir(
            registros, nome,
            clim_cfg["referencia_inicio"], clim_cfg["referencia_fim"],
            qual["confianca_minima_climatologia"],
            clim_cfg["n_minimo_por_mes"], clim_cfg["estimador"],
        )
        for reg in registros:
            z = clim.z(reg["data"], reg.get(nome))
            reg[f"{nome}_z"] = round(z, 4) if z is not None else None
            if icfg.get("alerta"):
                reg[f"{nome}_status"] = status(
                    z, icfg.get("direcao", "alta"),
                    icfg.get("z_atencao", 1.5), icfg.get("z_alerta", 2.0),
                )
        if icfg.get("alerta"):
            confirmar_persistencia(registros, nome,
                                   qual["confianca_minima_notificacao"],
                                   alerta_cfg["exigir_persistencia"])

    for reg in registros:
        achados = avaliar_combinacoes(reg, alerta_cfg.get("combinacoes_relevantes"))
        reg["combinacoes"] = "; ".join(achados)

    # Níveis só podem ser avaliados depois que todos os índices têm status.
    avaliar_niveis(registros, alerta_cfg)
    consolidar_persistencia_nivel(registros, alerta_cfg.get("exigir_persistencia", True))

    # A área em hectares só é comparável quando quase toda a represa foi
    # observada. Em cena encoberta o valor mede o que estava visível, não o
    # reservatório — e exibido sem ressalva sugere que ele encolheu.
    for reg in registros:
        reg["area_confiavel"] = "SIM" if reg.get("confianca") == "ALTA" else "NAO"

    n_vig = sum(1 for r in registros if r.get("nivel") == "VIGILANCIA")
    n_con = sum(1 for r in registros if r.get("nivel_confirmado") == "SIM")
    log.info("  níveis: %d em vigilância | %d confirmados", n_vig, n_con)
    return registros


# ── persistência ───────────────────────────────────────────────────────────

def mesclar(existentes: List[dict], novos: List[dict]) -> List[dict]:
    """Indexa por data: reexecutar sobre o mesmo intervalo é idempotente."""
    por_data = {r["data"]: r for r in existentes if r.get("data")}
    for r in novos:
        por_data[r["data"]] = r
    return [por_data[d] for d in sorted(por_data)]


def gravar(setor_id: str, registros: List[dict], cfg: dict) -> None:
    SAIDA.mkdir(parents=True, exist_ok=True)
    csv_path = SAIDA / f"{setor_id}.csv"

    campos: List[str] = []
    for r in registros:
        for k in r:
            if k not in campos:
                campos.append(k)

    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=campos, extrasaction="ignore")
        w.writeheader()
        w.writerows(registros)
    log.info("  %s: %d linhas", csv_path.relative_to(RAIZ), len(registros))

    if registros:
        ultimo = registros[-1]
        (SAIDA / f"{setor_id}_ultimo.json").write_text(
            json.dumps({
                "setor": setor_id,
                "observacao": ultimo,
                "atualizado_em": dt.datetime.now(dt.timezone.utc).isoformat(),
                "ressalvas": cfg["ressalvas"],
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def publicar_metadados(cfg: dict, setores: List[dict],
                       series: Dict[str, List[dict]]) -> None:
    """Escreve em docs/data/ o que a interface precisa além das séries:
    os polígonos, para desenhar o contorno, e um resumo por setor."""
    SAIDA.mkdir(parents=True, exist_ok=True)

    feicoes = []
    for setor in setores:
        geom = carregar_geometria(setor["arquivo"])
        feicoes.append({
            "type": "Feature",
            "properties": {"id": setor["id"], "nome": setor["nome"],
                           "principal": bool(setor.get("principal"))},
            "geometry": geom,
        })
    (SAIDA / "setores.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": feicoes},
                   ensure_ascii=False),
        encoding="utf-8",
    )

    resumo = {
        "local": cfg["local"],
        "atualizado_em": dt.datetime.now(dt.timezone.utc).isoformat(),
        "ressalvas": cfg["ressalvas"],
        "indices": {k: {"nome": v["nome"], "descricao": v["descricao"],
                        "alerta": bool(v.get("alerta"))}
                    for k, v in cfg["indices"].items()},
        "classificacoes": list(cfg["classificacao"].keys()),
        "setores": [{"id": s["id"], "nome": s["nome"],
                     "principal": bool(s.get("principal")),
                     "observacoes": len(series.get(s["id"], []))}
                    for s in setores],
        "contrastes": [{"id": c["id"], "nome": c["nome"]}
                       for c in (cfg.get("contrastes") or [])],
    }
    (SAIDA / "resumo.json").write_text(
        json.dumps(resumo, ensure_ascii=False, indent=2), encoding="utf-8")


def ler_existente(setor_id: str) -> List[dict]:
    p = SAIDA / f"{setor_id}.csv"
    if not p.exists():
        return []
    with open(p, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


# ── principal ──────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--desde", help="data inicial AAAA-MM-DD")
    ap.add_argument("--ate", help="data final AAAA-MM-DD")
    ap.add_argument("--setor", help="processar apenas este setor")
    args = ap.parse_args()

    cfg = carregar_config()
    hoje = dt.date.today()
    fim = dt.date.fromisoformat(args.ate) if args.ate else hoje
    inicio = (dt.date.fromisoformat(args.desde) if args.desde
              else fim - dt.timedelta(days=cfg["aquisicao"]["janela_dias"]))

    setores = [s for s in cfg["setores"]
               if not args.setor or s["id"] == args.setor]
    if not setores:
        log.error("Setor não encontrado em config.yaml: %s", args.setor)
        return 1

    log.info("Período %s a %s | %d setor(es)", inicio, fim, len(setores))
    token = sh.get_token()

    series: Dict[str, List[dict]] = {}
    for setor in setores:
        log.info("Setor %s", setor["id"])
        novos = coletar_setor(token, cfg, setor, inicio, fim)
        anteriores = ler_existente(setor["id"])
        if not novos:
            if not anteriores:
                # Falhar alto: gravar um CSV vazio e seguir em frente faz o
                # problema parecer resolvido quando não está.
                log.error(
                    "  Nenhuma observação obtida e não há série anterior. "
                    "Verifique acima se algum bloco foi recusado pela API — "
                    "erro 400 costuma indicar evalscript ou payload inválido."
                )
                return 2
            log.warning("  nenhuma observação nova no período; série mantida")
        completa = mesclar(anteriores, novos)
        calibrar_cobertura(cfg, completa)
        analisar(cfg, completa)
        gravar(setor["id"], completa, cfg)
        series[setor["id"]] = completa

    publicar_metadados(cfg, setores, series)

    # Validação independente: cruza as observações de campo com a série.
    # Roda sempre; se não houver observações, apenas registra isso no log.
    principal = next((s for s in setores if s.get("principal")), setores[0] if setores else None)
    if principal and series.get(principal["id"]):
        campo.executar(RAIZ, series[principal["id"]])

    for c in cfg.get("contrastes") or []:
        if c["a"] in series and c["b"] in series:
            log.info("Contraste %s", c["id"])
            diff = serie_contraste(series[c["a"]], series[c["b"]], c["indices"])
            gravar(c["id"], diff, cfg)

    log.info("Concluído.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

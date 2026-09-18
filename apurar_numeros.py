#!/usr/bin/env python3
"""
apurar_numeros.py
=================
Apura os números operacionais do sistema para preencher a Ficha de PTT
(Síntese quantitativa) e o Relatório (Quadros 3 e 4, Anexos III e IV).

Roda na raiz do repositório:

    python apurar_numeros.py

Para incluir as estatísticas do GitHub Actions (repositório público não
precisa de token; privado precisa):

    export GITHUB_TOKEN=ghp_xxx      # opcional
    python apurar_numeros.py --repo mauriciorschmitt/saolourencoalert

Imprime um bloco pronto para copiar nos documentos.
"""

import argparse
import csv
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

HISTORY_CSV = Path("docs/data/historico.csv")
ARCHIVE_DIR = Path("docs/images/archive")


# ── série histórica ─────────────────────────────────────────────────────────

def ler_serie():
    if not HISTORY_CSV.exists():
        sys.exit(f"Não encontrei {HISTORY_CSV}. Rode na raiz do repositório.")
    with open(HISTORY_CSV, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def analisar_serie(rows):
    datas = sorted(r["date"] for r in rows if r.get("date"))
    conf = Counter((r.get("confianca") or "—").upper() for r in rows)
    status = Counter((r.get("status") or "—").upper() for r in rows)
    confirmados = sum(
        1 for r in rows
        if (r.get("alerta_confirmado") or "").upper() == "SIM"
    )
    tem_colunas_novas = any(r.get("alerta_confirmado") for r in rows)

    por_ano = Counter(d[:4] for d in datas)
    return {
        "n": len(rows),
        "inicio": datas[0] if datas else "—",
        "fim": datas[-1] if datas else "—",
        "conf": conf,
        "status": status,
        "confirmados": confirmados,
        "tem_colunas_novas": tem_colunas_novas,
        "por_ano": por_ano,
    }


# ── imagens geradas ─────────────────────────────────────────────────────────

def contar_paineis():
    if not ARCHIVE_DIR.exists():
        return 0
    return sum(1 for p in ARCHIVE_DIR.glob("*/panel.png"))


# ── GitHub Actions ──────────────────────────────────────────────────────────

def apurar_actions(repo):
    try:
        import requests
    except ImportError:
        return None, "biblioteca requests não instalada"

    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    runs, page = [], 1
    while page <= 10:  # até 1000 execuções
        url = f"https://api.github.com/repos/{repo}/actions/runs"
        try:
            r = requests.get(url, headers=headers,
                             params={"per_page": 100, "page": page}, timeout=30)
        except Exception as exc:  # noqa: BLE001
            return None, f"falha na requisição: {exc}"
        if r.status_code == 404:
            return None, "repositório não encontrado ou privado (defina GITHUB_TOKEN)"
        if not r.ok:
            return None, f"HTTP {r.status_code}: {r.text[:120]}"
        lote = r.json().get("workflow_runs", [])
        if not lote:
            break
        runs.extend(lote)
        if len(lote) < 100:
            break
        page += 1

    if not runs:
        return None, "nenhuma execução retornada"

    por_wf = Counter(x["name"] for x in runs)
    por_res = Counter(x.get("conclusion") or "em andamento" for x in runs)
    datas = sorted(x["created_at"][:10] for x in runs)

    duracoes = []
    for x in runs:
        try:
            ini = datetime.fromisoformat(x["created_at"].replace("Z", "+00:00"))
            fim = datetime.fromisoformat(x["updated_at"].replace("Z", "+00:00"))
            duracoes.append((fim - ini).total_seconds() / 60)
        except Exception:  # noqa: BLE001
            pass

    return {
        "total": len(runs),
        "por_workflow": por_wf,
        "por_resultado": por_res,
        "primeira": datas[0],
        "ultima": datas[-1],
        "dur_media": sum(duracoes) / len(duracoes) if duracoes else None,
    }, None


# ── saída ───────────────────────────────────────────────────────────────────

def pct(parte, total):
    return f"{parte} ({parte / total * 100:.1f}%)" if total else "0"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="mauriciorschmitt/saolourencoalert")
    ap.add_argument("--sem-actions", action="store_true")
    args = ap.parse_args()

    rows = ler_serie()
    s = analisar_serie(rows)
    paineis = contar_paineis()

    print("=" * 68)
    print("SÉRIE HISTÓRICA  (docs/data/historico.csv)")
    print("=" * 68)
    print(f"Observações válidas .......... {s['n']}")
    print(f"Período ...................... {s['inicio']} a {s['fim']}")
    print(f"Painéis de imagem gerados .... {paineis}")
    print("\nPor confiança:")
    for k in ("ALTA", "MEDIA", "BAIXA"):
        if s["conf"].get(k):
            print(f"  {k:<6} {pct(s['conf'][k], s['n'])}")
    print("\nPor status:")
    for k, v in s["status"].most_common():
        print(f"  {k:<8} {pct(v, s['n'])}")

    if s["tem_colunas_novas"]:
        print(f"\nAlertas CONFIRMADOS (2 passagens) ... {s['confirmados']}")
    else:
        print("\n⚠️  A coluna 'alerta_confirmado' não existe neste CSV.")
        print("    Rode o backfill com a versão nova do código antes de")
        print("    informar qualquer contagem de alertas nos documentos.")

    print("\nObservações por ano:")
    for ano in sorted(s["por_ano"]):
        print(f"  {ano}: {s['por_ano'][ano]}")

    if not args.sem_actions:
        print("\n" + "=" * 68)
        print(f"GITHUB ACTIONS  ({args.repo})")
        print("=" * 68)
        a, erro = apurar_actions(args.repo)
        if erro:
            print(f"não apurado: {erro}")
            print("Alternativa manual: aba Actions do repositório, filtre por")
            print("workflow e leia o total em cada aba de resultado.")
        else:
            print(f"Execuções totais ............. {a['total']}")
            print(f"Primeira ..................... {a['primeira']}")
            print(f"Última ....................... {a['ultima']}")
            if a["dur_media"]:
                print(f"Duração média ................ {a['dur_media']:.1f} min")
            print("\nPor workflow:")
            for k, v in a["por_workflow"].most_common():
                print(f"  {k}: {v}")
            print("\nPor resultado:")
            for k, v in a["por_resultado"].most_common():
                print(f"  {k:<14} {pct(v, a['total'])}")

    print("\n" + "=" * 68)
    print("ATENÇÃO AO INTERPRETAR")
    print("=" * 68)
    print(
        "As observações da série NÃO equivalem a execuções automatizadas: a\n"
        "maior parte foi produzida de uma vez pelo backfill histórico. Nos\n"
        "documentos, informe os dois números separadamente — 'imagens\n"
        "processadas' (série) e 'execuções automatizadas' (Actions) — e diga\n"
        "quantas observações vieram de execução agendada. Somar tudo como se\n"
        "fosse operação contínua seria incorreto."
    )


if __name__ == "__main__":
    main()

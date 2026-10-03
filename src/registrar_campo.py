#!/usr/bin/env python3
"""
registrar_campo.py
==================
Converte uma issue de observação de campo numa linha de data/campo.csv.

Executado pelo workflow registrar_campo.yml quando uma issue com o rótulo
`observacao-campo` é aberta. O observador preenche o formulário em
docs/campo.html, anexa as fotos na issue e envia — não precisa editar
arquivo nenhum.

Uso:
    python -m src.registrar_campo --corpo "$CORPO" --autor fulano --issue 42
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import re
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
CSV = RAIZ / "data" / "campo.csv"

COLUNAS = ["data", "hora", "ponto", "latitude", "longitude", "cobertura",
           "especie", "metodo", "observador", "fotos", "notas", "issue"]

CATEGORIAS = {"AUSENTE", "ISOLADA", "MODERADA", "EXTENSA"}
METODOS = {"margem", "barco", "drone", "outro"}


def extrair_campos(corpo: str) -> dict:
    """Lê os pares `chave: valor` do bloco de dados da issue."""
    bloco = re.search(r"```dados\s*(.*?)```", corpo, re.S)
    texto = bloco.group(1) if bloco else corpo
    dados = {}
    for linha in texto.splitlines():
        m = re.match(r"\s*([a-zç]+)\s*:\s*(.*?)\s*$", linha, re.I)
        if m:
            dados[m.group(1).lower()] = m.group(2)
    return dados


def extrair_fotos(corpo: str) -> str:
    """URLs de imagem anexadas à issue, separadas por espaço.

    O GitHub hospeda os anexos e os insere no corpo como markdown de imagem
    ou link direto — o que resolve, de graça, onde guardar as fotos.
    """
    urls = re.findall(r"!\[[^\]]*\]\((https?://[^)\s]+)\)", corpo)
    urls += re.findall(r"<img[^>]+src=\"(https?://[^\"]+)\"", corpo)
    soltas = re.findall(
        r"(?<![(\"])\bhttps?://(?:user-images|github\.com/user-attachments)"
        r"[^\s)>\"]+", corpo)
    vistas, saida = set(), []
    for u in urls + soltas:
        if u not in vistas:
            vistas.add(u)
            saida.append(u)
    return " ".join(saida)


def validar(d: dict) -> tuple[bool, str]:
    data = d.get("data", "")
    try:
        dia = dt.date.fromisoformat(data)
    except ValueError:
        return False, f"data inválida: '{data}' (esperado AAAA-MM-DD)"
    if dia > dt.date.today() + dt.timedelta(days=1):
        return False, f"data no futuro: {data}"

    cob = (d.get("cobertura") or "").upper()
    if cob not in CATEGORIAS:
        return False, f"cobertura '{cob}' não é uma de {sorted(CATEGORIAS)}"

    for chave, lim in (("latitude", 90), ("longitude", 180)):
        v = (d.get(chave) or "").strip()
        if v:
            try:
                if abs(float(v)) > lim:
                    return False, f"{chave} fora de faixa: {v}"
            except ValueError:
                return False, f"{chave} não é numérica: '{v}'"
    return True, ""


def ja_registrada(issue: str) -> bool:
    if not CSV.exists() or not issue:
        return False
    with open(CSV, encoding="utf-8", newline="") as f:
        return any((r.get("issue") or "") == str(issue) for r in csv.DictReader(f))


def acrescentar(linha: dict) -> None:
    existe = CSV.exists() and CSV.stat().st_size > 0
    CSV.parent.mkdir(parents=True, exist_ok=True)
    cabecalho_atual = None
    if existe:
        with open(CSV, encoding="utf-8", newline="") as f:
            cabecalho_atual = next(csv.reader(f), None)

    # Reescreve o arquivo inteiro se o cabeçalho ainda não tem todas as
    # colunas — acontece na primeira observação, com o CSV recém-criado.
    if cabecalho_atual and cabecalho_atual != COLUNAS:
        with open(CSV, encoding="utf-8", newline="") as f:
            antigas = list(csv.DictReader(f))
        with open(CSV, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=COLUNAS, extrasaction="ignore")
            w.writeheader()
            for r in antigas:
                w.writerow(r)
            w.writerow(linha)
        return

    with open(CSV, "a" if existe else "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUNAS, extrasaction="ignore")
        if not existe:
            w.writeheader()
        w.writerow(linha)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpo", required=True)
    ap.add_argument("--autor", default="")
    ap.add_argument("--issue", default="")
    args = ap.parse_args()

    d = extrair_campos(args.corpo)
    ok, motivo = validar(d)
    if not ok:
        print(f"::error::Observação recusada — {motivo}")
        return 1

    if ja_registrada(args.issue):
        print(f"Issue #{args.issue} já registrada; nada a fazer.")
        return 0

    metodo = (d.get("metodo") or "margem").lower()
    linha = {
        "data": d["data"],
        "hora": d.get("hora", ""),
        "ponto": d.get("ponto", ""),
        "latitude": d.get("latitude", ""),
        "longitude": d.get("longitude", ""),
        "cobertura": d["cobertura"].upper(),
        "especie": d.get("especie", ""),
        "metodo": metodo if metodo in METODOS else "outro",
        "observador": d.get("observador") or args.autor,
        "fotos": extrair_fotos(args.corpo),
        "notas": (d.get("notas") or "").replace("\n", " "),
        "issue": args.issue,
    }
    acrescentar(linha)
    print(f"Registrada: {linha['data']} · {linha['ponto'] or 'sem ponto'} · "
          f"{linha['cobertura']}" +
          (f" · {len(linha['fotos'].split())} foto(s)" if linha["fotos"] else " · sem fotos"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

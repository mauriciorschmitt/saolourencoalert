"""
evalscripts.py
==============
Monta o evalscript enviado à Statistical API do Sentinel Hub.

O cálculo acontece do lado do servidor: a API devolve apenas estatísticas
agregadas por polígono, nunca a imagem. Isso mantém o consumo de unidades
de processamento baixo o suficiente para caber na camada gratuita.

Duas famílias de saída são produzidas:

  índices contínuos  -- média, desvio, mín/máx por data e setor;
  máscaras binárias  -- 0 ou 1 por pixel. A MÉDIA de uma máscara binária é
                        a fração de pixels válidos que cruzam o limiar, e
                        multiplicada pela área do pixel vira HECTARES.

A segunda família é o que diferencia este sistema do anterior: em vez de
"o NDVI subiu", ele responde "X hectares passaram a ter cobertura
flutuante" -- informação diretamente utilizável por quem gere o
reservatório.
"""

from typing import Dict, List

# Comprimentos de onda centrais (nm) das bandas Sentinel-2 usadas no FAI.
WL_RED, WL_NIR, WL_SWIR = 665.0, 842.0, 1610.0
FAI_SLOPE = (WL_NIR - WL_RED) / (WL_SWIR - WL_RED)  # ≈ 0.1873

# B02 fica de fora: nenhuma fórmula atual a usa, e cada banda pedida
# consome unidades de processamento da cota gratuita.
BANDAS = ["B03", "B04", "B05", "B08", "B11", "SCL", "dataMask"]

# Índices contínuos. Cada entrada vira uma saída da API.
# EPS evita divisão por zero quando ambas as bandas são nulas.
FORMULAS: Dict[str, str] = {
    "ndvi": "idx(s.B08, s.B04)",
    "ndwi": "idx(s.B03, s.B08)",
    "ndmi": "idx(s.B08, s.B11)",
    "ndci": "idx(s.B05, s.B04)",
    "ndti": "idx(s.B04, s.B03)",
    # FAI: subtração da linha de base traçada entre vermelho e SWIR.
    # Menos sensível a aerossol e a variação atmosférica que o NDVI, o que
    # importa numa série de dez anos com correção atmosférica imperfeita.
    "fai": f"s.B08 - (s.B04 + (s.B11 - s.B04) * {FAI_SLOPE:.6f})",
    # Proxy de matéria orgânica dissolvida. Razão simples, não normalizada:
    # a literatura de CDOM em água interior usa razões verde/vermelho.
    # Limitada a um intervalo plausível para não explodir a estatística.
    "cdom": "clamp(s.B03 / Math.max(s.B04, EPS), 0.0, 10.0)",
}


def _js_mask_expr(indice: str, limiar: float, operador: str) -> str:
    op = ">" if operador == "maior" else "<"
    return f"(({indice}) {op} {limiar}) ? 1 : 0"


def build_evalscript(indices: List[str], classificacoes: Dict[str, dict]) -> str:
    """Gera o evalscript para os índices e classificações pedidos.

    indices        -- chaves de FORMULAS a calcular.
    classificacoes -- {nome: {"indice": ..., "limiar": ..., "operador": ...}}
    """
    desconhecidos = [i for i in indices if i not in FORMULAS]
    if desconhecidos:
        raise ValueError(f"índice(s) não implementado(s): {desconhecidos}")

    saidas = []
    for nome in indices:
        saidas.append(f'    {{ id: "{nome}", bands: 1, sampleType: "FLOAT32" }}')
    for nome in classificacoes:
        saidas.append(f'    {{ id: "cls_{nome}", bands: 1, sampleType: "FLOAT32" }}')
    saidas.append('    { id: "dataMask", bands: 1 }')

    # Atribuições locais: cada índice vira uma variável JS reutilizável,
    # inclusive pelas máscaras de classificação.
    calc = "\n".join(
        f"  const {nome} = {FORMULAS[nome]};" for nome in
        sorted(set(indices) | {c["indice"] for c in classificacoes.values()})
    )

    retornos = [f'    {nome}: [{nome}]' for nome in indices]
    for nome, cfg in classificacoes.items():
        expr = _js_mask_expr(cfg["indice"], cfg["limiar"], cfg["operador"])
        retornos.append(f'    cls_{nome}: [{expr}]')
    retornos.append("    dataMask: [valid]")

    return f"""//VERSION=3
// Gerado automaticamente por src/evalscripts.py — não editar à mão.
const EPS = 1e-6;
const SCL_DESCARTADAS = SCL_LISTA;

function setup() {{
  return {{
    input: [{{ bands: {BANDAS} }}],
    output: [
{chr(10).join(s + "," for s in saidas[:-1])}
{saidas[-1]}
    ]
  }};
}}

function idx(a, b) {{
  const d = a + b;
  return Math.abs(d) < EPS ? 0 : (a - b) / d;
}}

function clamp(v, lo, hi) {{
  return isFinite(v) ? Math.min(Math.max(v, lo), hi) : lo;
}}

function pixelValido(s) {{
  if (s.dataMask !== 1) return 0;
  if (SCL_DESCARTADAS.indexOf(s.SCL) >= 0) return 0;
  return 1;
}}

function evaluatePixel(s) {{
  const valid = pixelValido(s);
{calc}
  return {{
{chr(10).join(r + "," for r in retornos[:-1])}
{retornos[-1]}
  }};
}}
"""


def render(indices: List[str], classificacoes: Dict[str, dict],
           scl_descartadas: List[int]) -> str:
    """Evalscript pronto, com a lista de classes SCL injetada."""
    return build_evalscript(indices, classificacoes).replace(
        "SCL_LISTA", str(list(scl_descartadas))
    )

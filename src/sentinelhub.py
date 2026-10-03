"""
sentinelhub.py
==============
Autenticação e consulta à Statistical API do Sentinel Hub, no Copernicus
Data Space Ecosystem.

Conta gratuita: cadastro em dataspace.copernicus.eu, depois um cliente OAuth
em Settings → OAuth clients. As credenciais entram como variáveis de
ambiente SH_CLIENT_ID e SH_CLIENT_SECRET — nunca no código.

A camada gratuita tem cota mensal de unidades de processamento. Como este
sistema pede apenas estatísticas agregadas sobre polígonos pequenos, e não
imagens, o consumo por requisição é baixo: o custo cresce com o número de
setores × execuções, não com o tamanho da série.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import time
from typing import Dict, Iterable, List, Optional

import requests

log = logging.getLogger(__name__)

TOKEN_URL = ("https://identity.dataspace.copernicus.eu/auth/realms/CDSE/"
             "protocol/openid-connect/token")
STATS_URL = "https://sh.dataspace.copernicus.eu/api/v1/statistics"

MAX_RETRIES = 4
BACKOFF_BASE = 2.0
TIMEOUT = 180


class SentinelHubError(RuntimeError):
    pass


# ── autenticação ───────────────────────────────────────────────────────────

def get_token() -> str:
    # strip() é essencial: colar credencial num campo de secret costuma
    # arrastar espaço ou quebra de linha invisível, e o servidor rejeita o
    # valor sem dizer por quê. Esta linha resolve a causa mais frequente
    # de 401 em primeira configuração.
    cid = (os.environ.get("SH_CLIENT_ID") or "").strip()
    secret = (os.environ.get("SH_CLIENT_SECRET") or "").strip()

    if not cid or not secret:
        raise SentinelHubError(
            "Defina SH_CLIENT_ID e SH_CLIENT_SECRET. Em execução local use "
            "variáveis de ambiente; no GitHub Actions, secrets do repositório."
        )

    resp = requests.post(
        TOKEN_URL,
        data={"grant_type": "client_credentials",
              "client_id": cid, "client_secret": secret},
        timeout=60,
    )
    if resp.ok:
        return resp.json()["access_token"]

    # Diagnóstico sem vazar segredo: comprimento e formato do client_id,
    # apenas o comprimento do secret, e a resposta do servidor.
    detalhe = resp.text[:200].replace(secret, "***") if secret else resp.text[:200]
    raise SentinelHubError(
        f"Falha na autenticação (HTTP {resp.status_code}).\n"
        f"  resposta do servidor: {detalhe}\n"
        f"  SH_CLIENT_ID: {len(cid)} caracteres, "
        f"formato UUID: {'sim' if len(cid) == 36 and cid.count('-') == 4 else 'NÃO'}\n"
        f"  SH_CLIENT_SECRET: {len(secret)} caracteres\n"
        "  O client_id do CDSE é um UUID (36 caracteres, 4 hífens). "
        "Se o formato acima indicar NÃO, os dois valores podem ter sido "
        "trocados entre si, ou o valor colado não é o do cliente OAuth."
    )


# ── geometria ──────────────────────────────────────────────────────────────

def utm_epsg(lon: float, lat: float) -> int:
    """EPSG UTM/WGS84 adequado ao ponto. Processar em UTM mantém o pixel
    métrico e quadrado, condição para converter contagem em hectares."""
    zona = int((lon + 180) // 6) + 1
    return (32700 if lat < 0 else 32600) + zona


def centroide(geom: dict) -> tuple:
    """Centroide aproximado do primeiro anel — suficiente para escolher a
    zona UTM, sem exigir shapely como dependência."""
    anel = geom["coordinates"][0]
    xs = [p[0] for p in anel]
    ys = [p[1] for p in anel]
    return sum(xs) / len(xs), sum(ys) / len(ys)


def reprojetar_utm(geom: dict, recuo_m: float = 0.0) -> tuple:
    """Converte um Polygon em WGS84 para a zona UTM correspondente, aplicando
    opcionalmente um recuo para dentro.

    A API interpreta resx/resy na unidade do CRS declarado. Enviar a
    geometria em graus e pedir resolução 10 produziria pixels de 10 GRAUS —
    daí a reprojeção ser obrigatória, e não um refinamento.

    O recuo remove o anel de pixels que cavalga a linha d'água. Num pixel de
    10 m, um recuo de 10 m descarta exatamente a faixa em que água e margem
    se misturam dentro do mesmo pixel, onde vegetação ripária contamina o
    índice sem que nada tenha acontecido na lâmina.

    Recuos maiores desconectam braços estreitos e fragmentam o polígono; a
    função aceita o resultado como MultiPolygon, mas avisa no log.
    """
    from pyproj import Transformer
    from shapely.geometry import shape, mapping, MultiPolygon
    from shapely.ops import transform as sh_transform

    lon, lat = centroide(geom)
    epsg = utm_epsg(lon, lat)
    tr = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)

    g = sh_transform(lambda x, y, z=None: tr.transform(x, y), shape(geom))
    if not g.is_valid:
        g = g.buffer(0)
    area_ini = g.area

    if recuo_m and recuo_m > 0:
        g = g.buffer(-abs(recuo_m))
        if g.is_empty:
            raise SentinelHubError(
                f"O recuo de {recuo_m} m eliminou o polígono inteiro. "
                "Reduza aquisicao.recuo_borda_m em config.yaml."
            )
        partes = len(g.geoms) if isinstance(g, MultiPolygon) else 1
        log.info(
            "  recuo de %g m: %.2f ha -> %.2f ha (-%.0f%%), %d parte(s)",
            recuo_m, area_ini / 10_000, g.area / 10_000,
            (1 - g.area / area_ini) * 100, partes,
        )
        if partes > 1:
            log.warning(
                "  o recuo fragmentou o polígono em %d partes: braços "
                "estreitos se desconectaram. Se não for intencional, use um "
                "recuo menor.", partes
            )

    return mapping(g), epsg


def area_pixel_ha(resolucao_m: int) -> float:
    """Hectares por pixel — converte contagem de pixels em área."""
    return (resolucao_m ** 2) / 10_000.0


# ── consulta ───────────────────────────────────────────────────────────────

def _post_com_retry(url: str, headers: dict, payload: dict) -> dict:
    ultimo = None
    for tentativa in range(1, MAX_RETRIES + 1):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=TIMEOUT)
        except requests.RequestException as exc:
            ultimo = str(exc)
        else:
            if r.ok:
                return r.json()
            # 429 e 5xx são transitórios; 4xx restantes não adianta repetir.
            if r.status_code != 429 and r.status_code < 500:
                raise SentinelHubError(f"HTTP {r.status_code}: {r.text[:300]}")
            ultimo = f"HTTP {r.status_code}"
        espera = BACKOFF_BASE ** tentativa
        log.warning("Tentativa %d/%d falhou (%s); aguardando %.0f s",
                    tentativa, MAX_RETRIES, ultimo, espera)
        time.sleep(espera)
    raise SentinelHubError(f"Esgotadas as tentativas: {ultimo}")


def consultar(token: str, geometria: dict, evalscript: str,
              inicio: dt.date, fim: dt.date, saidas: Iterable[str],
              colecao: str = "sentinel-2-l2a", resolucao: int = 10,
              recuo_m: float = 0.0) -> List[dict]:
    """Uma requisição estatística para um polígono e um intervalo de datas.

    Retorna a lista bruta de intervalos devolvida pela API, um por data com
    observação disponível.
    """
    geom_utm, epsg = reprojetar_utm(geometria, recuo_m)

    calculos = {s: {"statistics": {"default": {}}} for s in saidas}

    payload = {
        "input": {
            "bounds": {
                "geometry": geom_utm,
                "properties": {
                    "crs": f"http://www.opengis.net/def/crs/EPSG/0/{epsg}"
                },
            },
            "data": [{"type": colecao}],
        },
        "aggregation": {
            "timeRange": {
                "from": f"{inicio.isoformat()}T00:00:00Z",
                "to": f"{fim.isoformat()}T23:59:59Z",
            },
            "aggregationInterval": {"of": "P1D"},
            "resx": resolucao,
            "resy": resolucao,
            "evalscript": evalscript,
        },
        "calculations": calculos,
    }
    headers = {"Authorization": f"Bearer {token}",
               "Content-Type": "application/json"}

    dados = _post_com_retry(STATS_URL, headers, payload)
    return dados.get("data", [])


def extrair(intervalo: dict, saida: str) -> Optional[dict]:
    """Estatísticas de uma saída do evalscript, ou None se ausente."""
    return (intervalo.get("outputs", {})
                     .get(saida, {})
                     .get("bands", {})
                     .get("B0", {})
                     .get("stats"))

#!/usr/bin/env python3
"""
monitor_ndvi.py  –  v3
========================
Monitoramento automatizado do Reservatório São Lourenço (Mafra, SC).

O que este script faz em cada execução
----------------------------------------
1.  Autentica via OAuth2 no Copernicus Data Space Ecosystem (token único
    gerado uma vez e reutilizado em todas as chamadas).
2.  Consulta a API Estatística do Sentinel Hub para a janela dos últimos
    LOOKBACK_DAYS dias, obtendo NDVI, NDWI e NDMI para TODAS as cenas
    com pelo menos MIN_VALID_FRACTION de pixels válidos (pós-mascaramento
    SCL de nuvens).
3.  Identifica a "melhor cena" (menor cobertura de nuvens) e a usa para
    o alerta semanal e para o z-score.
4.  Gera um painel de imagem composto (cor real | NDVI | NDWI) para a
    melhor cena e para qualquer cena adicional ainda sem imagem no arquivo.
5.  Persiste:
      docs/data/ultimo.json          → estado mais recente (melhor cena)
      docs/data/historico.csv        → série histórica acumulada
      docs/data/passagens_30d.json   → TODAS as cenas do período + flag "best"
      docs/images/latest/panel.png   → painel da melhor cena
      docs/images/archive/YYYY-MM-DD/panel.png → arquivo por data
6.  Envia notificações via Telegram, e-mail e/ou WhatsApp (CallMeBot),
    incluindo o nível de alerta do módulo de qualidade da água, que roda
    antes no mesmo workflow (docs/data/represa_ultimo.json).
7.  Todas as chamadas HTTP têm retry com backoff exponencial.
"""

import csv
import datetime as dt
import io
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Optional, Tuple

import requests
import yaml
from pyproj import Transformer

from src import notificacao, sentinelhub as sh

# ── Logging estruturado ──────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    stream=sys.stdout,
)
log = logging.getLogger("monitor_ndvi")

# ═══════════════════════════════════════════════════════════════════════════════
# 1. CONFIGURAÇÃO
# ═══════════════════════════════════════════════════════════════════════════════

SCHEMA_VERSION = "3.0"

# Climatologia mensal de referência (período pré-surto, 2016-2024).
# Copie estes valores da planilha Salvinia_Outbreak_Statistical_Results.xlsx,
# aba Monthly_Climatology (colunas ndvi_clim_mean e ndvi_clim_sd).
MONTHLY_CLIMATOLOGY = {
    1:  {"mean":  0.062983286, "sd": 0.022422375},
    2:  {"mean":  0.102973448, "sd": 0.056671219},
    3:  {"mean":  0.129464011, "sd": 0.060937651},
    4:  {"mean":  0.131759039, "sd": 0.077113975},
    5:  {"mean":  0.081960490, "sd": 0.097855931},
    6:  {"mean":  0.005082073, "sd": 0.091559002},
    7:  {"mean":  0.058218087, "sd": 0.097758882},
    8:  {"mean":  0.039807745, "sd": 0.090688466},
    9:  {"mean":  0.052855900, "sd": 0.074378438},
    10: {"mean": -0.065839763, "sd": 0.137611379},
    11: {"mean":  0.079915785, "sd": 0.091192902},
    12: {"mean":  0.017970982, "sd": 0.152124159},
}

ROI_PATH = Path(__file__).parent / "data" / "roi.geojson"
with open(ROI_PATH, "r", encoding="utf-8") as _f:
    ROI_GEOJSON = json.load(_f)

CLOUD_THRESHOLD    = 10     # % — informativo, não bloqueia
ALERT_Z_THRESHOLD  = 2.0   # desvios-padrão (mesmo do artigo)
ATTENTION_Z        = 1.5   # limiar intermediário
LOOKBACK_DAYS      = 30    # janela de busca
MIN_VALID_FRACTION = 0.01  # descarta apenas cenas ~100% nubladas

# ── Confiabilidade da observação ───────────────────────────────────────────
# O filtro de ingestão continua permissivo de propósito: queremos TODAS as
# passagens no histórico. Mas uma média calculada sobre 5% da represa não é
# comparável a uma calculada sobre 100%, então cada observação é rotulada e
# o painel/as notificações tratam os rótulos de forma diferente.
RELIABLE_VALID_FRACTION = 0.70   # >= : confiança ALTA
USABLE_VALID_FRACTION   = 0.30   # >= : confiança MÉDIA (abaixo: BAIXA)

# Confiança mínima para DISPARAR notificação de alerta.
# "ALTA" (só cenas limpas) | "MEDIA" (padrão) | "BAIXA" (notifica sempre).
# Observações abaixo do corte continuam gravadas no histórico e visíveis no
# painel -- apenas não geram push. Evita que alerta falso recorrente treine
# o destinatário a ignorar todos os alertas.
NOTIFY_MIN_CONFIDENCE = "MEDIA"

# ── Persistência do alerta ─────────────────────────────────────────────────
# Nuvem residual produz pico de NDVI em UMA cena isolada; macrófita não
# desaparece em cinco dias. Exigir que duas observações qualificadas
# consecutivas ultrapassem o limiar rejeita o primeiro caso sem descartar
# nenhuma imagem do histórico -- que é o ponto: o filtro de ingestão continua
# em 1% e todas as passagens seguem registradas e visíveis no painel.
#
# Medido sobre a série 2016-2026: alertas anteriores ao surto caem de 31 para
# 5, mantendo 17 das 29 detecções durante o evento confirmado.
ALERT_REQUIRE_PERSISTENCE = True

# Pixels dentro do polígono (não da bounding box). Ver artigo, Seção 2.2.
# Para recalibrar: rode com MIN_VALID_FRACTION=0.001, colete os maiores
# valores de valid_px em datas visivelmente sem nuvem e use o maior.
GEOMETRY_PIXEL_COUNT = 8761

HTTP_MAX_RETRIES = 4
HTTP_BACKOFF_BASE = 2  # segundos (dobra a cada tentativa)

SH_TOKEN_URL   = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
SH_STATS_URL   = "https://sh.dataspace.copernicus.eu/api/v1/statistics"
SH_PROCESS_URL = "https://sh.dataspace.copernicus.eu/api/v1/process"
PANEL_IMAGE_SIZE = 512
PANEL_URL = "mauriciorschmitt.github.io/saolourencoalert"  # exibido nas notificações

# ═══════════════════════════════════════════════════════════════════════════════
# 2. EVALSCRIPTS (inalterados — mesma matemática de bandas do artigo)
# ═══════════════════════════════════════════════════════════════════════════════

EVALSCRIPT_INDICES = """
//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B08", "B04", "B03", "B11", "SCL", "dataMask"] }],
    output: [
      { id: "ndvi", bands: 1, sampleType: "FLOAT32" },
      { id: "ndwi", bands: 1, sampleType: "FLOAT32" },
      { id: "ndmi", bands: 1, sampleType: "FLOAT32" },
      { id: "dataMask", bands: 1 }
    ]
  };
}
function evaluatePixel(s) {
  // SCL: 3=sombra, 8=nuvem média, 9=nuvem alta, 10=cirro fino
  let isCloud = (s.SCL == 3 || s.SCL == 8 || s.SCL == 9 || s.SCL == 10);
  let valid = s.dataMask == 1 && !isCloud;
  let ndvi = (s.B08 + s.B04) == 0 ? 0 : (s.B08 - s.B04) / (s.B08 + s.B04);
  let ndwi = (s.B03 + s.B08) == 0 ? 0 : (s.B03 - s.B08) / (s.B03 + s.B08);
  let ndmi = (s.B08 + s.B11) == 0 ? 0 : (s.B08 - s.B11) / (s.B08 + s.B11);
  return { ndvi: [ndvi], ndwi: [ndwi], ndmi: [ndmi], dataMask: [valid ? 1 : 0] };
}
"""

EVALSCRIPT_TRUECOLOR = """
//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B04", "B03", "B02", "SCL", "dataMask"] }],
    output: { bands: 3, sampleType: "AUTO" }
  };
}
function evaluatePixel(s) {
  let isCloud = (s.SCL == 3 || s.SCL == 8 || s.SCL == 9 || s.SCL == 10);
  if (s.dataMask == 0) return [1, 1, 1];
  if (isCloud) return [0.85, 0.85, 0.85];
  let gain = 3.0, gamma = 1.8;
  return [
    Math.pow(s.B04 * gain, 1 / gamma),
    Math.pow(s.B03 * gain, 1 / gamma),
    Math.pow(s.B02 * gain, 1 / gamma),
  ];
}
"""

_NDVI_COLOR_RAMP = """
function ndviColor(v) {
  if (v < -0.2) return [0.65, 0.55, 0.40];
  if (v < 0.0)  return [0.80, 0.75, 0.55];
  if (v < 0.2)  return [0.90, 0.88, 0.55];
  if (v < 0.4)  return [0.65, 0.80, 0.35];
  if (v < 0.6)  return [0.30, 0.65, 0.20];
  return [0.05, 0.45, 0.05];
}
"""

EVALSCRIPT_NDVI_VIZ = ("""
//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B08", "B04", "SCL", "dataMask"] }],
    output: { bands: 3, sampleType: "AUTO" }
  };
}
""" + _NDVI_COLOR_RAMP + """
function evaluatePixel(s) {
  let isCloud = (s.SCL == 3 || s.SCL == 8 || s.SCL == 9 || s.SCL == 10);
  if (s.dataMask == 0) return [1, 1, 1];
  if (isCloud) return [0.85, 0.85, 0.85];
  return ndviColor((s.B08 - s.B04) / (s.B08 + s.B04));
}
""")

_NDWI_COLOR_RAMP = """
function ndwiColor(v) {
  if (v < -0.2) return [0.80, 0.75, 0.55];
  if (v < 0.0)  return [0.85, 0.85, 0.65];
  if (v < 0.2)  return [0.65, 0.80, 0.90];
  if (v < 0.4)  return [0.30, 0.60, 0.85];
  return [0.05, 0.25, 0.65];
}
"""

EVALSCRIPT_NDWI_VIZ = ("""
//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B03", "B08", "SCL", "dataMask"] }],
    output: { bands: 3, sampleType: "AUTO" }
  };
}
""" + _NDWI_COLOR_RAMP + """
function evaluatePixel(s) {
  let isCloud = (s.SCL == 3 || s.SCL == 8 || s.SCL == 9 || s.SCL == 10);
  if (s.dataMask == 0) return [1, 1, 1];
  if (isCloud) return [0.85, 0.85, 0.85];
  return ndwiColor((s.B03 - s.B08) / (s.B03 + s.B08));
}
""")

# ═══════════════════════════════════════════════════════════════════════════════
# 3. HTTP COM RETRY E BACKOFF EXPONENCIAL
# ═══════════════════════════════════════════════════════════════════════════════

def _http_post(url, *, max_retries=HTTP_MAX_RETRIES, backoff=HTTP_BACKOFF_BASE, **kwargs):
    """
    requests.post() com retry automático para erros transitórios (5xx,
    timeout, ConnectionError). Erros 4xx não são retentados — indicam
    problema de configuração, não de rede.
    """
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, **kwargs)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", backoff * attempt))
                log.warning("Rate-limited (429); aguardando %ds.", wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                log.warning("Servidor %d na tentativa %d/%d; retentando...",
                            resp.status_code, attempt, max_retries)
                time.sleep(backoff ** attempt)
                continue
            return resp
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
            wait = backoff ** attempt
            log.warning("Erro de rede (%s); tentativa %d/%d – aguardando %.0fs.",
                        exc, attempt, max_retries, wait)
            time.sleep(wait)
    raise RuntimeError(
        f"Todas as {max_retries} tentativas falharam para {url}. Último erro: {last_exc}"
    )

# ═══════════════════════════════════════════════════════════════════════════════
# 4. AUTENTICAÇÃO  (mesma credencial do módulo de qualidade da água)
# ═══════════════════════════════════════════════════════════════════════════════

def credencial() -> sh.Credencial:
    """Credencial que renova o token sozinha. Obtém o primeiro token já
    aqui, para falhar cedo se os secrets estiverem errados."""
    log.info("Obtendo token OAuth2 do Copernicus CDSE...")
    cred = sh.Credencial()
    cred.renovar()
    log.info("Token obtido com sucesso.")
    return cred

# ═══════════════════════════════════════════════════════════════════════════════
# 5. GEOMETRIA
# ═══════════════════════════════════════════════════════════════════════════════

def _iter_coords(coords):
    if isinstance(coords[0], (int, float)):
        yield coords
    else:
        for item in coords:
            yield from _iter_coords(item)

def reproject_to_utm(geometry: dict) -> Tuple[dict, int]:
    """Reprojeta geometria GeoJSON de EPSG:4326 para a zona UTM local."""
    pts = list(_iter_coords(geometry["coordinates"]))
    lon_c = sum(p[0] for p in pts) / len(pts)
    lat_c = sum(p[1] for p in pts) / len(pts)
    zone = int((lon_c + 180) // 6) + 1
    epsg = (32700 if lat_c < 0 else 32600) + zone
    t = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)

    def _tx(coords):
        if isinstance(coords[0], (int, float)):
            x, y = t.transform(coords[0], coords[1])
            return [x, y]
        return [_tx(c) for c in coords]

    return {"type": geometry["type"], "coordinates": _tx(geometry["coordinates"])}, epsg

# ═══════════════════════════════════════════════════════════════════════════════
# 6. BUSCA DE MÉTRICAS  →  retorna TODAS as cenas válidas do período
# ═══════════════════════════════════════════════════════════════════════════════

def _stat(item, output_id):
    return item.get("outputs", {}).get(output_id, {}).get("bands", {}).get("B0", {}).get("stats")

def _sanity(scene: dict):
    for k in ("ndvi", "ndwi", "ndmi"):
        v = scene.get(k)
        if v is not None and not (-1.0 <= v <= 1.0):
            log.warning("%s fora do intervalo físico: %.4f", k.upper(), v)

def fetch_all_scenes(cred: sh.Credencial) -> list[dict]:
    """
    Retorna lista de todas as cenas válidas dos últimos LOOKBACK_DAYS dias,
    ordenadas por data. Cada item tem: date, ndvi, ndwi, ndmi, cloud_pct,
    valid_fraction. A melhor cena (menor cloud_pct) tem flag best=True.
    """
    today = dt.date.today()
    start = today - dt.timedelta(days=LOOKBACK_DAYS)
    roi_wgs84 = ROI_GEOJSON["features"][0]["geometry"]
    roi_utm, epsg = reproject_to_utm(roi_wgs84)

    log.info("Consultando API Estatística (%s → %s)...", start, today)
    payload = {
        "input": {
            "bounds": {
                "geometry": roi_utm,
                "properties": {"crs": f"http://www.opengis.net/def/crs/EPSG/0/{epsg}"},
            },
            "data": [{"type": "sentinel-2-l2a"}],
        },
        "aggregation": {
            "timeRange": {
                "from": f"{start.isoformat()}T00:00:00Z",
                "to":   f"{today.isoformat()}T23:59:59Z",
            },
            "aggregationInterval": {"of": "P1D"},
            "resx": 10, "resy": 10,
            "evalscript": EVALSCRIPT_INDICES,
        },
        "calculations": {
            "ndvi": {"statistics": {"default": {}}},
            "ndwi": {"statistics": {"default": {}}},
            "ndmi": {"statistics": {"default": {}}},
        },
    }
    headers = {**cred.cabecalho(), "Content-Type": "application/json"}
    resp = _http_post(SH_STATS_URL, headers=headers, json=payload, timeout=90)
    if not resp.ok:
        raise RuntimeError(f"API Estatística: {resp.status_code} {resp.text}")

    scenes = []
    for item in resp.json().get("data", []):
        ns = _stat(item, "ndvi")
        ws = _stat(item, "ndwi")
        ms = _stat(item, "ndmi")
        if not ns or not ws or not ms:
            continue
        sc = ns.get("sampleCount", 0)
        if sc <= 0:
            continue
        valid_px = sc - ns.get("noDataCount", 0)
        vf = valid_px / max(GEOMETRY_PIXEL_COUNT, 1)
        if vf < MIN_VALID_FRACTION:
            continue
        date_str  = item["interval"]["from"][:10]
        cloud_pct = max(0.0, min(100.0, (1.0 - vf) * 100.0))
        scene = {
            "date":           date_str,
            "ndvi":           ns.get("mean"),
            "ndwi":           ws.get("mean"),
            "ndmi":           ms.get("mean"),
            "cloud_pct":      round(cloud_pct, 1),
            "valid_fraction": round(vf, 4),
            "best":           False,
        }
        _sanity(scene)
        scenes.append(scene)
        log.debug("Cena %s  NDVI=%.3f  nuvens=%.1f%%  fração=%.2f",
                  date_str, scene["ndvi"], cloud_pct, vf)

    if not scenes:
        log.warning("Nenhuma cena válida nos últimos %d dias.", LOOKBACK_DAYS)
        return []

    scenes.sort(key=lambda x: x["date"])
    # marca a melhor cena (menor cloud_pct; em empate, a mais recente)
    best = min(scenes, key=lambda x: (x["cloud_pct"], -scenes.index(x)))
    best["best"] = True
    log.info("Melhor cena: %s (NDVI=%.3f, nuvens=%.1f%%)",
             best["date"], best["ndvi"], best["cloud_pct"])
    log.info("Total de cenas válidas no período: %d", len(scenes))
    return scenes

# ═══════════════════════════════════════════════════════════════════════════════
# 7. PAINEL DE IMAGEM
# ═══════════════════════════════════════════════════════════════════════════════

def _bbox(geometry_wgs84: dict, pad: float = 0.05) -> list[float]:
    pts   = list(_iter_coords(geometry_wgs84["coordinates"]))
    lons  = [p[0] for p in pts]; lats = [p[1] for p in pts]
    dlon  = (max(lons) - min(lons)) * pad; dlat = (max(lats) - min(lats)) * pad
    return [min(lons)-dlon, min(lats)-dlat, max(lons)+dlon, max(lats)+dlat]

def _fetch_png(cred: sh.Credencial, evalscript: str, bbox: list, date_str: str,
               w: int, h: int) -> bytes:
    payload = {
        "input": {
            "bounds": {
                "bbox": bbox,
                "properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/4326"},
            },
            "data": [{
                "type": "sentinel-2-l2a",
                "dataFilter": {
                    "timeRange": {
                        "from": f"{date_str}T00:00:00Z",
                        "to":   f"{date_str}T23:59:59Z",
                    },
                    "mosaickingOrder": "leastCC",
                },
            }],
        },
        "output": {
            "width": w, "height": h,
            "responses": [{"identifier": "default", "format": {"type": "image/png"}}],
        },
        "evalscript": evalscript,
    }
    headers = {**cred.cabecalho(), "Content-Type": "application/json"}
    resp = _http_post(SH_PROCESS_URL, headers=headers, json=payload, timeout=60)
    if not resp.ok:
        raise RuntimeError(f"API de Processo: {resp.status_code} {resp.content[:300]}")
    return resp.content

def _load_font(size: int = 14):
    from PIL import ImageFont
    for path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/ttf-dejavu/DejaVuSans.ttf",
    ]:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()

def build_panel(cred: sh.Credencial, date_str: str, roi_wgs84: dict) -> Optional[bytes]:
    """
    Gera PNG composto (cor real | NDVI | NDWI) para uma data.
    Retorna bytes ou None se falhar (etapa não-bloqueante).
    """
    from PIL import Image, ImageDraw
    try:
        bb   = _bbox(roi_wgs84)
        dlon = bb[2] - bb[0]; dlat = bb[3] - bb[1]
        if dlon >= dlat:
            w = PANEL_IMAGE_SIZE; h = max(64, int(PANEL_IMAGE_SIZE * dlat / dlon))
        else:
            h = PANEL_IMAGE_SIZE; w = max(64, int(PANEL_IMAGE_SIZE * dlon / dlat))

        specs = [
            ("Cor real", EVALSCRIPT_TRUECOLOR),
            ("NDVI",     EVALSCRIPT_NDVI_VIZ),
            ("NDWI",     EVALSCRIPT_NDWI_VIZ),
        ]
        tiles = []
        for label, script in specs:
            png = _fetch_png(cred, script, bb, date_str, w, h)
            tiles.append((label, Image.open(io.BytesIO(png)).convert("RGB")))

        font_lg = _load_font(14)
        font_sm = _load_font(11)
        label_h = 26; gap = 6
        panel   = Image.new("RGB", (w * 3 + gap * 2, h + label_h), (245, 245, 245))
        draw    = ImageDraw.Draw(panel)
        x = 0
        for label, tile in tiles:
            panel.paste(tile, (x, label_h))
            draw.rectangle([x, 0, x + w, label_h - 1], fill=(50, 50, 50))
            draw.text((x + 6, 5),          label,    fill="white",          font=font_lg)
            draw.text((x + 6, label_h-13), date_str, fill=(200, 200, 200),  font=font_sm)
            x += w + gap

        buf = io.BytesIO()
        panel.save(buf, format="PNG", optimize=True)
        log.info("Painel gerado para %s (%d bytes).", date_str, buf.tell())
        return buf.getvalue()
    except Exception as exc:
        log.warning("Não foi possível gerar painel para %s: %s", date_str, exc)
        return None

# ═══════════════════════════════════════════════════════════════════════════════
# 8. DETECÇÃO DE ANOMALIA
# ═══════════════════════════════════════════════════════════════════════════════

def check_anomaly(date_str: str, ndvi: float) -> Tuple[float, bool]:
    month = int(date_str.split("-")[1])
    c     = MONTHLY_CLIMATOLOGY[month]
    z     = (ndvi - c["mean"]) / c["sd"]
    log.info("Z-score para %s (mês %02d): %.3f  (NDVI=%.3f μ=%.3f σ=%.3f)",
             date_str, month, z, ndvi, c["mean"], c["sd"])
    return z, z >= ALERT_Z_THRESHOLD

def _status(z: float) -> str:
    # Apenas a direção POSITIVA importa (macrófitas elevam o NDVI)
    if z >= ALERT_Z_THRESHOLD: return "ALERTA"
    if z >= ATTENTION_Z:       return "ATENÇÃO"
    return "NORMAL"

# Alias mantido para compatibilidade: backfill_historico.py importava este
# nome antes da renomeação. Mantido para não quebrar scripts externos.
_status_from_z = _status

_CONFIDENCE_RANK = {"BAIXA": 0, "MEDIA": 1, "ALTA": 2}

def _confidence(valid_fraction: float) -> str:
    """Rotula a observação pela fração da AOI efetivamente observada.

    ALTA  -- cena praticamente limpa; comparável à série de referência.
    MEDIA -- cobertura parcial; usar com ressalva.
    BAIXA -- média calculada sobre pequena parte do reservatório; indicativa
             apenas. Nuvem residual não capturada pela máscara SCL tende a
             elevar o NDVI, então z-scores altos nesta faixa são suspeitos.
    """
    vf = float(valid_fraction or 0.0)
    if vf >= RELIABLE_VALID_FRACTION: return "ALTA"
    if vf >= USABLE_VALID_FRACTION:   return "MEDIA"
    return "BAIXA"

def _should_notify(confidence: str) -> bool:
    return _CONFIDENCE_RANK.get(confidence, 0) >= _CONFIDENCE_RANK.get(
        NOTIFY_MIN_CONFIDENCE, 0
    )

def recalcular_confirmacao(by_date: dict) -> None:
    """Preenche `alerta_confirmado` em toda a série, in place.

    Percorre em ordem de data: confirma quando a observação qualificada e a
    qualificada imediatamente anterior ultrapassam o limiar. Observações de
    baixa confiança não confirmam nem quebram a sequência. Usado pelo monitor
    e pelo backfill, que assim seguem exatamente o mesmo critério; aplicado à
    série inteira, uma cena que entrou nesta execução também conta como
    anterior.
    """
    prev_qualified_z = None
    for date_key in sorted(by_date):
        row = by_date[date_key]
        try:
            zz = float(row.get("zscore") or 0.0)
            conf = row.get("confianca") or _confidence(
                float(row.get("valid_fraction") or 0.0))
        except (TypeError, ValueError):
            row["alerta_confirmado"] = ""
            continue
        if not _should_notify(conf):
            row["alerta_confirmado"] = "NAO"
            continue
        confirmado = (
            zz >= ALERT_Z_THRESHOLD
            and (not ALERT_REQUIRE_PERSISTENCE
                 or (prev_qualified_z is not None
                     and prev_qualified_z >= ALERT_Z_THRESHOLD))
        )
        row["alerta_confirmado"] = "SIM" if confirmado else "NAO"
        prev_qualified_z = zz

# ═══════════════════════════════════════════════════════════════════════════════
# 9. PERSISTÊNCIA
# ═══════════════════════════════════════════════════════════════════════════════

PROJECT_ROOT     = Path(__file__).parent
DATA_DIR         = PROJECT_ROOT / "docs" / "data"
LATEST_IMG_DIR   = PROJECT_ROOT / "docs" / "images" / "latest"
ARCHIVE_IMG_DIR  = PROJECT_ROOT / "docs" / "images" / "archive"
HISTORY_CSV      = DATA_DIR / "historico.csv"
LATEST_JSON      = DATA_DIR / "ultimo.json"
SCENES_30D_JSON  = DATA_DIR / "passagens_30d.json"
IMAGENS_JSON     = DATA_DIR / "imagens.json"   # índice do arquivo de painéis

CSV_FIELDS = ["date", "ndvi", "ndwi", "ndmi", "cloud_pct",
              "valid_fraction", "zscore", "status", "confianca",
              "alerta_confirmado"]

def save_panel_bytes(panel_bytes: Optional[bytes], date_str: str,
                     is_best: bool = False) -> None:
    if not panel_bytes:
        return
    archive = ARCHIVE_IMG_DIR / date_str
    archive.mkdir(parents=True, exist_ok=True)
    (archive / "panel.png").write_bytes(panel_bytes)
    if is_best:
        LATEST_IMG_DIR.mkdir(parents=True, exist_ok=True)
        (LATEST_IMG_DIR / "panel.png").write_bytes(panel_bytes)
        log.info("Painel da melhor cena copiado para latest/.")

def save_all(scenes: list[dict], best: dict, z: float, run_id: str) -> dict:
    """
    Persiste todos os arquivos de dados e imagens.
    Retorna o payload do ultimo.json.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_IMG_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_IMG_DIR.mkdir(parents=True, exist_ok=True)

    status     = _status(z)
    confianca  = _confidence(best["valid_fraction"])

    # ── historico.csv (série acumulada — upsert por data) ───────────────────
    # Todas as passagens do período entram, não só a melhor: é o mesmo que o
    # backfill grava, e sem isso a série ficaria mais esparsa daqui em diante
    # do que no passado. Reprocessar a mesma data apenas a sobrescreve.
    existing: dict[str, dict] = {}
    if HISTORY_CSV.exists():
        with open(HISTORY_CSV, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                existing[row["date"]] = row
    for sc in scenes:
        sz, _ = check_anomaly(sc["date"], sc["ndvi"])
        existing[sc["date"]] = {
            "date": sc["date"], "ndvi": sc["ndvi"], "ndwi": sc["ndwi"],
            "ndmi": sc["ndmi"], "cloud_pct": sc["cloud_pct"],
            "valid_fraction": sc["valid_fraction"], "zscore": round(sz, 4),
            "status": _status(sz),
            "confianca": _confidence(sc["valid_fraction"]),
        }
    recalcular_confirmacao(existing)
    persistente = existing[best["date"]]["alerta_confirmado"] == "SIM"
    with open(HISTORY_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for key in sorted(existing):
            w.writerow(existing[key])
    log.info("historico.csv atualizado (%d registros).", len(existing))

    # ── ultimo.json (estado mais recente — melhor cena) ─────────────────────
    payload = {
        "schema_version": SCHEMA_VERSION,
        "run_id":         run_id,
        "updated_at":     dt.datetime.now(dt.timezone.utc).isoformat(),
        "date":           best["date"],
        "ndvi":           best["ndvi"],
        "ndwi":           best["ndwi"],
        "ndmi":           best["ndmi"],
        "cloud_pct":      best["cloud_pct"],
        "valid_fraction": best["valid_fraction"],
        "zscore":         round(z, 4),
        "status":         status,
        "confianca":      confianca,
        "confianca_limiares": {
            "alta":  RELIABLE_VALID_FRACTION,
            "media": USABLE_VALID_FRACTION,
        },
        "confianca_nota": (
            "Rótulo derivado da fração da área do reservatório efetivamente "
            "observada. Em cenas de confiança BAIXA a média é calculada sobre "
            "uma pequena parte da represa e nuvem residual tende a elevar o "
            "NDVI: o valor é indicativo, não comparável à série de referência."
        ),
        "alerta_confirmado": "SIM" if persistente else "NAO",
        "persistencia_exigida": ALERT_REQUIRE_PERSISTENCE,
        "persistencia_nota": (
            "Alerta só é confirmado quando duas observações qualificadas "
            "consecutivas ultrapassam o limiar. Pico isolado de NDVI é "
            "sinal típico de nuvem residual, não de expansão de vegetação."
        ),
        "notificado": _should_notify(confianca),
        "cloud_threshold": CLOUD_THRESHOLD,
        "cloud_note": (
            "Cobertura estimada pela máscara SCL na área de estudo; "
            "10% é apenas referência e não bloqueia o envio."
        ),
    }
    LATEST_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info("ultimo.json atualizado (status=%s, run_id=%s).", status, run_id)

    # ── passagens_30d.json (todas as cenas do período + z-score) ────────────
    # Enriquece cada cena com z-score e status antes de salvar
    enriched = []
    for sc in scenes:
        sz, _ = check_anomaly(sc["date"], sc["ndvi"])
        enriched.append({
            **sc,
            "zscore": round(sz, 4),
            "status": _status(sz),
            "confianca": _confidence(sc.get("valid_fraction", 0.0)),
            # informa ao painel se a imagem de arquivo já existe
            "has_image": (ARCHIVE_IMG_DIR / sc["date"] / "panel.png").exists(),
        })
    scenes_payload = {
        "schema_version": SCHEMA_VERSION,
        "run_id":         run_id,
        "updated_at":     payload["updated_at"],
        "lookback_days":  LOOKBACK_DAYS,
        "scenes":         enriched,
    }
    SCENES_30D_JSON.write_text(
        json.dumps(scenes_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info("passagens_30d.json atualizado (%d cenas).", len(enriched))

    # ── imagens.json (índice completo do arquivo de painéis) ────────────────
    # passagens_30d.json cobre só a janela recente, mas o arquivo de imagens
    # cresce indefinidamente. Sem este índice, o painel web não tem como
    # saber quais datas possuem imagem e oferece apenas as últimas semanas.
    datas_com_painel = sorted(
        p.parent.name for p in ARCHIVE_IMG_DIR.glob("*/panel.png")
    )
    IMAGENS_JSON.write_text(
        json.dumps({
            "schema_version": "1.0",
            "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "datas": datas_com_painel,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info("imagens.json atualizado (%d painéis arquivados).",
             len(datas_com_painel))

    return payload

# ═══════════════════════════════════════════════════════════════════════════════
# 10. NOTIFICAÇÕES
# ═══════════════════════════════════════════════════════════════════════════════

# O envio em si fica em src/notificacao.py. Os nomes abaixo são mantidos
# para não quebrar quem já os importava.
send_telegram = notificacao.telegram
send_whatsapp = notificacao.whatsapp
send_email    = notificacao.email


# ── Resumo do módulo de qualidade da água ────────────────────────────────────
# O módulo de água roda antes, no mesmo workflow, e grava a última observação
# em represa_ultimo.json. Ler esse arquivo é o que põe os dois módulos num
# único aviso, sem que um importe a lógica do outro. Se o módulo de água
# falhou nesta execução, o arquivo é o da execução anterior: a data exibida
# deixa isso visível.

AGUA_JSON   = Path(__file__).parent / "docs" / "data" / "represa_ultimo.json"
CONFIG_YAML = Path(__file__).parent / "config.yaml"

def _num(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def resumo_agua() -> Tuple[str, bool]:
    """Texto para a notificação e se o nível exige aviso por si só
    (VIGILANCIA ou CONFIRMADO)."""
    try:
        obs = json.loads(AGUA_JSON.read_text(encoding="utf-8"))["observacao"]
    except (OSError, ValueError, KeyError) as exc:
        log.warning("Resumo da qualidade da água indisponível: %s", exc)
        return "", False
    try:
        alerta_cfg = yaml.safe_load(CONFIG_YAML.read_text(encoding="utf-8"))["alerta"]
    except (OSError, ValueError, KeyError, TypeError):
        alerta_cfg = {}

    nivel      = obs.get("nivel") or "NORMAL"
    confirmado = obs.get("nivel_confirmado") == "SIM"
    emoji  = {"CONFIRMADO": "🚨", "VIGILANCIA": "👀"}.get(nivel, "✅")
    rotulo = {"CONFIRMADO": "ALERTA CONFIRMADO" if confirmado
                            else "ALERTA (aguardando 2ª passagem)",
              "VIGILANCIA": "VIGILÂNCIA"}.get(nivel, "NORMAL")

    linhas = [f"💧 Qualidade da água — {obs.get('data', '?')}",
              f"{emoji} Nível: {rotulo}  (confiança {obs.get('confianca', '?')})"]
    agua = _num(obs.get("area_agua_ha"))
    cob  = _num(obs.get("area_cobertura_flutuante_ha"))
    if obs.get("area_confiavel") == "SIM" and agua is not None and cob is not None:
        linhas.append(f"📐 Lâmina d'água {agua:.1f} ha | cobertura flutuante {cob:.1f} ha")
    if nivel != "NORMAL" and obs.get("indices_concordantes"):
        indices = obs["indices_concordantes"].replace(";", ", ").upper()
        linhas.append(f"📈 Índices acima do padrão: {indices}")

    chave = {"CONFIRMADO": "confirmado", "VIGILANCIA": "vigilancia"}.get(nivel)
    mensagem = ((alerta_cfg.get(chave) or {}).get("mensagem") or "").strip() if chave else ""
    if mensagem:
        linhas.append(f"ℹ️ {mensagem}")
    return "\n".join(linhas) + "\n", nivel in ("CONFIRMADO", "VIGILANCIA")


# ── Resumo do radar (Sentinel-1) ─────────────────────────────────────────────
# Mesmo arranjo do bloco da água: src.radar roda antes no workflow e grava
# radar_ultimo.json. O radar atravessa nuvens, então costuma ter passagem
# mais recente que a melhor cena óptica.

RADAR_JSON = Path(__file__).parent / "docs" / "data" / "radar_ultimo.json"

def resumo_radar() -> Tuple[str, bool]:
    """Texto para a notificação e se o radar exige aviso por si só (ALERTA)."""
    try:
        obs = json.loads(RADAR_JSON.read_text(encoding="utf-8"))["observacao"]
    except (OSError, ValueError, KeyError) as exc:
        log.warning("Resumo do radar indisponível: %s", exc)
        return "", False
    st_radar   = obs.get("frac_vegetacao_status") or "SEM_REFERENCIA"
    confirmado = obs.get("frac_vegetacao_confirmado") == "SIM"
    emoji  = {"ALERTA": "🚨", "ATENCAO": "⚠️"}.get(st_radar, "✅")
    rotulo = {"ALERTA": "ALERTA CONFIRMADO" if confirmado
                        else "ALERTA (aguardando 2ª passagem)",
              "ATENCAO": "ATENÇÃO"}.get(st_radar, st_radar.replace("_", " "))
    area, z = _num(obs.get("area_vegetacao_ha")), _num(obs.get("frac_vegetacao_z"))
    linhas = [f"📡 Radar (atravessa nuvens) — {obs.get('data', '?')}",
              f"{emoji} Status: {rotulo}"]
    if area is not None:
        linhas.append(f"🌿 Assinatura de vegetação: {area:.1f} ha"
                      + (f" (z = {z:.1f})" if z is not None else ""))
    if st_radar == "ALERTA":
        linhas.append("ℹ️ Retroespalhamento típico de vegetação flutuante muito acima "
                      "do padrão do mês. Recomenda-se verificação de campo.")
    return "\n".join(linhas) + "\n", st_radar == "ALERTA"

# ═══════════════════════════════════════════════════════════════════════════════
# 11. SMOKE TEST
# ═══════════════════════════════════════════════════════════════════════════════

def smoke_test():
    """Falha rápido com mensagem clara se secrets obrigatórios estiverem ausentes."""
    missing = [k for k in ("SH_CLIENT_ID", "SH_CLIENT_SECRET")
               if not os.environ.get(k)]
    if missing:
        raise EnvironmentError(
            f"Secrets obrigatórios não configurados: {', '.join(missing)}. "
            "Configure em Settings > Secrets and variables > Actions no GitHub."
        )
    active = notificacao.canais_ativos()
    if notificacao.suprimido():
        log.info("Smoke test: NOTIFICAR=0, canais não são exigidos.")
        return
    if not active:
        raise EnvironmentError(
            "Nenhum canal de notificação configurado. "
            "Configure pelo menos Telegram, e-mail ou WhatsApp nos secrets."
        )
    log.info("Smoke test OK. Canais ativos: %s", ", ".join(active))

# ═══════════════════════════════════════════════════════════════════════════════
# 12. MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    run_id = str(uuid.uuid4())
    log.info("=== Iniciando execução  run_id=%s ===", run_id)

    smoke_test()

    cred   = credencial()
    scenes = fetch_all_scenes(cred)
    agua_txt, agua_relevante = resumo_agua()
    radar_txt, radar_relevante = resumo_radar()

    if not scenes:
        msg = (
            f"[São Lourenço] Nenhuma imagem Sentinel-2 com pixels válidos "
            f"foi encontrada nos últimos {LOOKBACK_DAYS} dias.\n"
        )
        for bloco in (radar_txt, agua_txt):
            if bloco:
                msg += "\n" + bloco
        log.warning(msg)
        notificacao.todos("Monitoramento São Lourenço – sem imagem", msg)
        return

    best        = next(s for s in scenes if s["best"])
    z, is_alert = check_anomaly(best["date"], best["ndvi"])
    roi_wgs84   = ROI_GEOJSON["features"][0]["geometry"]

    # Gera painel para a melhor cena (sempre)
    best_panel = build_panel(cred, best["date"], roi_wgs84)
    save_panel_bytes(best_panel, best["date"], is_best=True)

    # Gera painéis para cenas adicionais ainda sem imagem no arquivo
    for sc in scenes:
        if sc["best"]:
            continue
        archive_path = ARCHIVE_IMG_DIR / sc["date"] / "panel.png"
        if not archive_path.exists():
            log.info("Gerando imagem de arquivo para %s...", sc["date"])
            panel = build_panel(cred, sc["date"], roi_wgs84)
            save_panel_bytes(panel, sc["date"], is_best=False)

    # Persiste todos os dados (atualiza has_image depois de gerar imagens)
    payload    = save_all(scenes, best, z, run_id)
    status     = payload["status"]
    confianca  = payload["confianca"]
    confirmado = payload["alerta_confirmado"] == "SIM"

    emoji = {"ALERTA": "🚨", "ATENÇÃO": "⚠️", "NORMAL": "✅"}.get(status, "ℹ️")
    selo  = {"ALTA": "🟢", "MEDIA": "🟡", "BAIXA": "🔴"}.get(confianca, "")
    n_cenas = len(scenes)
    message = (
        f"🛰️ Monitoramento São Lourenço\n"
        f"📅 Melhor cena: {best['date']}\n"
        f"🌿 NDVI:    {best['ndvi']:.3f}\n"
        f"💧 NDWI:    {best['ndwi']:.3f}\n"
        f"🌱 NDMI:    {best['ndmi']:.3f}\n"
        f"☁️ Nuvens:  {best['cloud_pct']:.1f}%\n"
        f"👁️ Área observada: {best['valid_fraction'] * 100:.1f}%\n"
        f"📊 Z-score: {z:.2f}\n"
        f"{emoji} Status: {status}\n"
        f"{selo} Confiança: {confianca}\n"
        f"🔍 Passagens no período: {n_cenas} "
        f"(veja todas em {PANEL_URL})\n"
    )
    if confianca != "ALTA":
        message += (
            f"⚠️ Leitura obtida sobre {best['valid_fraction'] * 100:.0f}% da represa. "
            f"Nuvem residual tende a elevar o NDVI — valor indicativo.\n"
        )
    if is_alert and confirmado:
        message += (
            "⚠️ Anomalia CONFIRMADA em duas passagens consecutivas — "
            "recomenda-se verificação de campo.\n"
        )
    elif is_alert:
        message += (
            "🔎 Anomalia detectada nesta passagem, ainda NÃO confirmada: "
            "a observação qualificada anterior estava dentro do normal. "
            "Aguardando a próxima passagem para confirmar ou descartar.\n"
        )

    for bloco in (radar_txt, agua_txt):
        if bloco:
            message += "\n" + bloco

    log.info("Mensagem composta:\n%s", message)

    # Observações de baixa confiança continuam gravadas e visíveis no painel,
    # mas não disparam push: alerta falso recorrente faz o destinatário parar
    # de olhar os alertas verdadeiros. Vigilância ou alerta da qualidade da
    # água avisa mesmo assim: esses níveis já exigem cena de confiança ALTA.
    # Alerta do radar também: ele não depende de céu limpo, e é justamente
    # com a represa encoberta que o óptico ficaria calado.
    if _should_notify(confianca) or agua_relevante or radar_relevante:
        log.info("Alerta confirmado por persistência: %s", confirmado)
        assunto = f"Monitoramento São Lourenço – {status}"
        if radar_relevante:
            assunto += " | radar: ALERTA"
        if agua_relevante:
            assunto += " | água: atenção"
        notificacao.todos(assunto, message, best_panel)
    else:
        log.info(
            "Notificação suprimida: confiança %s abaixo do corte %s "
            "(área observada %.1f%%). Dados gravados normalmente.",
            confianca, NOTIFY_MIN_CONFIDENCE, best["valid_fraction"] * 100,
        )

    log.info("=== Execução concluída  run_id=%s  status=%s  confiança=%s  "
             "confirmado=%s ===", run_id, status, confianca, confirmado)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log.exception("Falha crítica na execução do monitor: %s", exc)
        try:
            if not notificacao.suprimido():
                send_telegram(f"[ERRO CRÍTICO] Monitor São Lourenço falhou: {exc}")
        except Exception:
            pass
        sys.exit(1)

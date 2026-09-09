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
6.  Envia notificações via Telegram, e-mail e/ou WhatsApp (CallMeBot).
7.  Todas as chamadas HTTP têm retry com backoff exponencial.
"""

import csv
import datetime as dt
import io
import json
import logging
import os
import smtplib
import sys
import time
import uuid
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from pyproj import Transformer

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
# 4. AUTENTICAÇÃO  (token único por execução)
# ═══════════════════════════════════════════════════════════════════════════════

def get_access_token() -> str:
    log.info("Obtendo token OAuth2 do Copernicus CDSE...")
    resp = _http_post(
        SH_TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id":     os.environ["SH_CLIENT_ID"],
            "client_secret": os.environ["SH_CLIENT_SECRET"],
        },
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f"Token endpoint: {resp.status_code} {resp.text}")
    log.info("Token obtido com sucesso.")
    return resp.json()["access_token"]

# ═══════════════════════════════════════════════════════════════════════════════
# 5. GEOMETRIA
# ═══════════════════════════════════════════════════════════════════════════════

def _iter_coords(coords):
    if isinstance(coords[0], (int, float)):
        yield coords
    else:
        for item in coords:
            yield from _iter_coords(item)

def reproject_to_utm(geometry: dict) -> tuple[dict, int]:
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

def fetch_all_scenes(token: str) -> list[dict]:
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
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
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

def _fetch_png(token: str, evalscript: str, bbox: list, date_str: str,
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
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
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

def build_panel(token: str, date_str: str, roi_wgs84: dict) -> bytes | None:
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
            png = _fetch_png(token, script, bb, date_str, w, h)
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

def check_anomaly(date_str: str, ndvi: float) -> tuple[float, bool]:
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

CSV_FIELDS = ["date", "ndvi", "ndwi", "ndmi", "cloud_pct",
              "valid_fraction", "zscore", "status"]

def save_panel_bytes(panel_bytes: bytes | None, date_str: str,
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

    status = _status(z)

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

    # ── historico.csv (série acumulada — upsert por data) ───────────────────
    existing: dict[str, dict] = {}
    if HISTORY_CSV.exists():
        with open(HISTORY_CSV, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                existing[row["date"]] = row
    existing[best["date"]] = {k: payload.get(k, "") for k in CSV_FIELDS}
    with open(HISTORY_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for key in sorted(existing):
            w.writerow(existing[key])
    log.info("historico.csv atualizado (%d registros).", len(existing))

    # ── passagens_30d.json (todas as cenas do período + z-score) ────────────
    # Enriquece cada cena com z-score e status antes de salvar
    enriched = []
    for sc in scenes:
        sz, _ = check_anomaly(sc["date"], sc["ndvi"])
        enriched.append({
            **sc,
            "zscore": round(sz, 4),
            "status": _status(sz),
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

    return payload

# ═══════════════════════════════════════════════════════════════════════════════
# 10. NOTIFICAÇÕES
# ═══════════════════════════════════════════════════════════════════════════════

# ── Telegram ─────────────────────────────────────────────────────────────────

def send_telegram(message: str, image_bytes: bytes | None = None):
    token    = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_raw = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_raw:
        log.info("Telegram não configurado; pulando.")
        return
    for chat_id in [c.strip() for c in chat_raw.split(",") if c.strip()]:
        try:
            if image_bytes:
                caption = message[:1021] + "..." if len(message) > 1024 else message
                resp = _http_post(
                    f"https://api.telegram.org/bot{token}/sendPhoto",
                    data={"chat_id": chat_id, "caption": caption},
                    files={"photo": ("panel.png", image_bytes, "image/png")},
                    timeout=60,
                )
            else:
                resp = _http_post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    data={"chat_id": chat_id, "text": message},
                    timeout=30,
                )
            if resp.ok:
                log.info("Telegram enviado para %s.", chat_id)
            else:
                log.error("Telegram falhou para %s: %s", chat_id, resp.text)
        except Exception as exc:
            log.error("Erro Telegram %s: %s", chat_id, exc)

# ── WhatsApp via CallMeBot (gratuito, sem servidor) ──────────────────────────
# Siga https://www.callmebot.com/blog/free-api-whatsapp-messages/ para
# obter sua APIKEY. Defina os secrets WHATSAPP_PHONE e WHATSAPP_APIKEY.
# Apenas texto — imagem chega pelo Telegram/e-mail.

def send_whatsapp(message: str):
    phone  = os.environ.get("WHATSAPP_PHONE")
    apikey = os.environ.get("WHATSAPP_APIKEY")
    if not phone or not apikey:
        log.info("WhatsApp não configurado; pulando.")
        return
    try:
        resp = requests.get(
            "https://api.callmebot.com/whatsapp.php",
            params={"phone": phone, "text": message, "apikey": apikey},
            timeout=30,
        )
        if resp.ok:
            log.info("WhatsApp enviado para %s.", phone)
        else:
            log.error("WhatsApp falhou: %d %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        log.error("Erro WhatsApp: %s", exc)

# ── E-mail (Gmail / Outlook / SMTP genérico) ─────────────────────────────────
# Gmail:   EMAIL_SMTP_HOST=smtp.gmail.com  EMAIL_SMTP_PORT=465   (SSL)
# Outlook: EMAIL_SMTP_HOST=smtp.office365.com  EMAIL_SMTP_PORT=587  (STARTTLS)
# Se EMAIL_SMTP_HOST não estiver definido, usa Gmail como padrão.

def send_email(subject: str, message: str, image_bytes: bytes | None = None):
    addr     = os.environ.get("EMAIL_ADDRESS")
    password = os.environ.get("EMAIL_APP_PASSWORD")
    to_raw   = os.environ.get("ALERT_EMAIL_TO")
    if not addr or not password or not to_raw:
        log.info("E-mail não configurado; pulando.")
        return
    smtp_host = os.environ.get("EMAIL_SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("EMAIL_SMTP_PORT", "465"))
    to_addrs  = [a.strip() for a in to_raw.split(",") if a.strip()]

    if image_bytes:
        msg = MIMEMultipart()
        msg["Subject"] = subject
        msg["From"]    = addr
        msg["To"]      = ", ".join(to_addrs)
        msg.attach(MIMEText(message))
        img = MIMEImage(image_bytes, _subtype="png")
        img.add_header("Content-Disposition", "attachment",
                       filename="reservatorio_panel.png")
        msg.attach(img)
    else:
        msg = MIMEText(message)
        msg["Subject"] = subject
        msg["From"]    = addr
        msg["To"]      = ", ".join(to_addrs)

    try:
        if smtp_port == 587:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as srv:
                srv.starttls(); srv.login(addr, password)
                srv.sendmail(addr, to_addrs, msg.as_string())
        else:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30) as srv:
                srv.login(addr, password)
                srv.sendmail(addr, to_addrs, msg.as_string())
        log.info("E-mail enviado para: %s", ", ".join(to_addrs))
    except Exception as exc:
        log.error("Erro e-mail: %s", exc)

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
    has_notif = any([
        os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"),
        os.environ.get("EMAIL_ADDRESS") and os.environ.get("EMAIL_APP_PASSWORD")
            and os.environ.get("ALERT_EMAIL_TO"),
        os.environ.get("WHATSAPP_PHONE") and os.environ.get("WHATSAPP_APIKEY"),
    ])
    if not has_notif:
        raise EnvironmentError(
            "Nenhum canal de notificação configurado. "
            "Configure pelo menos Telegram, e-mail ou WhatsApp nos secrets."
        )
    active = [
        name for name, cond in [
            ("Telegram",  os.environ.get("TELEGRAM_BOT_TOKEN")),
            ("E-mail",    os.environ.get("EMAIL_ADDRESS")),
            ("WhatsApp",  os.environ.get("WHATSAPP_PHONE")),
        ] if cond
    ]
    log.info("Smoke test OK. Canais ativos: %s", ", ".join(active))

# ═══════════════════════════════════════════════════════════════════════════════
# 12. MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    run_id = str(uuid.uuid4())
    log.info("=== Iniciando execução  run_id=%s ===", run_id)

    smoke_test()

    token  = get_access_token()
    scenes = fetch_all_scenes(token)

    if not scenes:
        msg = (
            f"[São Lourenço] Nenhuma imagem Sentinel-2 com pixels válidos "
            f"foi encontrada nos últimos {LOOKBACK_DAYS} dias."
        )
        log.warning(msg)
        send_telegram(msg); send_email("Monitoramento São Lourenço – sem imagem", msg)
        send_whatsapp(msg)
        return

    best        = next(s for s in scenes if s["best"])
    z, is_alert = check_anomaly(best["date"], best["ndvi"])
    roi_wgs84   = ROI_GEOJSON["features"][0]["geometry"]

    # Gera painel para a melhor cena (sempre)
    best_panel = build_panel(token, best["date"], roi_wgs84)
    save_panel_bytes(best_panel, best["date"], is_best=True)

    # Gera painéis para cenas adicionais ainda sem imagem no arquivo
    for sc in scenes:
        if sc["best"]:
            continue
        archive_path = ARCHIVE_IMG_DIR / sc["date"] / "panel.png"
        if not archive_path.exists():
            log.info("Gerando imagem de arquivo para %s...", sc["date"])
            panel = build_panel(token, sc["date"], roi_wgs84)
            save_panel_bytes(panel, sc["date"], is_best=False)

    # Persiste todos os dados (atualiza has_image depois de gerar imagens)
    payload = save_all(scenes, best, z, run_id)
    status  = payload["status"]

    emoji = {"ALERTA": "🚨", "ATENÇÃO": "⚠️", "NORMAL": "✅"}.get(status, "ℹ️")
    n_cenas = len(scenes)
    message = (
        f"🛰️ Monitoramento São Lourenço\n"
        f"📅 Melhor cena: {best['date']}\n"
        f"🌿 NDVI:    {best['ndvi']:.3f}\n"
        f"💧 NDWI:    {best['ndwi']:.3f}\n"
        f"🌱 NDMI:    {best['ndmi']:.3f}\n"
        f"☁️ Nuvens:  {best['cloud_pct']:.1f}%\n"
        f"📊 Z-score: {z:.2f}\n"
        f"{emoji} Status: {status}\n"
        f"🔍 Passagens no período: {n_cenas} "
        f"(veja todas em saolourenco.netlify.app)\n"
    )
    if best["cloud_pct"] > CLOUD_THRESHOLD:
        message += "☁️ Obs: cobertura > 10%; imagem mantida no histórico.\n"
    if is_alert:
        message += "⚠️ Anomalia estatística detectada — recomenda-se verificação de campo.\n"

    log.info("Mensagem composta:\n%s", message)
    send_telegram(message, image_bytes=best_panel)
    send_email(f"Monitoramento São Lourenço – {status}", message, image_bytes=best_panel)
    send_whatsapp(message)

    log.info("=== Execução concluída  run_id=%s  status=%s ===", run_id, status)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log.exception("Falha crítica na execução do monitor: %s", exc)
        try:
            send_telegram(f"[ERRO CRÍTICO] Monitor São Lourenço falhou: {exc}")
        except Exception:
            pass
        sys.exit(1)

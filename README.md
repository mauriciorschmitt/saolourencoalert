# Monitoramento Ambiental da Represa São Lourenço

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.22838354.svg)](https://doi.org/10.5281/zenodo.22838354)
[![Painel](https://img.shields.io/badge/painel-online-2e7d32)](https://mauriciorschmitt.github.io/saolourencoalert/)

Plataforma de monitoramento ambiental por sensoriamento remoto da Represa
São Lourenço, em Mafra (SC). Processa imagens Sentinel-2 duas vezes por
semana, sem intervenção manual, e roda inteiramente em infraestrutura
gratuita.

**Painel:** https://mauriciorschmitt.github.io/saolourencoalert/

A plataforma reúne dois módulos independentes sobre o mesmo reservatório:

| Módulo | O que faz | Série |
|---|---|---|
| **Macrófitas** | Acompanha cobertura de vegetação flutuante por NDVI, gera painéis de imagem por data e envia os avisos | desde jul/2016 |
| **Qualidade da água** | Sete índices espectrais, alerta em dois níveis e medição de área em hectares | desde jul/2016 |

Os dois rodam na mesma execução agendada, um depois do outro, e publicam
num único commit. Cada um mantém seu próprio arquivo de série; o aviso
enviado por Telegram e e-mail reúne os dois.

---

## Frequência de atualização

| Etapa | Quando |
|---|---|
| Execução agendada | segunda e quinta, 09:17 UTC (06:17 BRT) — o GitHub pode atrasar algumas horas |
| Janela consultada | últimos 30 dias a cada execução: nenhuma passagem se perde, e reprocessar a mesma data apenas a sobrescreve |
| Passagens do Sentinel-2 | a cada 2 a 5 dias; cenas totalmente encobertas são descartadas, então a série tem em média uma observação a cada 5 dias |
| Do satélite ao painel | até 3–4 dias, conforme o dia da passagem em relação à próxima execução |

Para atualizar fora da agenda: aba **Actions** → *Monitoramento São
Lourenço (agendado)* → **Run workflow**. A caixa *Enviar
Telegram/e-mail/WhatsApp* permite rodar sem disparar avisos.

---

## O que a plataforma entrega

**Área em hectares, não índice abstrato.** Em vez de apenas promediar
índices sobre o reservatório, o sistema classifica pixel a pixel e conta. A
saída não é "o NDVI subiu 0,08" — é "a lâmina d'água caiu de 66 para 14 ha e
a cobertura flutuante subiu de 3,6 para 45 ha". Foi assim que o evento de
2025 ficou registrado.

**Alerta em dois níveis**, porque antecipar e confirmar exigem evidências
diferentes e levam a ações diferentes.

**Qualidade declarada em cada leitura.** Toda observação carrega um rótulo
derivado da fração do reservatório efetivamente visível. Cenas encobertas
entram no histórico, mas não compõem a referência estatística, não disparam
notificação e não produzem estimativa de área.

**Validação de campo integrada.** Observações in loco entram por formulário
e alimentam uma tabela de concordância que se acumula ao longo do tempo.

**Aviso por Telegram e e-mail** a cada execução com cena aproveitável:
leitura das macrófitas, painel de imagem e nível de alerta da qualidade da
água. Vigilância ou alerta da água avisa mesmo quando a cena das macrófitas
é de baixa confiança.

---

## Alerta em dois níveis

### Vigilância — antecipação

Dispara quando a clorofila relativa se afasta do padrão histórico do mês
enquanto a cobertura ainda está dentro do normal.

O fundamento é empírico: em 12/02/2025 o NDCI atingiu z = 5,1 com a
cobertura em 10 ha. A área só cruzou o limiar em 18/04 — 65 dias depois — e
o pico veio em setembro.

**Ressalva, e ela importa.** Isso é *uma* ocorrência. Na mesma série de dez
anos a clorofila disparou outras cinco vezes sem que surto algum se
seguisse. O valor preditivo observado é de cerca de 1 em 6. É hipótese a
testar prospectivamente, não preditor validado — e por isso a saída do nível
é um convite a verificar, não uma afirmação de que algo ocorrerá.

Existe ainda uma explicação alternativa plausível: a banda red-edge responde
a clorofila de qualquer planta. O NDCI pode não estar antecipando um
processo distinto, e sim detectando as mesmas macrófitas mais cedo, por ser
mais sensível a pouca biomassa que o FAI. Para o uso prático dá no mesmo;
para a interpretação do mecanismo, não.

### Alerta confirmado — detecção

Exige que pelo menos três índices independentes estejam acima do padrão na
mesma passagem, em cena de alta confiança, em duas observações consecutivas.

A concordância é o que separa alteração real de artefato de uma banda
específica: o NDVI isolado produz 76 disparos na série; três índices
concordando produzem 14, dos quais 13 pertencem ao evento conhecido.

---

## Índices

| Índice | Bandas | O que acompanha | Alerta |
|---|---|---|---|
| NDVI | B08, B04 | Vegetação fotossintética sobre a água | sim |
| FAI | B08, B04, B11 | Algas e vegetação flutuante; robusto a aerossol | sim |
| NDCI | B05, B04 | Clorofila relativa — red-edge, eutrofização | sim |
| NDTI | B04, B03 | Turbidez relativa — sedimento em suspensão | sim |
| NDWI | B03, B08 | Delimitação da lâmina d'água | — |
| NDMI | B08, B11 | Umidade; separa vegetação de substrato exposto | — |
| CDOM (proxy) | B03, B04 | Matéria orgânica dissolvida — exploratório | — |

Explicação detalhada de cada um, com fórmula e interpretação, na página
[Indicadores e série completa](https://mauriciorschmitt.github.io/saolourencoalert/indicadores.html).

Todas as comparações são contra a média e o desvio **daquele mês** no
período 2016–2024, calculados apenas sobre cenas de alta confiança. Comparar
com o mesmo mês remove a sazonalidade: um valor alto em dezembro pode ser
normal para dezembro.

---

## Área de interesse

Polígono digitalizado manualmente sobre imagem de alta resolução, com 502
vértices e 79,49 ha.

Antes do processamento aplica-se um **recuo de 10 m** — exatamente um pixel.
Isso descarta o anel que mistura água e margem dentro do mesmo pixel, onde
vegetação ripária eleva os índices sem que nada tenha ocorrido na lâmina
d'água. A área efetiva cai para 65,10 ha, 7.334 pixels após rasterização.

Recuos maiores desconectam os braços estreitos: a partir de 15 m o polígono
se fragmenta em cinco partes.

---

## Validação de campo

O sistema compara a si mesmo há dez anos, o que é circular. Observações in
loco quebram esse círculo e são a lacuna mais importante do projeto.

O registro é feito pelo [formulário de campo](https://mauriciorschmitt.github.io/saolourencoalert/campo.html),
que captura a localização do aparelho e abre uma issue já preenchida; as
fotos são anexadas ali. Um fluxo converte a issue numa linha de
`data/campo.csv`. O protocolo completo está em
[PROTOCOLO_CAMPO.md](PROTOCOLO_CAMPO.md).

Registrar ausência de macrófitas vale tanto quanto confirmar um alerta: sem
observações de normalidade não há como estimar falso positivo.

A cada execução, `src/campo.py` associa cada observação à passagem mais
próxima e publica a concordância em `docs/data/validacao.json`. É essa
tabela que, acumulada, converte o valor preditivo da vigilância de hipótese
em medida.

---

## Estrutura

```
.
├── config.yaml                 parâmetros do módulo de qualidade da água
├── monitor_ndvi.py             módulo de macrófitas e envio dos avisos
├── backfill_historico.py       reconstrução da série de macrófitas
├── apurar_numeros.py           apuração de indicadores operacionais
├── data/
│   ├── roi.geojson             área de interesse — macrófitas
│   ├── setores/represa.geojson área de interesse — qualidade da água
│   └── campo.csv               observações in loco
├── src/
│   ├── sentinelhub.py          autenticação (com renovação do token), consulta e recuo do polígono
│   ├── notificacao.py          Telegram, e-mail e WhatsApp
│   ├── evalscripts.py          índices e máscaras de classificação
│   ├── analise.py              climatologia, z-score, níveis, persistência
│   ├── campo.py                validação contra observações de campo
│   ├── registrar_campo.py      conversão de issue em registro
│   └── monitor.py              orquestração da qualidade da água
├── docs/                       publicado via GitHub Pages
│   ├── index.html              painel com as duas abas
│   ├── macrofitas.html         painel original do módulo de macrófitas
│   ├── indicadores.html        explicação dos índices e série completa
│   ├── campo.html              formulário de campo (uso interno)
│   ├── data/                   séries, metadados e validação
│   └── images/                 painéis de imagem por data
└── .github/
    ├── workflows/
    │   ├── monitoramento.yml     os dois módulos — segundas e quintas
    │   ├── serie_historica.yml   reconstrução da série (água, macrófitas ou ambos), manual
    │   ├── registrar_campo.yml   converte issue de campo em registro
    │   └── apurar.yml            apuração de indicadores, manual
    └── scripts/
        ├── publicar.sh           commit e push com nova tentativa
        └── avisar_falha.sh       aviso de falha no Telegram
```

Os fluxos que gravam dados compartilham uma fila de concorrência
(`publicacao-dados`) para não disputarem o mesmo push.

O módulo de qualidade da água foi desenvolvido no repositório
`saolourenco`, hoje arquivado; seu endereço antigo redireciona para este
painel.

---

## Configuração

Conta gratuita no [Copernicus Data Space Ecosystem](https://dataspace.copernicus.eu/),
cliente OAuth em *Settings → OAuth clients*, e as credenciais em
*Settings → Secrets and variables → Actions*:

| Secret | Obrigatório | Uso |
|---|---|---|
| `SH_CLIENT_ID` | sim | Autenticação Sentinel Hub |
| `SH_CLIENT_SECRET` | sim | Autenticação Sentinel Hub |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | não | Notificação por Telegram |
| `EMAIL_ADDRESS`, `EMAIL_APP_PASSWORD`, `ALERT_EMAIL_TO` | não | Notificação por e-mail |
| `WHATSAPP_PHONE`, `WHATSAPP_APIKEY` | não | Notificação por WhatsApp |

A camada gratuita tem cota mensal de unidades de processamento. O consumo
cresce com o número de setores × execuções, não com o tamanho da série:
reconstruir dez anos são cerca de 41 requisições por módulo; a operação
corrente acrescenta cerca de 8 por mês, mais as imagens dos painéis.

## Execução local

```bash
pip install -r requirements.txt
export SH_CLIENT_ID=...  SH_CLIENT_SECRET=...

python -m src.monitor                      # janela padrão de 30 dias
python -m src.monitor --desde 2016-07-19   # série completa
python monitor_ndvi.py                     # módulo de macrófitas
```

Pelo navegador, sem instalar nada: aba **Actions**, escolha o fluxo e clique
em **Run workflow**. Localmente, `NOTIFICAR=0` roda sem enviar avisos.

## Replicar para outro reservatório

Troque o polígono em `data/setores/` e os parâmetros em `config.yaml`; o
código em `src/` é genérico. Depois reconstrua a série e recalcule a
climatologia sobre um período de referência que não contenha o fenômeno a
detectar. Limiares não são transferíveis entre áreas sem recalibração.

---

## Limitações

**Índices relativos, não concentrações.** Sem amostragem de campo para
calibração, o sistema produz anomalia frente ao histórico. Não escreva µg/L
de clorofila nem NTU de turbidez a partir destes dados.

**Correção atmosférica não específica para água.** O produto L2A destina-se
a superfícies terrestres e, sobre água escura, pode produzir reflectância
negativa no azul e no verde. Índices normalizados toleram isso; NDCI e NDTI,
que dependem mais de valores absolutos, sofrem mais. Correção específica
(ACOLITE, C2RCC) é o próximo passo técnico previsto.

**Não identifica origem.** O sistema detecta alteração espectral na
superfície da água. Não distingue lançamento de efluente de escoamento
agrícola, de revolvimento de sedimento por chuva ou de floração sazonal. Uma
anomalia a jusante de um ponto suspeito é indício técnico que justifica
investigação — não é prova de origem, e apresentá-la como tal enfraquece
qualquer encaminhamento formal.

**Limiares de classificação por calibrar.** Os cortes que convertem índice
em hectares são absolutos, não se ajustam ao histórico. Em anos sem evento o
sistema estima de 3,6 a 7,7 ha de cobertura flutuante, valor que
provavelmente reflete pixels de borda. É a calibração pendente de maior
impacto.

**Pixels mistos.** O reservatório tem perímetro 4,6 vezes maior que o de um
círculo de área equivalente. Mesmo após o recuo, a proporção de pixels que
contêm água e margem é significativa.

**CDOM é exploratório.** Em água interior o sinal se confunde com clorofila
e sedimento. Acompanha, não gera alerta.

### O que já foi verificado

A série é radiometricamente homogênea. Comparando cenas de alta confiança
antes e depois da mudança de baseline de processamento do Sentinel-2, em
janeiro de 2022, o degrau no NDVI é de 0,04 desvios-padrão — e abaixo de
0,41 em NDWI, NDTI e FAI. O risco de descontinuidade instrumental, que
poderia invalidar a comparação histórica, não se materializou.

---

## Citação

```
SCHMITT, Maurício Rodrigo. Sistema Automatizado de Monitoramento Ambiental
da Represa São Lourenço. Mafra: Universidade do Contestado, 2026. Software.
DOI: 10.5281/zenodo.22838354.
```

O DOI acima resolve sempre para a versão mais recente. Cada versão publicada
tem também um DOI próprio, indicado na respectiva release.

## Licença

A titularidade e o regime de licenciamento estão em definição junto à
Universidade do Contestado. Até que uma licença seja formalmente adotada, o
código permanece sob reserva integral de direitos — a ausência de arquivo de
licença não autoriza reuso.

## Autoria

**Maurício Rodrigo Schmitt**
Universidade do Contestado – UNC
Programa de Pós-Graduação em Engenharia Civil, Sanitária e Ambiental – PMPECSA
Grupo de Pesquisa CENAS (CNPq)

Dados Sentinel-2 do programa Copernicus, Agência Espacial Europeia.

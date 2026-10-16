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
| **Radar** | Sentinel-1, atravessa nuvens: área com retroespalhamento típico de vegetação flutuante | desde jul/2016, com lacuna em 2022–2024 |

Os três rodam na mesma execução agendada, um depois do outro, e publicam
num único commit. Cada um mantém seu próprio arquivo de série; o aviso
enviado por Telegram e e-mail reúne os dois.

---

## Frequência de atualização

| Etapa | Quando |
|---|---|
| Execução agendada | segunda e quinta, 09:17 UTC (06:17 BRT) — o GitHub pode atrasar algumas horas |
| Janela consultada | últimos 30 dias a cada execução: nenhuma passagem se perde, e reprocessar a mesma data apenas a sobrescreve |
| Passagens do Sentinel-2 | a cada 2 a 5 dias; cenas totalmente encobertas são descartadas, então a série tem em média uma observação a cada 5 dias |
| Passagens do Sentinel-1 (radar) | a cada 5 a 7 dias, com ou sem nuvem; só órbita descendente sobre Mafra |
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

**Um nível só, no topo da página.** Radar, qualidade da água e NDVI viram
um semáforo — Normal, Vigilância ou Alerta — com o que fazer em cada caso.

**Aviso por Telegram e e-mail quando o nível muda**, com lembrete a cada
execução enquanto houver alerta e um resumo de rotina às segundas.

---

## Alerta em dois níveis

### Vigilância — antecipação

Dispara quando a clorofila relativa se afasta do padrão histórico do mês
enquanto a cobertura ainda está dentro do normal.

O fundamento é empírico: em 12/02/2025 o NDCI atingiu z = 5,1 com a
cobertura em 10 ha. O NDVI só confirmou alerta em 29/03 — 45 dias depois —,
a cobertura passou de 30 ha em 12/06 e o pico veio em setembro (72 ha).

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

## Nível único (semáforo)

`src/alerta.py` roda por último e junta as leituras recentes num nível só,
que a página mostra no topo e que decide os avisos.

| Nível | Quando | O que fazer |
|---|---|---|
| 🟢 Normal | nenhum sinal | nada; o monitoramento segue automático |
| 🟡 Vigilância | sinal ainda não confirmado: clorofila acima do padrão (vale por 60 dias), radar em atenção por duas passagens ou em alerta numa só, NDVI confirmado mas radar normal, ou área crescendo rápido no radar | visita de campo nas próximas semanas |
| 🔴 Alerta | alerta **confirmado** no radar, na água (índices concordantes) ou no NDVI | verificação de campo e comunicar o responsável pela represa |

Sobe assim que um sinal aparece. Para descer exige 3 execuções seguidas no
nível mais baixo (≈ 10 dias), para o semáforo não piscar entre passagens.
Cada leitura só conta enquanto é recente (radar 21 dias, água e NDVI 30) e
de confiança suficiente.

**Reproduzido sobre a série 2016–2026**, execução a execução:

- evento de 2025: **Vigilância desde 13/02/2025** e **Alerta em
  31/03/2025**, quando o NDVI confirmou; a cobertura só passou de 30 ha em
  junho e o pico veio em setembro;
- nenhum Alerta fora do evento. Os dois que haveria (mar/2018, jan/2020)
  vinham só do NDVI, com o radar normal, e por isso ficam como Vigilância;
- 8 Vigilâncias sem evento em seguida, cerca de 2 meses cada. É o preço da
  antecipação: o sinal da clorofila acerta cerca de 1 em 6;
- 25 mudanças de nível em 10 anos.

### Avisos

Configuráveis em `config.yaml`, seção `avisos`:

- **mudança de nível**, para cima ou para baixo (entrar ou sair de "sem
  leitura recente" não avisa);
- **lembrete** a cada execução enquanto o nível for Alerta;
- **resumo semanal** às segundas, para mostrar que o sistema está vivo.

Na aba Actions, *Run workflow* com a caixa marcada força o envio; desmarcada,
roda sem avisar ninguém.

### Condições ambientais

`src/clima.py` busca no Open-Meteo a chuva dos últimos 90 dias e a
temperatura média dos últimos 30, comparadas com o normal da época do ano
(ERA5, 2016–2024). Chuva forte leva nutrientes à represa; calor acelera o
crescimento. Antes do surto de 2025 houve chuva muito acima do normal em
dez/2024 e um fim de verão quente em fev–mar/2025 — mas é um evento só, e
por isso as condições aparecem como **contexto** e não mudam o nível.

---

## Radar — através das nuvens

Na estação chuvosa o Sentinel-2 chega a passar 20 dias sem uma cena
aproveitável, justamente quando a infestação cresce. O Sentinel-1 é um radar:
não depende de luz nem de céu limpo.

Água parada funciona como um espelho e devolve pouco sinal ao satélite
(polarização VH em torno de −25 dB). Uma camada de macrófitas espalha a onda
e devolve muito mais (em torno de −18 dB). O módulo `src/radar.py` mede a
fração da represa com VH acima de −20 dB, converte em hectares e compara com
o padrão daquele mês.

| | Água normal (2016–2021, 2026) | Evento de 2025 (ago–nov) |
|---|---|---|
| Área com VH > −20 dB | 15% ± 3% da represa | 72% |
| Disparos com z ≥ 3 em duas passagens seguidas | nenhum em 374 passagens | confirmado em 31/05/2025 |

O radar confirmou o evento em 31/05/2025. Quando há cena aproveitável, o
NDVI óptico — depois do recuo da margem — confirma antes (29/03/2025); o
radar cobre os períodos encobertos e serve de contraprova. Foi contra ele
que a classificação óptica foi recalibrada (ver *Área de interesse e
classificação*): a cena limpa de 07/10/2026, que a regra antiga lia como
52 ha de cobertura flutuante, era água barrenta, e o radar seguiu normal.

**Limitações.** O radar detecta vegetação já formada; o sinal precoce de
clorofila, que antecede a infestação, só existe no módulo óptico. Sobre
Mafra só há órbita descendente. Entre dezembro de 2021 e abril de 2025 quase
não houve passagens, por causa da pane do Sentinel-1B, e a climatologia de
referência usa portanto 2016–2021. Vento e vegetação das margens mantêm
sempre alguns hectares de assinatura, mesmo com a represa limpa.

Parâmetros e calibração em `config.yaml`, seção `radar`.

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

## Área de interesse e classificação

Polígono digitalizado manualmente sobre imagem de alta resolução, com 502
vértices e 79,49 ha.

Antes do processamento aplica-se um **recuo de 10 m** — exatamente um pixel.
Isso descarta o anel que mistura água e margem dentro do mesmo pixel, onde
vegetação ripária eleva os índices sem que nada tenha ocorrido na lâmina
d'água. A área efetiva cai para 65,10 ha, 7.334 pixels após rasterização.

Os três módulos usam o mesmo polígono e o mesmo recuo. O de macrófitas
passou a usá-lo em out/2026: com a margem incluída, o NDVI de referência da
represa ficava em torno de +0,06; sem ela, em torno de −0,15. A série
antiga, sem recuo, está preservada em `docs/data/historico_sem_recuo.csv`,
e a climatologia passou a ser derivada da própria série
(`data/climatologia_macrofitas.json`) em vez de copiada da planilha do
artigo. Com isso o NDVI confirmou o evento de 2025 em 29/03, cinco semanas
antes do que confirmava sem o recuo (03/05).

### Classificação em hectares

Os limiares que convertem índice em área foram calibrados contra o radar,
sobre 124 pares de cena óptica limpa e passagem de radar com até 3 dias:

| Cobertura flutuante | correlação com o radar | erro médio | anos normais |
|---|---|---|---|
| FAI > 0,02 (regra antiga) | 0,73 | 6,7 ha | 12,6 ± 12,6 ha |
| **NDVI > 0,2 e FAI > 0,02** | **0,89** | **4,7 ha** | 9,1 ± 6,3 ha |
| radar (referência) | — | — | 10,6 ± 2,3 ha |

A regra antiga confundia água barrenta com vegetação, porque o FAI sobe com
sedimento em suspensão; exigir NDVI > 0,2 junto separa planta de sedimento.
A lâmina d'água passou de NDWI > 0, que em cenas limpas marcava quase nada
de água, para NDVI < 0,1.

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
│   ├── radar.py                radar Sentinel-1 — vegetação flutuante através das nuvens
│   ├── clima.py                chuva e temperatura vs. normal (Open-Meteo)
│   ├── alerta.py               nível único, política de avisos e mensagem
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
    │   ├── serie_historica.yml   reconstrução da série (água, radar, macrófitas ou todos), manual
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
python -m src.radar                        # radar, janela de 30 dias
python -m src.clima                        # condições ambientais
python monitor_ndvi.py                     # módulo de macrófitas
NOTIFICAR=0 python -m src.alerta           # nível único, sem enviar avisos
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

**Limiares calibrados contra outro satélite, não contra o campo.** A
classificação em hectares foi ajustada para concordar com o radar, que também
tem erro: vento e margem mantêm cerca de 10 ha de assinatura mesmo com a
represa limpa. A floração (NDCI) não foi recalibrada, por falta de
referência. Observações de campo continuam sendo a validação que falta.

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

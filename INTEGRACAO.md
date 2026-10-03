# Integrar o módulo de qualidade da água ao repositório existente

Este guia descreve como incorporar o monitoramento multiparamétrico de
qualidade da água ao repositório `saolourencoalert`, publicando o resultado
como **versão 2.0.0** do mesmo produto — em vez de criar um produto novo.

A escolha preserva o que já existe: o DOI de conceito do Zenodo continua
válido, a nova release entra como versão da mesma linhagem, e os endereços
já registrados na documentação do PTT seguem funcionando.

---

## O que não muda

- `monitor_ndvi.py`, `backfill_historico.py` e o arquivo de série
  `docs/data/historico.csv` permanecem intocados;
- os painéis em `docs/images/` continuam sendo gerados pelo mesmo módulo;
- os secrets `SH_CLIENT_ID` e `SH_CLIENT_SECRET` já configurados servem aos
  dois módulos;
- o painel original é preservado como `docs/macrofitas.html`.

## O que é acrescentado

```
config.yaml                         novo — parâmetros do módulo de água
data/setores/represa.geojson        novo — polígono do reservatório
src/                                novo — código do módulo de água
  ├── __init__.py
  ├── evalscripts.py
  ├── sentinelhub.py
  ├── analise.py
  └── monitor.py
docs/index.html                     SUBSTITUÍDO — painel com duas abas
docs/macrofitas.html                novo — cópia do painel original
docs/indicadores.html               novo — explicação e série completa
.github/workflows/agua.yml                    novo
.github/workflows/agua_serie_historica.yml    novo
.github/workflows/ndvi_monitor.yml  ALTERADO — ver abaixo
requirements.txt                    ALTERADO — união das dependências
```

Os dois módulos escrevem em `docs/data/`, mas em arquivos distintos:

| Módulo | Arquivos que escreve |
|---|---|
| Macrófitas | `historico.csv`, `ultimo.json`, `passagens_30d.json`, `images/` |
| Qualidade da água | `represa.csv`, `resumo.json`, `setores.geojson` |

Não há colisão de nomes.

---

## Passo a passo

### 1. Preserve o painel atual

Antes de qualquer coisa, no GitHub: abra `docs/index.html`, clique no lápis,
e use o menu de três pontos → *Rename*. Troque o nome para
`macrofitas.html` e confirme.

Isso mantém o painel original acessível e intacto. A aba de macrófitas do
painel novo aponta para ele.

### 2. Suba os arquivos novos

Pelo navegador, respeitando as pastas:

| Arquivo | Destino |
|---|---|
| `config.yaml` | raiz |
| `requirements.txt` | raiz (substitui) |
| `src/*.py` | `src/` |
| `data/setores/represa.geojson` | `data/setores/` |
| `docs/index.html` | `docs/` |
| `docs/indicadores.html` | `docs/` |
| `.github/workflows/agua.yml` | `.github/workflows/` |
| `.github/workflows/agua_serie_historica.yml` | `.github/workflows/` |
| `.github/workflows/ndvi_monitor.yml` | `.github/workflows/` (substitui) |

Para criar pastas que ainda não existem, use *Add file → Create new file* e
digite o caminho completo com barras: `src/monitor.py`, por exemplo.

### 3. Rode a série histórica do módulo de água

Actions → *Qualidade da água — construir série histórica* → Run workflow.
Deixe a data inicial padrão. Ao terminar, `docs/data/` deve conter
`represa.csv`, `resumo.json` e `setores.geojson`.

### 4. Publique a versão 2.0.0

Releases → *Draft a new release* → tag `v2.0.0`. O Zenodo arquiva
automaticamente e emite um DOI de versão novo, sob o mesmo DOI de conceito.

---

## Por que o workflow antigo mudou

Três ajustes, todos necessários para a convivência:

**Fila compartilhada.** Os dois módulos declaram
`concurrency: group: publicacao-dados`, de modo que um espera o outro em vez
de os dois tentarem publicar ao mesmo tempo.

**Agenda defasada.** Macrófitas às 09:00 UTC, água às 09:40, nas mesmas
segundas e quintas. Reduz a chance de encontro.

**Publicação resiliente.** O push original falhava quando o remoto avançava
durante o processamento — foi exatamente o que ocorreu em 28/09. Agora há
`git pull --rebase` e até cinco tentativas antes de desistir. Sem isso, com
dois módulos publicando, a falha passaria a ser rotineira.

---

## Consumo da cota gratuita

O módulo de água consulta a Statistical API uma vez por bloco de 90 dias
por setor. A reconstrução completa da série, dez anos e um setor, são cerca
de 41 requisições. A operação corrente, duas execuções semanais, acrescenta
cerca de 8 requisições por mês. Cada requisição pede estatísticas agregadas
sobre um polígono de 65 ha, não imagens.

Ao acrescentar setores, o custo multiplica pelo número deles. Confira a
cota vigente da sua conta antes de configurar muitos.

---

## Reflexo na documentação do PTT

A integração favorece o enquadramento como **produto único de escopo
ampliado**, e não como dois produtos. Os documentos precisam refletir isso:

- o título do produto passa a descrever monitoramento ambiental
  multiparamétrico, não apenas macrófitas;
- a versão sobe para 2.0.0 em todos os documentos;
- o DOI da versão muda; o de conceito permanece;
- a seção de evolução prevista incorpora o que foi realizado;
- a complexidade e a inovação ganham argumento: sete índices, setorização,
  classificação por pixel com saída em hectares e critério de persistência.

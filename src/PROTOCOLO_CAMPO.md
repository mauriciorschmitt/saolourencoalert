# Protocolo de observação de campo

O sistema compara a si mesmo há dez anos. Toda afirmação que ele faz é
verificada contra sua própria série — o que é circular. Observações de campo
quebram essa circularidade, e são hoje a lacuna mais importante do projeto.

Este protocolo foi escrito para ser cumprido **a pé, da margem, com um
celular**, em menos de vinte minutos. Não exige barco, drone nem
equipamento. Uma observação simples e bem datada vale mais que um
levantamento elaborado que nunca acontece.

---

## O que registrar

Você não consegue medir 73 hectares da margem, e não precisa. O que o
sistema precisa saber é **categórico**:

| Categoria | O que significa |
|---|---|
| `AUSENTE` | Nenhuma macrófita visível na água |
| `ISOLADA` | Manchas pequenas e dispersas, água aberta predomina |
| `MODERADA` | Manchas extensas, mas ainda há bastante água livre |
| `EXTENSA` | Macrófita predomina no campo de visão |

Registrar `AUSENTE` numa data em que o sistema indicou normalidade é tão
valioso quanto confirmar um alerta. São os dois tipos de acerto, e sem o
primeiro não há como estimar falso positivo.

## Quando ir

Três situações justificam uma ida:

1. **Quando o sistema emite vigilância ou alerta.** É a verificação de maior
   valor: responde se o aviso procedia.
2. **Periodicamente, sem aviso.** Duas ou três vezes por ano, em datas
   quaisquer. Constrói a base de verdadeiros negativos.
3. **Logo após uma passagem do satélite.** Quanto menor a distância entre a
   observação e a cena, mais direta a comparação. Até sete dias é bom; até
   três é ótimo.

## Como fazer

1. Escolha um ponto de onde se enxergue uma extensão razoável da água.
   Use os mesmos pontos sempre que possível — comparar o mesmo ângulo ao
   longo do tempo revela mudança melhor que fotos de lugares diferentes.
2. Tire ao menos três fotos: uma panorâmica, uma da água próxima à margem e
   uma de qualquer mancha de vegetação.
3. **Confirme que a câmera está gravando localização.** No Android, em
   Câmera → Configurações → Marcas de localização; no iPhone, em Ajustes →
   Privacidade → Serviços de Localização → Câmera. Sem isso a foto perde
   metade do valor como evidência.
4. Anote a hora e a categoria observada.
5. Se houver vegetação, fotografe de perto. Identificação de espécie é
   desejável mas opcional — `indeterminada` é resposta aceitável.

## Como registrar no sistema

Abra `data/campo.csv` no GitHub, clique no lápis e acrescente uma linha.
Funciona pelo celular, no próprio navegador.

```
data,hora,ponto,latitude,longitude,cobertura,especie,metodo,observador,fotos,notas
2026-10-12,09:30,barragem,-26.1648,-49.8911,AUSENTE,,margem,MRS,,"água limpa, sem manchas"
```

| Coluna | Preenchimento |
|---|---|
| `data` | AAAA-MM-DD, obrigatória |
| `hora` | HH:MM, aproximada basta |
| `ponto` | Nome curto e **constante** para o mesmo local |
| `latitude`, `longitude` | Graus decimais; o celular informa |
| `cobertura` | AUSENTE, ISOLADA, MODERADA ou EXTENSA |
| `especie` | Nome, `indeterminada`, ou vazio |
| `metodo` | margem, barco, drone, outro |
| `observador` | Iniciais bastam |
| `fotos` | Link, ou nome dos arquivos se forem anexados ao repositório |
| `notas` | Texto livre entre aspas, se houver vírgulas |

Não é preciso rodar nada depois. O próximo monitoramento incorpora a
observação e atualiza a validação.

---

## O que o sistema faz com isso

A rotina `src/campo.py` associa cada observação à passagem de satélite mais
próxima e compara o que o sistema disse com o que você viu. Dessa
comparação saem três coisas:

**Tabela de concordância.** Quantas vezes o sistema acertou, em que direção
errou, e com qual frequência.

**Desfecho dos alertas.** Cada vigilância e cada alerta confirmado passa a
ter um resultado registrado. É assim que, em dois ou três anos, o valor
preditivo da vigilância deixa de ser uma hipótese baseada em um evento.

**Calibração dos limiares.** Observações em que você estimou a cobertura
permitem ajustar o limiar do FAI, hoje definido por aproximação.

Uma ressalva importante: observação de margem enxerga uma fração do
reservatório, enquanto o satélite integra os 73 ha. Discordância entre os
dois nem sempre é erro do sistema — pode ser que a mancha esteja num braço
que você não vê. Registre o ponto de observação para que essa distinção
seja possível depois.

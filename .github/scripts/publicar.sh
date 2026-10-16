#!/usr/bin/env bash
# Commita os caminhos indicados e publica na branch atual.
#
#   bash .github/scripts/publicar.sh "mensagem do commit" caminho [caminho...]
#
# O remoto pode ter avançado enquanto o processamento rodava (um envio pela
# interface web, o registro de uma observação de campo). Rebase e tenta de
# novo, em vez de falhar e descartar o trabalho já feito.
set -uo pipefail

mensagem="$1"; shift

git config user.name  "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

git add -- "$@"
if git diff --cached --quiet; then
  echo "Nada mudou — nenhum commit necessário."
  exit 0
fi
git commit -q -m "$mensagem"

for tentativa in 1 2 3 4 5; do
  if git pull --rebase --autostash origin "${GITHUB_REF_NAME}" \
     && git push origin "HEAD:${GITHUB_REF_NAME}"; then
    echo "Publicado na tentativa $tentativa."
    exit 0
  fi
  echo "Tentativa $tentativa falhou; aguardando..."
  sleep $((tentativa * 5))
done
echo "::error::Não foi possível publicar após 5 tentativas."
exit 1

#!/usr/bin/env bash
# Avisa no Telegram que o workflow falhou, para todos os chats configurados.
# Usa só curl de propósito: precisa funcionar mesmo quando a instalação das
# dependências Python é justamente o que falhou.
#
#   bash .github/scripts/avisar_falha.sh "texto do aviso"
set -uo pipefail

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
  echo "Telegram não configurado; aviso de falha não enviado."
  exit 0
fi

texto="$1 https://github.com/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"
IFS=',' read -ra IDS <<< "$TELEGRAM_CHAT_ID"
for id in "${IDS[@]}"; do
  id="${id// /}"
  [ -z "$id" ] && continue
  curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${id}" --data-urlencode "text=${texto}" > /dev/null
done

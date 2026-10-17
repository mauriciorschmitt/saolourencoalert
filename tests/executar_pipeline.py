"""
Roda o pipeline inteiro, na ordem do workflow agendado, sobre a cópia do
repositório que é o diretório atual, com a rede simulada. Imprime no fim uma
linha JSON com o que aconteceu, que tests/test_pipeline.py confere.

Roda em processo separado porque os módulos gravam em docs/data/ relativo à
própria localização: o teste aponta para uma cópia e não suja o repositório.
"""
import json
import os
import runpy
import sys
from pathlib import Path

raiz = Path.cwd()
sys.path.insert(0, str(raiz))
sys.path.insert(0, str(raiz / "tests"))

import rede_simulada as rede  # noqa: E402

rede.instalar()
os.environ.update({
    "SH_CLIENT_ID": "00000000-0000-0000-0000-000000000000",
    "SH_CLIENT_SECRET": "segredo",
    "TELEGRAM_BOT_TOKEN": "bot", "TELEGRAM_CHAT_ID": "111, 222",
    "EMAIL_ADDRESS": "a@x", "EMAIL_APP_PASSWORD": "p", "ALERT_EMAIL_TO": "b@x,c@x",
})
os.environ.pop("NOTIFICAR", None)


def rodar(modulo, *args, caminho=False):
    sys.argv = [modulo, *args]
    try:
        if caminho:
            runpy.run_path(str(raiz / modulo), run_name="__main__")
        else:
            runpy.run_module(modulo, run_name="__main__")
    except SystemExit as e:
        if e.code not in (0, None):
            raise RuntimeError(f"{modulo} saiu com {e.code}")


resultado = {}
rodar("src.monitor")
rodar("src.radar")
rodar("src.clima")
rodar("monitor_ndvi.py", caminho=True)
resultado["avisos_antes_do_alerta"] = len(rede.CHAMADAS["telegram"]) + len(rede.CHAMADAS["email"])

(raiz / "docs/data/alerta.json").unlink(missing_ok=True)
rodar("src.alerta")
resultado["telegram_1a"] = len(rede.CHAMADAS["telegram"])
rodar("src.alerta")
resultado["telegram_2a"] = len(rede.CHAMADAS["telegram"])
rodar("src.alerta", "--forcar-aviso")
resultado["telegram_forcado"] = len(rede.CHAMADAS["telegram"])

resultado["chamadas"] = {k: v for k, v in rede.CHAMADAS.items() if not isinstance(v, list)}
resultado["telegram"] = rede.CHAMADAS["telegram"][:2]
resultado["email"] = rede.CHAMADAS["email"][:1]
resultado["alerta"] = json.loads((raiz / "docs/data/alerta.json").read_text(encoding="utf-8"))
resultado["arquivos"] = sorted(p.name for p in (raiz / "docs/data").iterdir())
print("RESULTADO " + json.dumps(resultado, ensure_ascii=False, default=str))

"""docs/PROMPTS.md precisa refletir os prompts do código. Se falhar: rode  python -m src.documentar_prompts"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import documentar_prompts as DP  # noqa: E402
from src import llm  # noqa: E402


def test_documento_de_prompts_atualizado():
    assert DP.DESTINO.exists(), "gere com: python -m src.documentar_prompts"
    assert DP.DESTINO.read_text(encoding="utf-8") == DP.gerar(), \
        "docs/PROMPTS.md desatualizado: rode  python -m src.documentar_prompts"


def test_documento_contem_os_prompts_e_nao_vaza_dados():
    doc = DP.gerar()
    for trecho in (llm.PROMPT_JUIZ[:60], llm.REGRA_PUBLICA.strip()[:60], llm.VERSAO_PROMPT_RESPOSTA,
                   llm.VERSAO_PROMPT_JUIZ, llm.MODELO_JUIZ, "<mensagem_cliente>"):
        assert trecho in doc
    assert "GCH556292" not in doc  # exemplo do Reclame Aqui sai mascarado

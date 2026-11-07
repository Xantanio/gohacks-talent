"""Gera docs/PROMPTS.md a partir do código: os prompts que os agentes REALMENTE usam, para revisão e validação.

Uso:  .\\venv\\Scripts\\python.exe -m src.documentar_prompts
O teste tests/test_prompts_doc.py falha se o documento estiver desatualizado em relação ao código.
Não chama o proxy: os exemplos são renderizados localmente com um ticket real (AT116524), já mascarado.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import agente, llm
from .decisao import decidir_ticket
from .ferramentas import Ambiente, pedido_na_posicao
from .politicas import texto_regra

DESTINO = Path(__file__).resolve().parent.parent / "docs" / "PROMPTS.md"
TICKET_EXEMPLO = ("AT116524", "erro_personalizacao")
TEXTO_EXEMPLO = ("Oi! Sentimos muito pela personalização do seu pedido. A reposição foi aberta em 30/10 e está "
                 "aguardando expedição. Qualquer dúvida, estamos à disposição!")


def _bloco(texto: str, linguagem: str = "text") -> str:
    return f"```{linguagem}\n{texto.rstrip()}\n```"


def _mensagens(msgs: list[dict[str, str]]) -> str:
    return "\n\n".join(f"**[{m['role']}]**\n\n{_bloco(m['content'])}" for m in msgs)


def gerar() -> str:
    amb = Ambiente()
    cid, categoria = TICKET_EXEMPLO
    conversa = amb.ler_conversa(cid)
    meta = conversa["conversa"]
    pedido = pedido_na_posicao(amb, meta["order_id"])
    d = decidir_ticket(amb, cid, categoria)
    texto_cliente = "\n".join(m["texto"] or "" for m in conversa["mensagens"] if m["autor"] == "cliente")
    fatos = list(d.avaliacao.fatos) + [f"transportadora do pedido: {pedido['transportadora']}"]
    acoes = [f"{p.ferramenta}: {p.nota}" for p in d.passos if p.nota]
    publica = meta["canal"] in agente.FERRAMENTAS_DE_MENSAGEM or meta["canal"] == "reclame_aqui"

    estado_jev = llm.montar_estado_classificacao(conversa, pedido)
    redator = llm.montar_prompt_resposta(empresa="Gocase", publica=publica, intencao=d.intencao_resposta,
                                         acoes=acoes, fatos=fatos, mensagem_cliente=texto_cliente)
    juiz = llm.montar_prompt_juiz(texto=TEXTO_EXEMPLO, empresa="Gocase", publica=publica,
                                  intencao=d.intencao_resposta, acoes=acoes, fatos=fatos,
                                  mensagem_cliente=texto_cliente)
    sistema_privado = llm.REGRAS_REDACAO.format(empresa="{empresa}", regra_publica="")
    promessas = "\n".join(f"| `{f}` | `{p}` |" for f, p in llm.PROMESSAS.items())
    notas = " · ".join(f"{c} ≥ {n}" for c, n in llm.NOTA_MINIMA.items())
    criterios = "\n".join(f"| {c} | {txt} | {llm.NOTA_MINIMA[c]} |" for c, txt in llm.CRITERIOS_JUIZ.items())

    L = [
        "# Prompts dos agentes",
        "",
        "> **Gerado automaticamente** por `python -m src.documentar_prompts` a partir do código (`src/llm.py`, "
        "`src/agente.py`). Não edite à mão: mude o código e gere de novo. O teste `tests/test_prompts_doc.py` "
        "falha se este arquivo estiver desatualizado.",
        "",
        "Princípio (CLAUDE.md, Guardrails): **a IA não escolhe ferramentas nem decide dinheiro.** O Jev classifica, o "
        "redator escreve a partir de uma decisão já tomada pelo código, e o juiz avalia o texto. Quem decide, "
        "executa e aprova é o código.",
        "",
        "## Visão geral",
        "",
        "| Etapa | Modelo | Endpoint | Versão do prompt | Parâmetros | Saída | Quem decide com ela |",
        "|---|---|---|---|---|---|---|",
        f"| 1. Classificação | Jev (`jev-latest`) | `POST /v1/systemone` | `{llm.VERSAO_PROMPT_CLASSIFICACAO}` | "
        f"perguntas tipadas (choice/score/noul) | categoria + confiança + sinais | código: confiança < "
        f"{llm.LIMIAR_CONFIANCA} → análise de risco (D-020) |",
        f"| 2. Redação | `{llm.MODELO_TEXTO}` | `POST /v1/chat/completions` | `{llm.VERSAO_PROMPT_RESPOSTA}` | "
        f"max_tokens {llm.MAX_TOKENS_RESPOSTA}, reasoning_effort `{llm.ESFORCO_RACIOCINIO}` | JSON "
        "`{resposta, justificativa}` | validador + juiz |",
        f"| 3. Validação determinística | — (código) | — | — | regras abaixo | aprovado / motivos | código |",
        f"| 4. Juiz | `{llm.MODELO_JUIZ}` | `POST /v1/chat/completions` | `{llm.VERSAO_PROMPT_JUIZ}` | "
        "max_tokens 2000 | JSON com notas 1–5 e justificativas | código: mínimos por critério |",
        f"| 5. Reescrita | `{llm.MODELO_TEXTO}` | mesmo chat da redação | — | até {agente.TENTATIVAS_REDACAO} "
        "tentativas no total | nova redação | validador + juiz de novo; depois pessoa |",
        "",
        "Proteções comuns a todas as chamadas:",
        "- texto do cliente sempre **delimitado** (`<mensagem_cliente>`) e tratado como dado, nunca instrução;",
        "- CPF, cartão e telefone **mascarados antes** de sair para o modelo; no Reclame Aqui também o número do "
        "pedido e o rastreio;",
        "- **falha segura:** erro, 429 esgotado, timeout ou JSON inválido → nenhum chute; o caso vai para uma pessoa "
        "(o juiz fora do ar é exceção, ver D-022);",
        "- custo de toda chamada registrado (Jev em US$; chat em tokens, sem preço por chamada);",
        "- modelos `deepseek-*` recusados no próprio cliente.",
        "",
        f"Exemplo usado abaixo: ticket real **{cid}** (Reclame Aqui, {categoria}), renderizado localmente sem chamar "
        "o proxy.",
        "",
        "---",
        "",
        f"## 1. Classificação — Jev (`{llm.VERSAO_PROMPT_CLASSIFICACAO}`)",
        "",
        "O Jev não recebe um prompt de texto: recebe um **estado** e **perguntas tipadas**. "
        f"Conversa sem mensagem do cliente vira `sem_interacao` por regra, sem chamada.",
        "",
        "### Perguntas (`questions`)",
        "",
        _bloco(json.dumps(llm.PERGUNTAS_JEV, ensure_ascii=False, indent=2), "json"),
        "",
        f"### Estado enviado (`state`) — exemplo {cid}",
        "",
        _bloco(json.dumps(estado_jev, ensure_ascii=False, indent=2), "json"),
        "",
        "### Como o código usa a resposta",
        "",
        f"- `confidence` < {llm.LIMIAR_CONFIANCA} → o agente testa a decisão para cada categoria com probabilidade ≥ "
        f"{agente.LIMIAR_CATEGORIA_CANDIDATA}: se todas levam só a ações sem risco "
        f"({', '.join(sorted(agente.ACOES_SEM_RISCO))}), segue automático; senão, pessoa (D-020).",
        "- `outros` ou categoria fora da lista → pessoa. `irritacao` só muda prioridade e estilo, nunca a veracidade.",
        "- `caso_sensivel` ou `ameaca_externa` > 0,5 → resposta retida para revisão humana.",
        "",
        "---",
        "",
        f"## 2. Redação — `{llm.MODELO_TEXTO}` (`{llm.VERSAO_PROMPT_RESPOSTA}`)",
        "",
        "Só é chamado quando a decisão pede texto livre ou quando nenhuma ferramenta de mensagem respondeu (D-019).",
        "",
        "### Prompt de sistema (canal privado: WhatsApp, e-mail)",
        "",
        _bloco(sistema_privado),
        "",
        "### Acréscimo no canal público (Reclame Aqui), inserido na regra 3",
        "",
        _bloco(llm.REGRA_PUBLICA.strip()),
        "",
        "### Mensagem do usuário (modelo)",
        "",
        _bloco("INTENÇÃO: <intenção decidida pelo código>\n"
               "AÇÕES (o que o sistema fez ou vai fazer): <ferramentas executadas, pendentes e opções ao cliente>\n"
               "FATOS que podem ser citados: <fatos dos sistemas, mascarados>\n"
               "<mensagem_cliente>\n<texto do cliente, mascarado>\n</mensagem_cliente>"),
        "",
        f"### Exemplo renderizado — {cid}",
        "",
        _mensagens(redator),
        "",
        "---",
        "",
        "## 3. Validação determinística (código, não é prompt)",
        "",
        "Roda antes do juiz. Reprova o texto quando:",
        "",
        "- contém dado pessoal (CPF, cartão, telefone) e, no canal público, número de pedido completo ou rastreio;",
        f"- usa termo de acusação: `{llm.PALAVRAS_PROIBIDAS.pattern}`;",
        f"- promete compensação inexistente (não há ferramenta de cupom): `{llm.COMPENSACAO.pattern}`;",
        "- promete uma ferramenta de dinheiro que a decisão não executa nem oferece (tabela abaixo). Ação só "
        "**pendente de aprovação** pode ser citada, mas nunca como feita;",
        "- cita valor em R$ que não está na decisão;",
        "- cita prazo (número + unidade, data) que não está nos fatos nem na regra da política, ou promessa vaga "
        "(\"amanhã\", \"até sexta\") (D-021);",
        f"- cita transportadora diferente da do pedido (inclui \"Correios\", que nenhum pedido usa): "
        f"{', '.join(llm.TRANSPORTADORAS)};",
        "- passa de 1.200 caracteres.",
        "",
        "| Ferramenta | Padrão que conta como promessa |",
        "|---|---|",
        promessas,
        "",
        f"Padrão de ação apresentada como já feita (vale para ações pendentes): `{llm.CONCLUIDO.pattern}`",
        "",
        "---",
        "",
        f"## 4. Juiz — `{llm.MODELO_JUIZ}` (`{llm.VERSAO_PROMPT_JUIZ}`)",
        "",
        f"Modelo de outra família que o redator (GPT × GLM), para não avaliar o próprio texto. Só avalia texto que "
        f"passou no validador. **Quem aprova é o código**: {notas}.",
        "",
        "| Critério | O que o juiz avalia | Nota mínima |",
        "|---|---|---|",
        criterios,
        "",
        "### Prompt de sistema",
        "",
        _bloco(llm.PROMPT_JUIZ),
        "",
        f"### Exemplo renderizado — {cid} (texto proposto de exemplo)",
        "",
        _mensagens(juiz),
        "",
        "---",
        "",
        "## 5. Reescrita (autocorreção)",
        "",
        f"Quando o validador ou o juiz reprova, a conversa com o redator continua com a resposta reprovada e esta "
        f"mensagem. São até {agente.TENTATIVAS_REDACAO} tentativas no total; depois, a resposta fica retida para uma "
        "pessoa.",
        "",
        _bloco(agente.MENSAGEM_REESCRITA),
        "",
        "`{motivo}` é `revisão automática: <motivos do validador>` ou `revisor de qualidade: <critérios reprovados, "
        "justificativas, problemas e sugestão do juiz>`.",
        "",
        "---",
        "",
        "## 6. Onde ficam os registros de cada chamada",
        "",
        "| Arquivo (em `saida/<execução>/`) | Conteúdo |",
        "|---|---|",
        "| `validacoes.csv` | uma linha por tentativa de texto: redator (modelo, versão, justificativa), validador "
        "(motivos), juiz (notas, justificativas, problemas, sugestão) e resultado |",
        "| `acoes_executadas.csv` | trilha de cada ação, incluindo `origem` (ex.: `llm glm-5.3 (resposta-v2) + validador "
        "+ juiz gpt-6-astra (juiz-v2)`) |",
        "| `dossies/<ticket>.md` | classificação com justificativa do Jev, decisão, ações, texto final e parecer do juiz |",
        "| `log_chamadas.jsonl` | toda chamada de ferramenta, inclusive as recusadas |",
        "",
        "---",
        "",
        "## 7. Lista de verificação para quem valida os prompts",
        "",
        "- [ ] O redator recebe só INTENÇÃO, AÇÕES e FATOS decididos pelo código, e nada que permita decidir dinheiro?",
        "- [ ] O texto do cliente está delimitado e mascarado em todos os prompts (Jev, redator, juiz)?",
        "- [ ] As regras do redator e as do juiz são coerentes entre si (ex.: ausência de prazo é correta para os dois)?",
        "- [ ] Os mínimos do juiz estão adequados ao risco (fidelidade, veracidade e segurança mais rígidos)?",
        "- [ ] As categorias do Jev cobrem todas as categorias de `politicas.csv` (+ `outros`)?",
        "- [ ] Mudou algum prompt? Então suba a versão (`VERSAO_PROMPT_*`), gere este arquivo e registre em "
        "`docs/DECISOES.md` o que mudou e por quê.",
        "",
    ]
    return "\n".join(L)


def main() -> None:
    DESTINO.write_text(gerar(), encoding="utf-8")
    print(f"gerado: {DESTINO}")


if __name__ == "__main__":
    main()

"""Cadeira do SUPERVISOR: resolve as pendências que os agentes deixaram e dispara a retomada. (D-027)

Fecha o ciclo da "mão levantada": o agente pede, uma pessoa decide aqui (pela interface ou por código), e o próprio
agente retoma o caso sem intervenção extra. Cada decisão usa a ferramenta do catálogo com o papel certo:
- aprovar / rejeitar      -> aprovar_acao / rejeitar_acao (supervisor) -> atendente executa com aprovacao_id e avisa
- decidir_suspeita        -> decidir_suspeita (supervisor) -> procedente: atendente reprocessa o ticket
- liberar / descartar     -> atendente publica a mensagem retida (texto original ou editado, sempre pelo validador)
- registrar_cosmetovig.   -> registrar_cosmetovigilancia (supervisor), com sku/lote/fotos já preparados
- decidir_negocio         -> analista avisa os clientes fora do prazo, se o prazo for estendido

As mensagens que saem depois de uma decisão humana são textos fixos, validados pelo mesmo validador das respostas.
"""
from __future__ import annotations

from typing import Any, Callable

from . import llm
from .acoes import eh_publica
from .ferramentas import CATALOGO, Ambiente

SUPERVISOR_PADRAO = "supervisor_01"
ATOR_ATENDENTE = "agente_ia"
ATOR_ANALISTA = "agente_analista"

NOME_ACAO = {"reembolsar": "o reembolso", "reenviar_produto": "o reenvio", "trocar_produto": "a troca",
             "reimprimir_personalizacao": "a nova produção"}
TEXTO_APROVADO = {
    "reembolsar": "Boa notícia: o reembolso de R$ {valor} foi aprovado e já solicitado. O valor volta pelo mesmo meio "
                  "de pagamento da compra. Qualquer dúvida, estamos à disposição.",
    "reenviar_produto": "Boa notícia: o reenvio do seu pedido foi aprovado e já está sendo preparado, sem custo para "
                        "você. Qualquer dúvida, estamos à disposição.",
    "reimprimir_personalizacao": "Boa notícia: a nova produção da sua personalização foi aprovada e já está em "
                                 "andamento, sem custo para você. Qualquer dúvida, estamos à disposição.",
    "trocar_produto": "Boa notícia: a troca foi aprovada e já está em andamento. Qualquer dúvida, estamos à disposição.",
}
TEXTO_REJEITADO = ("Analisamos sua solicitação com cuidado junto à nossa supervisão e, neste caso, não foi possível "
                   "seguir com {acao}. Se quiser, responda esta mensagem que seguimos conversando.")


def _cadeiras(amb: Ambiente, supervisor: str) -> tuple[Ambiente, Ambiente, Ambiente]:
    return (amb.como("supervisor", supervisor), amb.como("atendente", ATOR_ATENDENTE),
            amb.como("analista", ATOR_ANALISTA))


def _aberta(amb: Ambiente, pendencia_id: str, tipo: str) -> dict[str, Any]:
    p = amb.estado.pendencia(pendencia_id)
    if p is None:
        raise ValueError(f"pendência inexistente: {pendencia_id}")
    if p["tipo"] != tipo:
        raise ValueError(f"{pendencia_id} é do tipo {p['tipo']}, não {tipo}")
    if p["status"] != "aberta":
        raise ValueError(f"{pendencia_id} já está {p['status']}")
    return p


def _publicar(atend: Ambiente, conversation_id: str, texto: str, trilha: dict[str, Any],
              permitidas: set[str] = frozenset(), valores: set[float] = frozenset()) -> dict[str, Any]:
    """Toda mensagem depois de decisão humana passa pelo validador (inclusive texto editado pela pessoa)."""
    v = llm.validar_resposta(texto, publica=eh_publica(conversation_id), ferramentas_permitidas=set(permitidas),
                             valores_permitidos=set(valores), fatos=[texto])  # prazos citados vêm da própria decisão
    if not v.ok:
        return {"ok": False, "erro": "texto reprovado pelo validador: " + "; ".join(v.motivos)}
    with atend.atendendo(conversation_id):
        return atend.responder_cliente(conversation_id=conversation_id, texto=texto, trilha=trilha)


# ---------------------------------------------------------------------------
# Aprovação
# ---------------------------------------------------------------------------

def aprovar(amb: Ambiente, pendencia_id: str, supervisor: str = SUPERVISOR_PADRAO,
            comentario: str | None = None) -> dict[str, Any]:
    p = _aberta(amb, pendencia_id, "aprovacao")
    sup, atend, _ = _cadeiras(amb, supervisor)
    d = p["dados"]
    r = sup.aprovar_acao(aprovacao_id=d["aprovacao_id"], comentario=comentario,
                         trilha={"por_que": f"{supervisor} aprovou: {comentario or p['resumo']}", "origem": "humano"})
    if not r.get("ok"):
        return r
    trilha = {"por_que": f"retomada após aprovação {d['aprovacao_id']} por {supervisor}",
              "evidencias": [p["resumo"]], "origem": "regra (retomada após decisão humana)"}
    with atend.atendendo(p["conversation_id"]):
        ex = atend.chamar(d["acao"], trilha=trilha, **d["params"], aprovacao_id=d["aprovacao_id"])
    msg = None
    if ex.get("ok") and d["acao"] in TEXTO_APROVADO:
        valor = f"{d['valor']:.2f}".replace(".", ",")
        msg = _publicar(atend, p["conversation_id"], TEXTO_APROVADO[d["acao"]].format(valor=valor), trilha,
                        permitidas={d["acao"]}, valores={d["valor"]})
    amb.estado.resolver_pendencia(pendencia_id, supervisor, f"aprovada; {d['acao']} "
                                  + (f"executado ({ex.get('acao_id')})" if ex.get("ok") else f"falhou: {ex.get('erro')}"))
    amb.estado.registrar_evento("retomada", f"{p['conversation_id']}: {d['acao']} executado após aprovação de {supervisor}",
                                pendencia_id=pendencia_id)
    return {"ok": bool(ex.get("ok")), "execucao": ex, "mensagem": msg}


def rejeitar(amb: Ambiente, pendencia_id: str, justificativa: str, supervisor: str = SUPERVISOR_PADRAO) -> dict[str, Any]:
    p = _aberta(amb, pendencia_id, "aprovacao")
    sup, atend, _ = _cadeiras(amb, supervisor)
    d = p["dados"]
    r = sup.rejeitar_acao(aprovacao_id=d["aprovacao_id"], justificativa=justificativa,
                          trilha={"por_que": f"{supervisor} rejeitou: {justificativa}", "origem": "humano"})
    if not r.get("ok"):
        return r
    # texto fixo que NEGA a ação: a palavra dela é permitida (o validador por palavra-chave a leria como promessa)
    msg = _publicar(atend, p["conversation_id"], TEXTO_REJEITADO.format(acao=NOME_ACAO.get(d["acao"], "o pedido")),
                    {"por_que": f"retorno ao cliente após rejeição de {supervisor}", "origem": "regra (retomada)"},
                    permitidas={d["acao"]})
    amb.estado.resolver_pendencia(pendencia_id, supervisor, f"rejeitada: {justificativa}")
    return {"ok": True, "mensagem": msg}


# ---------------------------------------------------------------------------
# Suspeita
# ---------------------------------------------------------------------------

def decidir_suspeita(amb: Ambiente, pendencia_id: str, procedente: bool, justificativa: str,
                     supervisor: str = SUPERVISOR_PADRAO, redigir: Callable | None = None,
                     julgar: Callable | None = None) -> dict[str, Any]:
    p = _aberta(amb, pendencia_id, "suspeita")
    sup, atend, _ = _cadeiras(amb, supervisor)
    r = sup.decidir_suspeita(conversation_id=p["conversation_id"], decisao="procedente" if procedente else "improcedente",
                             justificativa=justificativa,
                             trilha={"por_que": f"{supervisor}: {justificativa}", "origem": "humano"})
    if not r.get("ok"):
        return r
    reprocesso = None
    if procedente:  # o atendente retoma o ticket; a decisão não sinaliza de novo (decisao.py)
        from . import agente
        reprocesso = agente.processar(atend, p["dados"]["_ticket"], redigir, julgar)
    amb.estado.resolver_pendencia(pendencia_id, supervisor,
                                  ("procedente: ticket retomado" if procedente else "improcedente: resposta neutra enviada")
                                  + f" — {justificativa}")
    return {"ok": True, "reprocesso": reprocesso}


# ---------------------------------------------------------------------------
# Publicação retida
# ---------------------------------------------------------------------------

def liberar(amb: Ambiente, pendencia_id: str, texto: str | None = None,
            supervisor: str = SUPERVISOR_PADRAO) -> dict[str, Any]:
    """Libera a resposta retida: executa as mensagens de ferramenta retidas e publica o texto (original ou editado)."""
    p = _aberta(amb, pendencia_id, "publicacao")
    _, atend, _ = _cadeiras(amb, supervisor)
    d, cid = p["dados"], p["conversation_id"]
    final = texto if texto is not None else d.get("texto")
    if not final and not d.get("retidos"):
        return {"ok": False, "erro": "não há rascunho: escreva o texto para publicar"}
    trilha = {"por_que": f"{supervisor} liberou a publicação" + (" com edição" if texto is not None else ""),
              "evidencias": d.get("motivos", []), "origem": "humano"}
    feitos = []
    with atend.atendendo(cid):
        for ret in d.get("retidos", []):
            res = atend.chamar(ret["ferramenta"], trilha=trilha, **ret["params"])
            feitos.append({"ferramenta": ret["ferramenta"], "ok": res.get("ok"), "erro": res.get("erro")})
    msg = None
    if final:
        ja_feitas = {a["acao"] for a in amb.estado.acoes if a["conversation_id"] == cid}
        msg = _publicar(atend, cid, final, trilha, permitidas=ja_feitas)
        if not msg.get("ok"):
            return {"ok": False, "erro": msg["erro"], "retidos_executados": feitos}
    amb.estado.resolver_pendencia(pendencia_id, supervisor, "liberada" + (" com edição" if texto is not None else ""))
    return {"ok": True, "retidos_executados": feitos, "mensagem": msg}


def descartar(amb: Ambiente, pendencia_id: str, motivo: str, supervisor: str = SUPERVISOR_PADRAO) -> dict[str, Any]:
    p = amb.estado.pendencia(pendencia_id)
    if p is None or p["status"] != "aberta":
        return {"ok": False, "erro": f"pendência {pendencia_id} não está aberta"}
    amb.estado.resolver_pendencia(pendencia_id, supervisor, f"descartada: {motivo}", status="descartada")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Cosmetovigilância e decisão de negócio
# ---------------------------------------------------------------------------

def registrar_cosmetovigilancia(amb: Ambiente, pendencia_id: str, supervisor: str = SUPERVISOR_PADRAO,
                                descricao: str | None = None) -> dict[str, Any]:
    p = _aberta(amb, pendencia_id, "cosmetovigilancia")
    sup, _, _ = _cadeiras(amb, supervisor)
    d = p["dados"]
    params = {k: v for k, v in {"conversation_id": p["conversation_id"], "sku": d["sku"], "lote": d.get("lote"),
                                "descricao": descricao or d["descricao"], "fotos": d.get("fotos")}.items()
              if v and k in CATALOGO["registrar_cosmetovigilancia"].parametros}
    r = sup.registrar_cosmetovigilancia(trilha={"por_que": f"{supervisor} registrou a reação adversa",
                                                "origem": "humano"}, **params)
    if r.get("ok"):
        amb.estado.resolver_pendencia(pendencia_id, supervisor,
                                      f"registrado {r['registro_id']} ({'no prazo' if r['dentro_do_prazo'] else 'FORA do prazo'})")
    return r


def decidir_negocio(amb: Ambiente, pendencia_id: str, estender: bool, justificativa: str,
                    supervisor: str = SUPERVISOR_PADRAO) -> dict[str, Any]:
    p = _aberta(amb, pendencia_id, "decisao_negocio")
    _, _, an = _cadeiras(amb, supervisor)
    d = p["dados"]
    r: dict[str, Any] = {"ok": True}
    if estender:
        r = an.notificar_clientes(order_ids=d["order_ids"], tipo_aviso=d["tipo_aviso"], texto=d["texto_aviso"],
                                  trilha={"por_que": f"{supervisor} estendeu o prazo de reposição: {justificativa}",
                                          "origem": "humano (decisão de negócio) + analista"})
    amb.estado.resolver_pendencia(pendencia_id, supervisor,
                                  (f"prazo estendido; {r.get('enviados', 0)} avisados" if estender else "prazo mantido")
                                  + f" — {justificativa}")
    return r

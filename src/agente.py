"""O agente: lê a fila, prioriza, e para cada ticket classifica -> investiga -> política -> decisão -> age -> responde.

Uso:
    .\\venv\\Scripts\\python.exe -m src.agente                       # Reclame Aqui, com IA
    .\\venv\\Scripts\\python.exe -m src.agente --limite 5            # só os 5 mais prioritários
    .\\venv\\Scripts\\python.exe -m src.agente --canais reclame_aqui email
    .\\venv\\Scripts\\python.exe -m src.agente --sem-ia               # sem proxy: tudo vai para revisão humana

Saída em `saida/<execução>/`: dossiê Markdown por ticket, resumo.md e a exportação do Estado (CSV + log + Slack).

Regras de execução (D-019):
- Quem escolhe as ferramentas é o código (decisao.py). A IA classifica (Jev) e redige (glm-5.3). Nada mais.
- Ferramentas de mensagem (solicitar_foto, informar_status, negar_por_politica, orientar_suspensao_uso, agradecer)
  usam o texto fixo delas: seguro, mascarado e auditável. O LLM escreve quando a decisão pede texto livre ou quando
  nenhuma mensagem saiu (ex.: reenvio executado, aprovação pedida, caso escalado).
- RETER (não publicar; rascunho vai ao canal do time) quando: caso sensível, ameaça externa, suspeita sinalizada,
  confiança do Jev < 0,7, validador reprovou, ou a IA falhou (CLAUDE.md, Guardrails item 4).
- Passos que dependem de dado que o código não tem (aprovação pendente, novo endereço, código de cupom) não são
  executados: viram "pendente" no dossiê e, se preciso, mão levantada.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from . import dados, llm
from .decisao import Decisao, decidir, valor_do_cliente
from .ferramentas import CATALOGO, Ambiente, mascarar_pii, mascarar_publico, pedido_na_posicao
from .politicas import CAMPANHA, POL, Passo, avaliar, coletar_fatos, texto_regra
from .veracidade import avaliar_veracidade

FERRAMENTAS_DE_MENSAGEM = {"solicitar_foto", "informar_status", "negar_por_politica", "orientar_suspensao_uso",
                           "agradecer", "responder_cliente"}
SLA_HORAS = {"whatsapp": POL["sla_primeira_resposta"]["whatsapp.minutos"] / 60,
             "email": float(POL["sla_primeira_resposta"]["email.horas"]),
             "reclame_aqui": float(POL["sla_primeira_resposta"]["reclame_aqui.horas"])}
CUSTO_CANAL = {k.split(".")[0]: float(v) for k, v in POL["custo_por_conversa_rs"].items()}
# Ações sem dinheiro e reversíveis: se o agente errar a categoria, o custo é baixo (D-020).
ACOES_SEM_RISCO = {"informar_status", "solicitar_foto", "abrir_chamado_transportadora", "responder_duvida",
                   "agradecer", "validar_cupom", "responder_cliente"}
LIMIAR_CATEGORIA_CANDIDATA = 0.15
MENSAGEM_REESCRITA = ("Seu texto foi reprovado pela {motivo}. Reescreva corrigindo isso e mantendo as demais regras. "
                      "Responda só com o JSON.")
TENTATIVAS_REDACAO = 3  # 1ª redação + até 2 reescritas com o motivo da reprovação (validador ou juiz)
REPOSICAO_POR_CATEGORIA = {"erro_personalizacao": "reimprimir_personalizacao", "avaria_vazamento": "reenviar_produto",
                           "item_faltante": "reenviar_produto"}

# Pesos da prioridade (explícitos; D-019). Pontos maiores = atender antes.
PESOS = {"sla": 40.0, "sla_vencido": 50.0, "custo_canal": 10.0, "ltv": 10.0, "novo": 8.0, "irritacao": 5.0,
         "ameaca": 10.0, "pendente": 6.0, "sensivel": 30.0, "replicas": 4.0}


# ---------------------------------------------------------------------------
# Priorização
# ---------------------------------------------------------------------------

@dataclass
class Ticket:
    conversation_id: str
    customer_id: str
    order_id: str | None
    canal: str
    marca: str
    empresa: str
    status: str
    referencia_sla: datetime
    horas_restantes: float
    classificacao: llm.Classificacao | None = None
    prioridade: float = 0.0
    componentes: dict[str, float] = field(default_factory=dict)
    ltv: float = 0.0
    novo: bool = False
    replicas: int = 0


def carregar_tickets(amb: Ambiente, canais: list[str]) -> list[Ticket]:
    fila = dados.tabela("fila_aberta")
    fila = fila[fila["canal"].isin(canais)]
    ra = dados.tabela("reclame_aqui").set_index("conversation_id")
    msgs = dados.tabela("historico_mensagens")
    agora = pd.Timestamp(amb.agora)
    tickets = []
    for _, t in fila.iterrows():
        cid = t["conversation_id"]
        # SLA: Reclame Aqui conta da publicação; cliente que voltou (pendente) conta da última mensagem dele.
        ref = ra.loc[cid, "data_publicacao"] if cid in ra.index else t["data_abertura"]
        if t["status"] == "pendente":
            dele = msgs[(msgs["conversation_id"] == cid) & (msgs["autor"] == "cliente") & (msgs["timestamp"] <= agora)]
            if not dele.empty:
                ref = dele["timestamp"].max()
        ref = pd.Timestamp(ref)
        restantes = (ref + pd.Timedelta(hours=SLA_HORAS[t["canal"]]) - agora).total_seconds() / 3600
        tickets.append(Ticket(
            conversation_id=cid, customer_id=t["customer_id"], order_id=t["order_id"] if pd.notna(t["order_id"]) else None,
            canal=t["canal"], marca=t["marca"], status=t["status"],
            empresa=ra.loc[cid, "empresa"] if cid in ra.index else t["marca"],
            referencia_sla=ref.to_pydatetime(), horas_restantes=round(restantes, 1),
            replicas=int(ra.loc[cid, "replicas_consumidor"]) if cid in ra.index
            and pd.notna(ra.loc[cid, "replicas_consumidor"]) else 0))
    return tickets


PRAZO_COSMETOVIGILANCIA_H = float(POL["categorias"]["reacao_adversa.prazo_registro_horas"])


def _prazo_regulatorio(amb: Ambiente, t: Ticket) -> float | None:
    """Caso sensível: horas que restam do prazo de cosmetovigilância (24h desde o 1º relato do cliente)."""
    msgs = dados.tabela("historico_mensagens")
    agora = pd.Timestamp(amb.agora)
    dele = msgs[(msgs["conversation_id"] == t.conversation_id) & (msgs["autor"] == "cliente")
                & (msgs["timestamp"] <= agora)]
    if dele.empty:
        return None
    return round(PRAZO_COSMETOVIGILANCIA_H - (agora - dele["timestamp"].min()).total_seconds() / 3600, 1)


def priorizar(amb: Ambiente, tickets: list[Ticket]) -> list[Ticket]:
    for t in tickets:
        cli = amb.consultar_cliente(t.customer_id)
        if cli["ok"]:
            vc = valor_do_cliente(cli["cliente"])
            t.ltv, t.novo = vc.ltv, vc.novo
        c = t.classificacao
        sla_total = SLA_HORAS[t.canal]
        if c and (c.caso_sensivel or 0) > 0.5:
            regulatorio = _prazo_regulatorio(amb, t)
            if regulatorio is not None and regulatorio < t.horas_restantes:  # vale o prazo mais apertado
                t.horas_restantes, sla_total = regulatorio, PRAZO_COSMETOVIGILANCIA_H
        comp = {
            "sla": PESOS["sla_vencido"] if t.horas_restantes <= 0
                   else PESOS["sla"] * max(0.0, 1 - t.horas_restantes / sla_total),
            "custo_canal": PESOS["custo_canal"] * CUSTO_CANAL[t.canal] / max(CUSTO_CANAL.values()),
            "ltv": PESOS["ltv"] * min(t.ltv / 1000, 1.0),
            "novo": PESOS["novo"] if t.novo else 0.0,
            "irritacao": PESOS["irritacao"] * ((c.irritacao or 0) / 2 if c else 0),
            "ameaca": PESOS["ameaca"] * ((c.ameaca_externa or 0) if c else 0),
            "pendente": PESOS["pendente"] if t.status == "pendente" else 0.0,
            "sensivel": PESOS["sensivel"] * ((c.caso_sensivel or 0) if c else 0),
            "replicas": PESOS["replicas"] * t.replicas,
        }
        t.componentes = {k: round(v, 1) for k, v in comp.items() if v}
        t.prioridade = round(sum(comp.values()), 1)
    return sorted(tickets, key=lambda t: -t.prioridade)


# ---------------------------------------------------------------------------
# Processamento de um ticket
# ---------------------------------------------------------------------------

@dataclass
class Resultado:
    ticket: Ticket
    decisao: Decisao | None = None
    executados: list[dict[str, Any]] = field(default_factory=list)   # {ferramenta, params, ok, erro, acao_id}
    pendentes: list[str] = field(default_factory=list)
    resposta: str | None = None
    resposta_origem: str | None = None    # ferramenta | llm | rascunho
    publicada: bool = False
    motivos_retencao: list[str] = field(default_factory=list)
    validacao: llm.Validacao | None = None
    redacao: llm.Redacao | None = None
    tentativas: list[str] = field(default_factory=list)   # motivos das redações reprovadas antes da final
    confianca_baixa: str | None = None                    # análise do D-020 quando o Jev ficou em dúvida
    julgamento: llm.Julgamento | None = None              # último parecer do juiz (D-022)
    padroes: list[str] = field(default_factory=list)      # o que o analista fez quando o atendente o chamou (D-026)
    erro: str | None = None


def _sku_brinde(pedido: dict[str, Any] | None, itens: list[dict[str, Any]]) -> str | None:
    linha = next((i for i in itens if i.get("tipo_item") == "brinde"), None)
    if linha:
        return linha["sku"]
    prods = dados.tabela("produtos")
    achado = prods.loc[prods["nome"] == CAMPANHA["brinde"], "sku"]
    return achado.iloc[0] if not achado.empty else None


def _placeholder(v: Any) -> bool:
    return isinstance(v, str) and v.startswith("<") and v.endswith(">")


def _completar(passo: Passo, d: Decisao) -> Passo:
    """Preenche parâmetros que a decisão sabe e a política deixou abertos (contexto do acionar_pessoa)."""
    params = dict(passo.params)
    if passo.ferramenta == "acionar_pessoa":
        params.setdefault("contexto", f"{d.conversation_id}: {d.resumo} | " + "; ".join(d.avaliacao.fatos[:4]))
        params.setdefault("area", "supervisão")
        params.setdefault("assunto", f"Revisão — {d.conversation_id}")
    return Passo(passo.ferramenta, params, passo.nota, passo.opcional)


def _faltando(passo: Passo) -> list[str]:
    spec = CATALOGO[passo.ferramenta]
    return [p for p in spec.obrigatorios if p not in passo.params or passo.params[p] in (None, "")
            or _placeholder(passo.params[p])] + [k for k, v in passo.params.items() if _placeholder(v)]


def _kw_politica(c: llm.Classificacao, categoria: str, f) -> dict[str, Any]:
    if categoria == "item_faltante" and (c.falta_brinde or 0) > 0.5 and f.pedido:
        return {"sku_alvo": _sku_brinde(f.pedido, f.itens)}
    return {}


def confianca_baixa_sem_risco(amb: Ambiente, c: llm.Classificacao, f) -> tuple[bool, str]:
    """Jev em dúvida: se TODAS as categorias prováveis levam só a ações sem risco, seguir é seguro (D-020).

    Roda política + decisão para cada candidata (só leituras; nada é executado) e compara as ferramentas.
    """
    candidatas = [k for k, p in sorted(c.probabilidades.items(), key=lambda kv: -kv[1])
                  if p >= LIMIAR_CATEGORIA_CANDIDATA][:3] or [c.categoria]
    if "outros" in candidatas:
        return False, "uma categoria provável está fora da política"
    detalhes = []
    for cat in candidatas:
        av = avaliar(cat, f, **_kw_politica(c, cat, f))
        d = decidir(amb, f, av, avaliar_veracidade(amb, f, cat))
        ferramentas = {p.ferramenta for p in d.passos} | {p.ferramenta for alt in av.alternativas for p in alt}
        arriscadas = ferramentas - ACOES_SEM_RISCO
        detalhes.append(f"{cat} ({c.probabilidades.get(cat, 0):.2f}) -> {', '.join(sorted(ferramentas)) or 'nada'}")
        if arriscadas or d.requer_humano or av.situacao == "escalar":
            return False, (f"se a categoria certa for {cat}, a ação seria {', '.join(sorted(arriscadas)) or 'escalar'}"
                           " (custo de errar alto)")
    return True, "todas as categorias prováveis levam só a ações sem risco: " + " | ".join(detalhes)


def _motivos_para_reter(t: Ticket, d: Decisao | None, confianca_liberada: bool = False) -> list[str]:
    c, m = t.classificacao, []
    if c is None or c.erro:
        m.append("classificação indisponível")
    elif c.revisao_humana and not confianca_liberada:
        m.append(f"revisão da classificação ({c.motivo_revisao})")
    if c and (c.caso_sensivel or 0) > 0.5:
        m.append("caso sensível (saúde)")
    if c and (c.ameaca_externa or 0) > 0.5:
        m.append("ameaça externa (Procon/jurídico)")
    if d and d.caminho == "sinalizar_suspeita":
        m.append("suspeita sinalizada")
    if d and d.categoria == "reacao_adversa" and "caso sensível (saúde)" not in m:
        m.append("caso sensível (reação adversa)")
    return m


def _chamar_analista(amb: Ambiente, r: Resultado) -> None:
    """O atendente percebeu um caso repetível: passa ao analista, que confirma o padrão e trata o incidente (D-026).

    Orquestração interna entre os dois agentes (o catálogo não tem ferramenta de passagem); fica no dossiê.
    """
    from . import analista
    t, c = r.ticket, r.ticket.classificacao
    for tr in analista.chamado_pelo_atendente(amb, t.conversation_id, t.order_id, c.categoria if c else ""):
        inc = tr.candidato
        r.padroes.append(
            f"{tr.incidente_id} — {inc.titulo} ({inc.escopo})"
            + (": incidente já aberto" if tr.ja_existia else
               f": incidente aberto agora; {tr.notificados} clientes avisados antes de reclamar"
               + (f", {tr.chamados_abertos} chamados abertos" if tr.chamados_abertos else "")
               + (f", {len(tr.fora_do_prazo)} fora do prazo para decisão humana" if tr.fora_do_prazo else "")))


def processar(amb: Ambiente, t: Ticket, redigir: Callable[[list[dict[str, str]]], llm.Redacao] | None,
              julgar: Callable[[list[dict[str, str]]], llm.Julgamento] | None = None,
              com_analista: bool = False) -> Resultado:
    r = Resultado(t)
    c = t.classificacao
    with amb.atendendo(t.conversation_id):
        r.motivos_retencao = _motivos_para_reter(t, None)
        if c is None or c.categoria is None or c.categoria == "outros":
            return _escalar_sem_decisao(amb, r, redigir)

        amb.classificar_conversa(conversation_id=t.conversation_id, categoria=c.tag,
                                 trilha={"por_que": c.justificativa(), "origem": c.origem,
                                         "evidencias": [f"confiança {c.confianca:.2f}"]})
        f = coletar_fatos(amb, t.conversation_id, t.order_id)
        liberada, porque_liberada = False, None
        if c.revisao_humana and (c.motivo_revisao or "").startswith("confiança"):
            liberada, porque_liberada = confianca_baixa_sem_risco(amb, c, f)
            r.confianca_baixa = porque_liberada
        av = avaliar(c.categoria, f, **_kw_politica(c, c.categoria, f))
        d = decidir(amb, f, av, avaliar_veracidade(amb, f, c.categoria))
        r.decisao = d
        r.motivos_retencao = _motivos_para_reter(t, d, confianca_liberada=liberada)
        reter = bool(r.motivos_retencao)
        trilha = d.trilha()
        trilha["evidencias"] = [f"classificação: {c.justificativa()}"] + trilha["evidencias"]
        if liberada:
            trilha["evidencias"].insert(1, f"confiança baixa, seguiu automático: {porque_liberada}")

        mensagem_enviada = False
        passos_texto_livre: list[Passo] = []
        for passo in d.passos:
            passo = _completar(passo, d)
            if passo.opcional:
                r.pendentes.append(f"{passo.ferramenta} (opcional: {passo.nota})")
                continue
            if passo.ferramenta == "responder_cliente":
                passos_texto_livre.append(passo)
                continue
            if passo.ferramenta in FERRAMENTAS_DE_MENSAGEM and reter:
                r.pendentes.append(f"{passo.ferramenta} retido para revisão humana")
                continue
            falta = _faltando(passo)
            if falta:
                r.pendentes.append(f"{passo.ferramenta}: falta {', '.join(sorted(set(falta)))}"
                                   + (f" ({passo.nota})" if passo.nota else ""))
                continue
            res = amb.chamar(passo.ferramenta, trilha=trilha, **passo.params)
            r.executados.append({"ferramenta": passo.ferramenta, "params": passo.params, "ok": res.get("ok"),
                                 "erro": res.get("erro"), "acao_id": res.get("acao_id"), "texto": res.get("texto")})
            if res.get("ok") and passo.ferramenta in FERRAMENTAS_DE_MENSAGEM:
                mensagem_enviada = True
                r.resposta, r.resposta_origem, r.publicada = res.get("texto"), "ferramenta", True

        if com_analista:
            _chamar_analista(amb, r)
        if mensagem_enviada and not passos_texto_livre:
            return r
        _responder_com_llm(amb, r, d, redigir, reter, trilha, passos_texto_livre, julgar)
    return r


def _acoes_para_prompt(r: Resultado, d: Decisao) -> list[str]:
    acoes = [f"{e['ferramenta']} executado" for e in r.executados if e["ok"]]
    acoes += [f"pendente: {p}" for p in r.pendentes]
    acoes += [f"{p.ferramenta}: {p.nota}" for p in d.passos if p.ferramenta == "responder_cliente" and p.nota]
    for alt in d.avaliacao.alternativas:
        acoes.append("opção ao cliente: " + " + ".join(p.ferramenta for p in alt if p.ferramenta != "solicitar_aprovacao"))
    return acoes


def _responder_com_llm(amb: Ambiente, r: Resultado, d: Decisao, redigir, reter: bool, trilha: dict[str, Any],
                       passos_texto: list[Passo], julgar=None) -> None:
    t = r.ticket
    publica = t.canal in {"reclame_aqui"}
    pedido = pedido_na_posicao(amb, t.order_id) if t.order_id else None
    conversa = amb.ler_conversa(t.conversation_id)
    texto_cliente = "\n".join(m["texto"] or "" for m in conversa["mensagens"] if m["autor"] == "cliente")
    if redigir is None:
        r.motivos_retencao.append("IA desligada (--sem-ia)")
        r.pendentes.append("resposta ao cliente: redigir manualmente")
        _levantar_mao(amb, r, None)
        return
    fatos = list(d.avaliacao.fatos)
    if pedido and pedido.get("transportadora"):  # sem isso o glm inventa "Correios" (D-019)
        fatos.append(f"transportadora do pedido: {pedido['transportadora']}")
    msgs = llm.montar_prompt_resposta(empresa=t.empresa, publica=publica, intencao=d.intencao_resposta,
                                      acoes=_acoes_para_prompt(r, d), fatos=fatos,
                                      mensagem_cliente=texto_cliente)
    permitidas = {e["ferramenta"] for e in r.executados if e["ok"]} | {
        p.ferramenta for alt in d.avaliacao.alternativas for p in alt}
    if d.avaliacao.situacao == "em_andamento":  # pode citar a reposição que JÁ existe (D-016)
        permitidas.add(REPOSICAO_POR_CATEGORIA.get(d.categoria, ""))
    pendentes = {p.ferramenta for p in d.passos if any(_placeholder(v) for v in p.params.values())}
    valores = {d.avaliacao.valor_acao} if d.avaliacao.valor_acao else set()
    acoes_prompt = _acoes_para_prompt(r, d)
    jul: llm.Julgamento | None = None
    for tentativa in range(1, TENTATIVAS_REDACAO + 1):
        red = redigir(msgs)
        r.redacao = red
        # 1) regras duras, determinísticas
        r.validacao = llm.validar_resposta(red.texto, publica=publica, ferramentas_permitidas=permitidas,
                                           ferramentas_pendentes=pendentes,
                                           valores_permitidos=valores,
                                           permite_cupom=d.categoria == "cupom_promocao",
                                           transportadora=(pedido or {}).get("transportadora"),
                                           # prazos aceitos: os dos fatos do caso e os da regra da política (D-021)
                                           fatos=fatos + [texto_regra(d.categoria)])
        # 2) qualidade, por um juiz de outra família (só para texto que passou nas regras) (D-022)
        jul = None
        if not red.erro and r.validacao.ok and julgar is not None:
            jul = julgar(llm.montar_prompt_juiz(texto=red.texto, empresa=t.empresa, publica=publica,
                                                intencao=d.intencao_resposta, acoes=acoes_prompt, fatos=fatos,
                                                mensagem_cliente=texto_cliente))
            r.julgamento = jul
        juiz_ok = jul is None or jul.aprovado or bool(jul.erro)  # juiz fora do ar: vale o validador (D-022)
        aprovado = not red.erro and r.validacao.ok and juiz_ok
        ultima = bool(red.erro) or aprovado or tentativa == TENTATIVAS_REDACAO
        _registrar_validacao(amb, r, tentativa, red, jul, "aprovado" if aprovado
                             else "reprovado_final" if ultima else "reescrever")
        if ultima:
            break
        # 3) autocorreção (D-020): o redator reescreve sabendo exatamente o que foi reprovado e por quem
        motivo = ("revisão automática: " + "; ".join(r.validacao.motivos)) if not r.validacao.ok \
            else f"revisor de qualidade: {jul.feedback()}"
        r.tentativas.append(motivo)
        msgs = msgs + [{"role": "assistant", "content": red.texto or ""},
                       {"role": "user", "content": MENSAGEM_REESCRITA.format(motivo=motivo)}]
    if red.erro:
        r.motivos_retencao.append(f"redação falhou ({red.erro})")
    elif not r.validacao.ok:
        r.motivos_retencao.append("validador reprovou: " + "; ".join(r.validacao.motivos))
    elif jul and not jul.aprovado and not jul.erro:
        r.motivos_retencao.append(f"juiz reprovou ({jul.resumo()}): {jul.feedback()}")
    r.resposta = red.texto
    if r.motivos_retencao:
        r.resposta_origem = "rascunho"
        _levantar_mao(amb, r, red.texto)
        return
    origem = f"llm {red.modelo} ({red.versao_prompt}) + validador"
    if jul:
        origem += f" + juiz {jul.modelo} ({jul.versao_prompt})"
    evid = list(trilha["evidencias"]) + ([f"juiz: {jul.resumo()}"] if jul else [])
    res = amb.responder_cliente(conversation_id=t.conversation_id, texto=red.texto,
                                trilha={**trilha, "por_que": f"{trilha['por_que']} | texto: {red.justificativa}",
                                        "evidencias": evid, "origem": origem})
    r.executados.append({"ferramenta": "responder_cliente", "params": {}, "ok": res.get("ok"), "erro": res.get("erro"),
                         "acao_id": res.get("acao_id"), "texto": res.get("texto")})
    r.publicada, r.resposta_origem = bool(res.get("ok")), "llm"
    r.resposta = res.get("texto") or red.texto


def _registrar_validacao(amb: Ambiente, r: Resultado, tentativa: int, red: llm.Redacao, jul: llm.Julgamento | None,
                         resultado: str) -> None:
    """Uma linha por tentativa de texto: o que o redator escreveu, o que o validador e o juiz acharam e por quê."""
    t, v = r.ticket, r.validacao
    amb.estado.validacoes.append({
        "conversation_id": t.conversation_id, "tique": amb.tique, "data_hora": amb.estado.agora_txt(),
        "tentativa": tentativa, "resultado": resultado, "publica": t.canal == "reclame_aqui",
        "redator_modelo": red.modelo, "redator_prompt": red.versao_prompt, "redator_justificativa": red.justificativa,
        "redator_erro": red.erro, "texto": red.texto,
        "validador_ok": v.ok if v else None, "validador_motivos": v.motivos if v else None,
        "juiz_modelo": jul.modelo if jul else None, "juiz_prompt": jul.versao_prompt if jul else None,
        "juiz_aprovado": jul.aprovado if jul else None, "juiz_notas": jul.notas if jul else None,
        "juiz_reprovados": jul.reprovados if jul else None, "juiz_justificativas": jul.justificativas if jul else None,
        "juiz_problemas": jul.problemas if jul else None, "juiz_sugestao": jul.sugestao if jul else None,
        "juiz_erro": jul.erro if jul else None,
    })


def _levantar_mao(amb: Ambiente, r: Resultado, rascunho: str | None) -> None:
    t = r.ticket
    filtro = mascarar_publico if t.canal == "reclame_aqui" else mascarar_pii
    urgente = any("sensível" in m or "ameaça" in m for m in r.motivos_retencao) or t.horas_restantes < 6
    contexto = (f"Motivo: {'; '.join(r.motivos_retencao)}. "
                + (f"Decisão: {r.decisao.resumo} " if r.decisao else "")
                + (f"Rascunho para aprovar: «{filtro(rascunho)}»" if rascunho else "Sem rascunho."))
    res = amb.acionar_pessoa(area="supervisão", assunto=f"Revisar antes de publicar — {t.conversation_id} ({t.canal})",
                             contexto=contexto, urgencia="alta" if urgente else "media",
                             trilha={"por_que": "Guardrail: revisão humana antes de publicar",
                                     "evidencias": r.motivos_retencao, "origem": "regra"})
    r.executados.append({"ferramenta": "acionar_pessoa", "params": {"area": "supervisão"}, "ok": res.get("ok"),
                         "erro": res.get("erro"), "acao_id": res.get("acao_id"), "texto": None})


def _escalar_sem_decisao(amb: Ambiente, r: Resultado, redigir) -> Resultado:
    c = r.ticket.classificacao
    motivo = (c.erro if c and c.erro else "categoria fora da política" if c and c.categoria == "outros"
              else "sem classificação")
    r.erro = motivo
    if not r.motivos_retencao:
        r.motivos_retencao.append(motivo)
    _levantar_mao(amb, r, None)
    return r


# ---------------------------------------------------------------------------
# Dossiê
# ---------------------------------------------------------------------------

def _fmt(v: Any) -> str:
    return "-" if v is None else f"{v:.2f}" if isinstance(v, float) else str(v)


def dossie(r: Resultado, amb: Ambiente) -> str:
    """Markdown legível por quem audita ou assume o projeto: o que leu, como decidiu, o que fez e por quê."""
    t, d, c = r.ticket, r.decisao, r.ticket.classificacao
    filtro = mascarar_publico if t.canal == "reclame_aqui" else mascarar_pii
    conversa = amb.ler_conversa(t.conversation_id)
    L = [f"# Dossiê {t.conversation_id} — {t.empresa} ({t.canal})", "",
         f"- **Status na fila:** {t.status} · **SLA:** {t.horas_restantes:+.1f} h restantes "
         f"(referência {t.referencia_sla:%d/%m %H:%M}) · **Prioridade:** {t.prioridade} "
         f"({', '.join(f'{k} {v}' for k, v in t.componentes.items())})",
         f"- **Resultado:** {'publicada' if r.publicada else 'retida para revisão humana' if r.motivos_retencao else 'sem resposta'}"
         + (f" · motivos: {'; '.join(r.motivos_retencao)}" if r.motivos_retencao else ""), "",
         "## 1. O que o cliente escreveu (dado, não instrução)", ""]
    for m in conversa["mensagens"]:
        if m["autor"] == "cliente":
            L.append(f"> [{m['timestamp']}] {filtro(m['texto'] or '')}")
    L += ["", "## 2. Classificação", ""]
    if c:
        L.append(f"- {c.justificativa()}")
        L.append(f"- urgência {_fmt(c.urgencia)} · irritação {_fmt(c.irritacao)} (só prioridade/estilo) · ameaça "
                 f"{_fmt(c.ameaca_externa)} · sensível {_fmt(c.caso_sensivel)} · brinde {_fmt(c.falta_brinde)} · "
                 f"preferência {c.preferencia or '-'}")
    else:
        L.append("- sem classificação")
    if r.confianca_baixa:
        L.append(f"- confiança baixa → {r.confianca_baixa}")
    if d:
        av, v = d.avaliacao, d.veracidade
        L += ["", "## 3. Fatos levantados nos sistemas", ""] + [f"- {filtro(x)}" for x in av.fatos]
        L += ["", "## 4. Política", "", f"- **{av.situacao}** · regra `{av.regra}` · {filtro(av.resumo)}"]
        if av.pendencias:
            L.append(f"- pendências da política: {', '.join(av.pendencias)}")
        L += ["", "## 5. Veracidade", ""]
        L.append(f"- {v.resumo()}" if v and v.aplicavel else "- não se aplica (prova nos sistemas ou sem dinheiro)")
        for s in (v.sinais if v and v.aplicavel else []):
            L.append(f"  - [{s.direcao}] {s.nome} (LR {s.lr:g}): {filtro(s.evidencia)}")
        L += ["", "## 6. Decisão", "", f"- **caminho:** {d.caminho} · {filtro(d.resumo)}"]
        if d.cliente:
            L.append(f"- cliente {d.cliente.segmento}: {d.cliente.criterio}")
        for m in d.motivos:
            L.append(f"- {filtro(m)}")
        if d.alternativas:
            L.append("- custo esperado: " + " · ".join(f"{k} R$ {v:.2f}" for k, v in d.alternativas.items()))
    L += ["", "## 7. Ações", ""]
    for e in r.executados:
        L.append(f"- {'✅' if e['ok'] else '❌'} `{e['ferramenta']}` {e.get('acao_id') or ''} {e.get('erro') or ''}")
    for p in r.pendentes:
        L.append(f"- ⏸️ {p}")
    L += ["", "## 8. Resposta ao cliente", ""]
    if r.resposta:
        L.append(f"- origem: {r.resposta_origem} · {'publicada' if r.publicada else 'NÃO publicada (rascunho)'}")
        for i, motivo in enumerate(r.tentativas, 1):
            L.append(f"- tentativa {i} reprovada e reescrita automaticamente: {motivo}")
        if r.julgamento:
            j = r.julgamento
            L.append(f"- juiz {j.modelo} ({j.versao_prompt}): {j.resumo()}")
            for crit, just in j.justificativas.items():
                L.append(f"  - {crit} {j.notas.get(crit)}: {just}")
            for prob in j.problemas:
                L.append(f"  - problema apontado: {prob}")
        if r.validacao:
            L.append(f"- validador: {'aprovado' if r.validacao.ok else 'reprovado: ' + '; '.join(r.validacao.motivos)}")
        if r.redacao and r.redacao.justificativa:
            L.append(f"- justificativa do modelo: {r.redacao.justificativa}")
        L += ["", f"> {filtro(r.resposta)}"]
    else:
        L.append("- nenhuma")
    if r.padroes:
        L += ["", "## 9. Padrão percebido → analista", ""] + [f"- {p}" for p in r.padroes]
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
# Execução
# ---------------------------------------------------------------------------

def rodar(canais: list[str] | None = None, limite: int | None = None, usar_ia: bool = True,
          pasta_saida: str | Path = "saida", amb: Ambiente | None = None,
          classificar: Callable | None = None, redigir: Callable | None = None,
          julgar: Callable | None = None, com_analista: bool = True) -> tuple[list[Resultado], Path]:
    """Processa a fila dos `canais` (padrão: só Reclame Aqui, escopo do MVP). Retorna resultados e a pasta de saída.

    Com o analista: o atendente o chama ao perceber um padrão e, no fim, ele varre por conta própria (D-026).
    """
    canais = canais or ["reclame_aqui"]
    amb = amb or Ambiente()
    if usar_ia:
        classificar = classificar or llm.classificar
        redigir = redigir or llm.redigir
        julgar = julgar or llm.julgar
    tickets = carregar_tickets(amb, canais)
    for t in tickets:
        conv = amb.ler_conversa(t.conversation_id)
        t.classificacao = classificar(conv, pedido_na_posicao(amb, t.order_id) if t.order_id else None) \
            if classificar else None
    tickets = priorizar(amb, tickets)[:limite]
    resultados = []
    for t in tickets:
        try:
            resultados.append(processar(amb, t, redigir, julgar, com_analista=com_analista))
        except Exception as e:  # falha segura: um ticket com erro não derruba a fila nem gera ação de dinheiro
            res = Resultado(t, erro=f"{type(e).__name__}: {e}"[:300], motivos_retencao=["erro interno"])
            with amb.atendendo(t.conversation_id):
                _levantar_mao(amb, res, None)
            resultados.append(res)
    relatorio = None
    if com_analista:  # por conta própria: o que nenhum ticket acionou
        from . import analista
        relatorio = analista.varrer(amb)
    pasta = Path(pasta_saida) / datetime.now().strftime("%Y%m%d-%H%M%S")
    (pasta / "dossies").mkdir(parents=True, exist_ok=True)
    for r in resultados:
        (pasta / "dossies" / f"{r.ticket.conversation_id}.md").write_text(dossie(r, amb), encoding="utf-8")
    amb.estado.exportar(pasta)
    if relatorio is not None:
        from . import analista
        (pasta / "analista.md").write_text(analista.resumo(relatorio), encoding="utf-8")
    (pasta / "resumo.md").write_text(resumo(resultados, canais, amb), encoding="utf-8")
    return resultados, pasta


def resumo(resultados: list[Resultado], canais: list[str], amb: Ambiente | None = None) -> str:
    L = [f"# Execução do agente — {', '.join(canais)}", "",
         f"{len(resultados)} tickets · {sum(r.publicada for r in resultados)} respondidos · "
         f"{sum(bool(r.motivos_retencao) for r in resultados)} retidos para revisão humana", ""]
    if amb is not None and amb.estado.incidentes:
        L += [f"Analista: {len(amb.estado.incidentes)} incidentes · {len(amb.estado.notificacoes)} clientes avisados "
              f"antes de reclamar · detalhes em [analista.md](analista.md)", ""]
        L += [f"- {r.ticket.conversation_id} percebeu o padrão → {p}" for r in resultados for p in r.padroes]
        L.append("")
    L += [f"Custo de IA: {llm.CUSTOS.resumo()}", "",
         "| # | Conversa | Prioridade | SLA (h) | Categoria | Caminho | Ações | Resposta |", "|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(resultados, 1):
        t = r.ticket
        acoes = ", ".join(e["ferramenta"] for e in r.executados if e["ok"]) or "-"
        resp = "publicada" if r.publicada else ("retida: " + "; ".join(r.motivos_retencao))[:80]
        L.append(f"| {i} | [{t.conversation_id}](dossies/{t.conversation_id}.md) | {t.prioridade} | "
                 f"{t.horas_restantes:+.1f} | {t.classificacao.categoria if t.classificacao else '-'} | "
                 f"{r.decisao.caminho if r.decisao else '-'} | {acoes} | {resp} |")
    return "\n".join(L) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="Agente de atendimento (Desafio 1)")
    ap.add_argument("--canais", nargs="+", default=["reclame_aqui"], choices=list(SLA_HORAS))
    ap.add_argument("--limite", type=int, default=None)
    ap.add_argument("--sem-ia", action="store_true", help="não chama o proxy; tudo vai para revisão humana")
    ap.add_argument("--saida", default="saida")
    ap.add_argument("--sem-analista", action="store_true", help="não aciona o agente analista (incidentes e avisos)")
    a = ap.parse_args()
    resultados, pasta = rodar(a.canais, a.limite, not a.sem_ia, a.saida, com_analista=not a.sem_analista)
    print(f"\n{len(resultados)} tickets · {sum(r.publicada for r in resultados)} respondidos · "
          f"{sum(bool(r.motivos_retencao) for r in resultados)} retidos · saída em {pasta}")
    print(f"Custo de IA: {llm.CUSTOS.resumo()}")


if __name__ == "__main__":
    main()

"""Interface da demo (Streamlit): a fila, o supervisor humano, o analista, o canal do time, a semana e a auditoria.

Rodar:  .\\venv\\Scripts\\streamlit.exe run app.py

O estado da execução fica na sessão do navegador (st.session_state). Quem usa a tela faz o papel do SUPERVISOR:
cada decisão chama src/supervisor.py, e os próprios agentes retomam o caso na hora (D-027).
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from src import agente, analista, dados, llm, semana, supervisor
from src.ferramentas import Ambiente

st.set_page_config(page_title="Agente de Atendimento — GoHacks", page_icon="🤖", layout="wide")

TIPOS = {"aprovacao": "💰 Aprovação", "suspeita": "🕵️ Suspeita", "publicacao": "📝 Publicação retida",
         "cosmetovigilancia": "🧪 Cosmetovigilância", "decisao_negocio": "🏢 Decisão de negócio"}


# ---------------------------------------------------------------------------
# Estado da sessão
# ---------------------------------------------------------------------------

def sessao() -> dict:
    s = st.session_state
    s.setdefault("amb", None)
    s.setdefault("resultados", [])
    s.setdefault("pasta", None)
    s.setdefault("usar_ia", True)
    s.setdefault("aviso", None)
    return s


def avisar(ok: bool, texto: str) -> None:
    st.session_state["aviso"] = ("success" if ok else "error", texto)


def mostrar_aviso() -> None:
    a = st.session_state.get("aviso")
    if a:
        getattr(st, a[0])(a[1])
        st.session_state["aviso"] = None


def restante(prazo: str | None, amb: Ambiente) -> str:
    if not prazo:
        return "sem prazo"
    h = (pd.Timestamp(prazo) - pd.Timestamp(amb.agora)).total_seconds() / 3600
    if h <= 0:
        return f"⛔ vencido há {abs(h):.0f}h"
    return f"⏳ {h:.0f}h restantes" if h >= 1 else f"⏳ {h * 60:.0f} min restantes"


# ---------------------------------------------------------------------------
# Barra lateral: rodar e números principais
# ---------------------------------------------------------------------------

def barra_lateral(s: dict) -> None:
    st.sidebar.title("🤖 Agente de Atendimento")
    st.sidebar.caption("Gocase / GoBeauté · GoHacks Talent 2026")
    canais = st.sidebar.multiselect("Canais", ["reclame_aqui", "email", "whatsapp"], default=["reclame_aqui"])
    limite = st.sidebar.number_input("Limite de tickets (0 = todos)", 0, 100, 0)
    s["usar_ia"] = st.sidebar.toggle("Usar IA (Jev + glm-5.3 + juiz)", value=s["usar_ia"],
                                     help="Sem IA, nada é classificado e tudo vai para revisão humana.")
    com_analista = st.sidebar.toggle("Agente analista", value=True)
    if st.sidebar.button("▶️ Rodar a fila", type="primary", width="stretch"):
        llm.CUSTOS.chamadas.clear()
        llm.CUSTOS.ao_registrar = None
        amb = Ambiente()
        with st.spinner("Classificando, decidindo, agindo e respondendo..."):
            resultados, pasta = agente.rodar(canais or ["reclame_aqui"], limite or None, s["usar_ia"], "saida",
                                             amb=amb, com_analista=com_analista)
        s.update(amb=amb, resultados=resultados, pasta=pasta)
        avisar(True, f"{len(resultados)} tickets processados · saída em {pasta}")
        st.rerun()

    amb = s["amb"]
    if amb is None:
        st.sidebar.info("Rode a fila para começar.")
        return
    st.sidebar.divider()
    st.sidebar.metric("🕐 Agora (ambiente)", amb.agora.strftime("%d/%m %H:%M"), f"tique {amb.tique}")
    res = s["resultados"]
    c1, c2 = st.sidebar.columns(2)
    c1.metric("Tickets", len(res))
    c2.metric("Respondidos", sum(r.publicada for r in res))
    c1.metric("Incidentes", len(amb.estado.incidentes))
    c2.metric("Avisados", len(amb.estado.notificacoes))
    st.sidebar.metric("🙋 Pendências abertas", len(amb.estado.pendencias_abertas()))
    cu = llm.CUSTOS.resumo()
    st.sidebar.caption(f"Custo de IA: Jev US$ {cu['jev_usd']:.5f} · chat {cu['chat_tokens']:,} tokens "
                       f"({cu['chat_chamadas']} chamadas, sem preço por chamada)".replace(",", "."))


# ---------------------------------------------------------------------------
# Abas
# ---------------------------------------------------------------------------

def aba_fila(s: dict) -> None:
    res, amb = s["resultados"], s["amb"]
    linhas = [{"#": i, "Conversa": r.ticket.conversation_id, "Canal": r.ticket.canal, "Marca": r.ticket.empresa,
               "Prioridade": r.ticket.prioridade, "SLA (h)": r.ticket.horas_restantes,
               "Categoria": r.ticket.classificacao.categoria if r.ticket.classificacao else "-",
               "Caminho": r.decisao.caminho if r.decisao else "-",
               "Ações": ", ".join(e["ferramenta"] for e in r.executados if e["ok"]) or "-",
               "Resposta": "✅ publicada" if r.publicada else "🙋 retida" if r.motivos_retencao else "-",
               "Padrão → analista": "🔎" if r.padroes else ""}
              for i, r in enumerate(res, 1)]
    st.dataframe(pd.DataFrame(linhas), hide_index=True, width="stretch")
    escolha = st.selectbox("Abrir o dossiê do ticket", [r.ticket.conversation_id for r in res])
    r = next(r for r in res if r.ticket.conversation_id == escolha)
    with st.container(border=True):
        st.markdown(agente.dossie(r, amb))


def _form_aprovacao(amb, p, k) -> None:
    d = p["dados"]
    st.write(f"**{d['acao']}** de **R$ {d['valor']:.2f}** · aprovação `{d['aprovacao_id']}`")
    texto = st.text_input("Comentário / justificativa", key=f"j{k}")
    c1, c2 = st.columns(2)
    if c1.button("✅ Aprovar", key=f"a{k}", type="primary"):
        r = supervisor.aprovar(amb, p["pendencia_id"], comentario=texto or None)
        avisar(r["ok"], f"Aprovado: {d['acao']} executado pelo atendente e cliente avisado." if r["ok"]
               else f"Falhou: {r.get('erro') or r.get('execucao', {}).get('erro')}")
        st.rerun()
    if c2.button("❌ Rejeitar", key=f"r{k}"):
        if not texto:
            st.warning("Informe a justificativa (interna) da rejeição.")
        else:
            r = supervisor.rejeitar(amb, p["pendencia_id"], texto)
            avisar(r["ok"], "Rejeitado: cliente recebeu resposta neutra.")
            st.rerun()


def _form_suspeita(amb, p, k, s) -> None:
    st.write(f"Sinais: {p['dados'].get('sinais') or '-'}")
    decisao = st.radio("Decisão", ["procedente", "improcedente"], horizontal=True, key=f"d{k}")
    texto = st.text_input("Justificativa", key=f"j{k}")
    if st.button("⚖️ Decidir", key=f"b{k}", type="primary", disabled=not texto):
        ia = s["usar_ia"]
        r = supervisor.decidir_suspeita(amb, p["pendencia_id"], decisao == "procedente", texto,
                                        redigir=llm.redigir if ia else None, julgar=llm.julgar if ia else None)
        avisar(r["ok"], "Procedente: o atendente retomou o ticket." if decisao == "procedente"
               else "Improcedente: resposta neutra enviada e conversa encerrada.")
        st.rerun()


def _form_publicacao(amb, p, k) -> None:
    d = p["dados"]
    st.write("Motivos da retenção: " + "; ".join(d.get("motivos") or []))
    for ret in d.get("retidos") or []:
        st.caption(f"Mensagem de ferramenta retida: `{ret['ferramenta']}` (será enviada ao liberar)")
    texto = st.text_area("Texto ao cliente (edite se quiser; passa pelo validador)", d.get("texto") or "",
                         key=f"t{k}", height=160)
    c1, c2 = st.columns(2)
    if c1.button("📤 Liberar", key=f"l{k}", type="primary"):
        editado = texto if texto != (d.get("texto") or "") else None
        r = supervisor.liberar(amb, p["pendencia_id"], texto=editado if texto else None)
        avisar(r["ok"], "Publicado pelo atendente." if r["ok"] else r["erro"])
        st.rerun()
    if c2.button("🗑️ Descartar", key=f"x{k}"):
        supervisor.descartar(amb, p["pendencia_id"], "descartado na interface")
        avisar(True, "Descartado: nada foi publicado.")
        st.rerun()


def _form_cosmeto(amb, p, k) -> None:
    d = p["dados"]
    st.write(f"SKU **{d['sku']}** · lote **{d.get('lote') or 'não informado'}** · fotos: {d.get('fotos') or 'nenhuma'}")
    texto = st.text_area("Descrição do relato", d["descricao"], key=f"t{k}", height=110)
    if st.button("🧪 Registrar cosmetovigilância", key=f"c{k}", type="primary"):
        r = supervisor.registrar_cosmetovigilancia(amb, p["pendencia_id"], descricao=texto)
        avisar(r["ok"], f"Registrado {r.get('registro_id')} — {'no prazo' if r.get('dentro_do_prazo') else 'FORA do prazo'}"
               + (f" · pendências: {', '.join(r['pendencias'])}" if r.get("pendencias") else "") if r["ok"] else r["erro"])
        st.rerun()


def _form_negocio(amb, p, k) -> None:
    d = p["dados"]
    st.write(f"{len(d['order_ids'])} clientes afetados já passaram do prazo de reposição. Aviso proposto:")
    st.info(d["texto_aviso"])
    texto = st.text_input("Justificativa", key=f"j{k}")
    c1, c2 = st.columns(2)
    if c1.button("✅ Estender prazo e avisar", key=f"e{k}", type="primary", disabled=not texto):
        r = supervisor.decidir_negocio(amb, p["pendencia_id"], True, texto)
        avisar(True, f"Prazo estendido: {r.get('enviados', 0)} clientes avisados pelo analista.")
        st.rerun()
    if c2.button("✋ Manter a política", key=f"m{k}", disabled=not texto):
        supervisor.decidir_negocio(amb, p["pendencia_id"], False, texto)
        avisar(True, "Política mantida.")
        st.rerun()


def aba_supervisor(s: dict) -> None:
    amb = s["amb"]
    abertas = sorted(amb.estado.pendencias_abertas(), key=lambda p: p["prazo"] or "9999")
    st.caption("Você é o supervisor. Cada decisão usa a ferramenta do catálogo com o seu papel, e o agente retoma o caso.")
    if not abertas:
        st.success("Nenhuma pendência aberta. 🎉")
    for i, p in enumerate(abertas):
        titulo = (f"{TIPOS.get(p['tipo'], p['tipo'])} · {p['conversation_id'] or '-'} · {restante(p['prazo'], amb)} · "
                  f"{p['resumo'][:90]}")
        with st.expander(titulo, expanded=i == 0):
            k = p["pendencia_id"]
            {"aprovacao": lambda: _form_aprovacao(amb, p, k), "suspeita": lambda: _form_suspeita(amb, p, k, s),
             "publicacao": lambda: _form_publicacao(amb, p, k), "cosmetovigilancia": lambda: _form_cosmeto(amb, p, k),
             "decisao_negocio": lambda: _form_negocio(amb, p, k)}[p["tipo"]]()
            if p["alertas"]:
                st.caption(f"Alertas já enviados: {', '.join(p['alertas'])}")
    resolvidas = [p for p in amb.estado.pendencias if p["status"] != "aberta"]
    if resolvidas:
        st.subheader("Resolvidas")
        st.dataframe(pd.DataFrame([{"Pendência": p["pendencia_id"], "Tipo": TIPOS.get(p["tipo"], p["tipo"]),
                                    "Conversa": p["conversation_id"], "Por": p["resolvido_por"],
                                    "Quando": p["resolvido_em"], "Resolução": p["resolucao"]} for p in resolvidas]),
                     hide_index=True, width="stretch")


def aba_analista(s: dict) -> None:
    amb = s["amb"]
    if not amb.estado.incidentes:
        st.info("Nenhum incidente aberto (o analista está desligado ou não achou padrão).")
    for inc in amb.estado.incidentes:
        chave = str(inc.get("chave", "")).replace("brinde ", "")
        avisos = [n for n in amb.estado.notificacoes if chave and chave in n["tipo_aviso"]]
        with st.expander(f"🚨 {inc['incidente_id']} · {inc['tipo']} · {inc['escopo']}", expanded=True):
            c1, c2, c3 = st.columns(3)
            c1.metric("Clientes avisados antes de reclamar", len(avisos))
            c2.metric("Área responsável", inc["area_responsavel"])
            c3.metric("Aberto", inc["aberto_em"][5:16])
            st.caption(f"Origem: {inc.get('origem')}")
            for e in str(inc["evidencias"]).split(" | "):
                st.markdown(f"- {e}")
            if avisos:
                st.info(avisos[0]["texto"])
    _, notas = analista.detectar(0)
    with st.expander("✅ Verificado sem incidente"):
        for n in notas:
            st.markdown(f"- {n}")


def aba_canal(s: dict) -> None:
    msgs = s["amb"].estado.canal_do_time
    st.caption(f"{len(msgs)} mensagens no canal do time (formato Slack), mais recentes primeiro.")
    for m in reversed(msgs[-80:]):
        with st.container(border=True):
            st.markdown(m["texto_slack"].replace("*", "**"))


def aba_semana(s: dict) -> None:
    amb = s["amb"]
    st.write(f"🕐 Agora: **{amb.agora:%d/%m %H:%M}** (tique {amb.tique} de {dados.ULTIMO_TIQUE})")
    st.caption("Cobra os prazos das pendências (75% e vencido), chamados sem resposta e roda o analista às 08:00.")
    c1, c2, c3 = st.columns(3)
    for col, rotulo, horas in ((c1, "+1 hora", 1), (c2, "+1 dia", 24), (c3, "Até 08/11 23:00", None)):
        if col.button(rotulo, disabled=amb.tique >= dados.ULTIMO_TIQUE, width="stretch"):
            alvo = dados.ULTIMO_TIQUE if horas is None else min(amb.tique + horas, dados.ULTIMO_TIQUE)
            ev = semana.simular(amb, ate_tique=alvo)
            avisar(True, f"Relógio em {amb.agora:%d/%m %H:%M}: {len(ev)} eventos.")
            st.rerun()
    if amb.estado.eventos:
        st.dataframe(pd.DataFrame(amb.estado.eventos)[["data_hora", "tipo", "descricao"]].iloc[::-1],
                     hide_index=True, width="stretch")


def aba_auditoria(s: dict) -> None:
    amb = s["amb"]
    st.subheader("Ações executadas (com a trilha de decisão)")
    acoes = pd.DataFrame([{"Quando": a["data_hora"], "Ação": a["acao"], "Conversa": a["conversation_id"],
                           "Pedido": a["order_id"], "Executado por": a["executado_por"], "Aprovado por": a["aprovado_por"],
                           "Por quê": a["trilha"].get("por_que"), "Origem": a["trilha"].get("origem")}
                          for a in amb.estado.acoes])
    st.dataframe(acoes, hide_index=True, width="stretch")
    if amb.estado.validacoes:
        st.subheader("Validações de texto (validador + juiz)")
        v = pd.DataFrame(amb.estado.validacoes)
        v["juiz_notas"] = v["juiz_notas"].apply(lambda x: json.dumps(x, ensure_ascii=False) if x else "")
        st.dataframe(v[["conversation_id", "tentativa", "resultado", "validador_ok", "juiz_aprovado", "juiz_notas",
                        "texto"]], hide_index=True, width="stretch")
    st.subheader("Custo de IA")
    st.json(llm.CUSTOS.resumo())
    if s["pasta"]:
        st.caption(f"Arquivos completos (CSV, log, dossiês) em `{Path(s['pasta'])}`")


# ---------------------------------------------------------------------------

def main() -> None:
    s = sessao()
    barra_lateral(s)
    mostrar_aviso()
    if s["amb"] is None:
        st.title("Agente de Atendimento — colega de trabalho")
        st.markdown("Lê a fila, investiga nos sistemas, decide pela política, **age** com ferramentas e "
                    "**levanta a mão** quando não pode decidir sozinho. Um **analista** acha problemas repetidos e "
                    "avisa os clientes antes de eles reclamarem.\n\nUse **▶️ Rodar a fila** na barra lateral.")
        return
    abas = st.tabs(["📋 Fila", f"🙋 Supervisor ({len(s['amb'].estado.pendencias_abertas())})", "🔎 Analista",
                    "💬 Canal do time", "🗓️ Semana", "🧾 Auditoria"])
    for aba, f in zip(abas, (aba_fila, aba_supervisor, aba_analista, aba_canal, aba_semana, aba_auditoria)):
        with aba:
            f(s)


main()

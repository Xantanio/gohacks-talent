"""Interface da demo (Streamlit): fila, decisões da supervisão, problemas recorrentes, canal do time, semana, auditoria.

Rodar:  .\\venv\\Scripts\\python.exe -m streamlit run app.py    (ou clique duplo em rodar_app.bat)

Visual (D-029): fundo branco, azul como cor principal; cor só para o que importa —
azul = informação, verde = resolvido, âmbar = esperando alguém, vermelho = urgente/vencido.
Quem usa a tela faz o papel da SUPERVISÃO: cada decisão chama src/supervisor.py e o agente retoma o caso (D-027).
"""
from __future__ import annotations

import html
import json
from pathlib import Path

import pandas as pd
import streamlit as st

from src import agente, analista, dados, linguagem as lg, llm, semana, supervisor
from src.ferramentas import Ambiente

st.set_page_config(page_title="Agente de Atendimento", page_icon="💬", layout="wide")

CORES = {  # (fundo, texto)
    "azul": ("#E8EFFD", "#1D4ED8"), "verde": ("#E6F4EA", "#1E7B45"), "ambar": ("#FFF4DB", "#8A5A00"),
    "vermelho": ("#FDECEC", "#B42318"), "cinza": ("#F1F3F5", "#4B5563"),
}
COR_URGENCIA = {"baixa": "cinza", "media": "azul", "alta": "ambar", "critica": "vermelho"}
TIPO_CANAL = {"APROVAÇÃO": "Pedido de aprovação", "SUSPEITA": "Possível reclamação indevida",
              "MÃO LEVANTADA": "Pessoa chamada", "INCIDENTE": "Incidente aberto",
              "COSMETOVIGILÂNCIA": "Reação adversa registrada", "RESPOSTA A COLABORADOR": "Resposta a colega"}
TIPO_EVENTO = {"prazo_75": "Prazo acabando", "prazo_vencido": "Prazo vencido",
               "chamados_sem_resposta": "Transportadora sem resposta", "analista_diario": "Rotina diária do analista",
               "retomada": "Caso retomado"}
TIPO_INCIDENTE = {"maquina": "Falha de impressão", "transportadora_uf": "Atraso de transportadora",
                  "turno": "Brinde faltando na separação", "lote": "Defeito em lote", "gateway": "Cobrança duplicada"}

CSS = """
<style>
.block-container {padding-top: 2rem; max-width: 1200px;}
h1, h2, h3 {color: #0F2B5B; font-weight: 600;}
[data-testid="stMetric"] {background: #F3F6FB; border: 1px solid #DCE6F5; border-radius: 10px; padding: 10px 14px;}
[data-testid="stMetricLabel"] p {color: #4B5563;}
[data-testid="stVerticalBlockBorderWrapper"] {border-color: #DCE6F5 !important; border-radius: 10px;}
.selo {display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 0.78rem; font-weight: 600;
       margin-right: 6px;}
.cartao {border: 1px solid #DCE6F5; border-left: 4px solid var(--borda); border-radius: 10px; padding: 12px 16px;
         margin-bottom: 10px; background: #FFFFFF;}
.cartao .titulo {font-weight: 600; color: #0F2B5B; margin: 6px 0 4px 0;}
.cartao .campo {color: #374151; font-size: 0.9rem; margin: 2px 0;}
.cartao .espera {background: #E8EFFD; color: #1D4ED8; border-radius: 6px; padding: 6px 10px; margin-top: 8px;
                 font-size: 0.9rem;}
.discreto {color: #6B7280; font-size: 0.85rem;}
table.fila {width: 100%; border-collapse: collapse; font-size: 0.92rem;}
table.fila th {text-align: left; color: #4B5563; font-weight: 600; border-bottom: 2px solid #DCE6F5; padding: 8px;}
table.fila td {border-bottom: 1px solid #EEF2F8; padding: 8px; vertical-align: top;}
</style>
"""


def selo(texto: str, cor: str = "azul") -> str:
    fundo, frente = CORES[cor]
    return f'<span class="selo" style="background:{fundo};color:{frente}">{html.escape(texto)}</span>'


# ---------------------------------------------------------------------------
# Estado da sessão
# ---------------------------------------------------------------------------

def sessao() -> dict:
    s = st.session_state
    for k, v in (("amb", None), ("resultados", []), ("pasta", None), ("usar_ia", True), ("aviso", None)):
        s.setdefault(k, v)
    return s


def avisar(ok: bool, texto: str) -> None:
    st.session_state["aviso"] = ("success" if ok else "error", texto)


def mostrar_aviso() -> None:
    a = st.session_state.get("aviso")
    if a:
        getattr(st, a[0])(a[1])
        st.session_state["aviso"] = None


def prazo_info(prazo: str | None, amb: Ambiente) -> tuple[str, str]:
    """(texto, cor) do prazo de uma pendência."""
    if not prazo:
        return "sem prazo", "cinza"
    h = (pd.Timestamp(prazo) - pd.Timestamp(amb.agora)).total_seconds() / 3600
    if h <= 0:
        return f"vencido há {abs(h):.0f}h", "vermelho"
    texto = f"faltam {h:.0f}h" if h >= 1 else f"faltam {h * 60:.0f} min"
    return texto, "ambar" if h <= 6 else "azul"


# ---------------------------------------------------------------------------
# Barra lateral
# ---------------------------------------------------------------------------

def barra_lateral(s: dict) -> None:
    st.sidebar.markdown("### Agente de Atendimento")
    st.sidebar.caption("Gocase · GoBeauté")
    canais = st.sidebar.multiselect("Canais", list(lg.CANAL), default=["reclame_aqui"], format_func=lg.CANAL.get)
    limite = st.sidebar.number_input("Quantos tickets (0 = todos)", 0, 100, 0)
    s["usar_ia"] = st.sidebar.toggle("Usar IA", value=s["usar_ia"],
                                     help="Classificação, redação e revisão por IA. Sem IA, tudo vai para uma pessoa.")
    com_analista = st.sidebar.toggle("Analista de problemas recorrentes", value=True)
    if st.sidebar.button("Rodar a fila", type="primary", width="stretch"):
        llm.CUSTOS.chamadas.clear()
        llm.CUSTOS.ao_registrar = None
        amb = Ambiente()
        with st.spinner("Lendo, decidindo e respondendo..."):
            resultados, pasta = agente.rodar(canais or ["reclame_aqui"], limite or None, s["usar_ia"], "saida",
                                             amb=amb, com_analista=com_analista)
        s.update(amb=amb, resultados=resultados, pasta=pasta)
        avisar(True, f"{len(resultados)} tickets processados.")
        st.rerun()
    if s["amb"] is not None:
        cu = llm.CUSTOS.resumo()
        st.sidebar.divider()
        st.sidebar.caption(f"Custo de IA nesta execução: Jev US$ {cu['jev_usd']:.5f} · "
                           f"textos {cu['chat_tokens']:,} tokens".replace(",", "."))


def painel(s: dict) -> None:
    amb, res = s["amb"], s["resultados"]
    st.markdown(f"## Atendimento · {amb.agora:%d/%m %H:%M}")
    c = st.columns(5)
    c[0].metric("Tickets", len(res))
    c[1].metric("Respondidos sozinho", sum(r.publicada for r in res))
    c[2].metric("Esperando uma pessoa", len(amb.estado.pendencias_abertas()))
    c[3].metric("Problemas recorrentes", len(amb.estado.incidentes))
    c[4].metric("Clientes avisados antes", len(amb.estado.notificacoes))


# ---------------------------------------------------------------------------
# Abas
# ---------------------------------------------------------------------------

def aba_fila(s: dict) -> None:
    res, amb = s["resultados"], s["amb"]
    linhas = []
    for r in res:
        t = r.ticket
        feitas = [lg.acao(e["ferramenta"]) for e in r.executados
                  if e["ok"] and e["ferramenta"] not in ("classificar_conversa", "acionar_pessoa")]
        if r.publicada:
            situacao = selo("Respondido", "verde")
        elif r.motivos_retencao:
            situacao = selo("Esperando uma pessoa", "ambar")
        else:
            situacao = selo("Sem resposta", "cinza")
        if r.padroes:
            situacao += selo("Problema recorrente", "azul")
        linhas.append(f"<tr><td><b>{t.conversation_id}</b><br><span class='discreto'>{html.escape(t.empresa)}</span></td>"
                      f"<td>{lg.CANAL.get(t.canal, t.canal)}</td>"
                      f"<td>{html.escape(lg.categoria(t.classificacao.categoria if t.classificacao else None))}</td>"
                      f"<td>{html.escape(', '.join(dict.fromkeys(feitas)) or '-')}</td><td>{situacao}</td></tr>")
    st.markdown("<table class='fila'><tr><th>Conversa</th><th>Canal</th><th>Assunto</th><th>O que o agente fez</th>"
                "<th>Situação</th></tr>" + "".join(linhas) + "</table>", unsafe_allow_html=True)
    st.write("")
    escolha = st.selectbox("Ver o resumo de um ticket", [r.ticket.conversation_id for r in res])
    r = next(r for r in res if r.ticket.conversation_id == escolha)
    with st.container(border=True):
        st.markdown(agente.dossie_simples(r, amb), unsafe_allow_html=True)
    with st.expander("Detalhes técnicos (auditoria)"):
        st.markdown(agente.dossie_tecnico(r, amb))


def _titulo_pendencia(p: dict) -> str:
    d = p["dados"]
    if p["tipo"] == "aprovacao":
        return f"Aprovar {lg.acao(d['acao'])} de R$ {d['valor']:.2f}".replace(".", ",")
    if p["tipo"] == "publicacao":
        return "Revisar a resposta antes de publicar"
    if p["tipo"] == "cosmetovigilancia":
        return f"Registrar reação adversa ao produto {d['sku']}"
    if p["tipo"] == "suspeita":
        return "Decidir se a reclamação procede"
    return lg.humanizar(p["resumo"])


def _form_aprovacao(amb, p, k) -> None:
    d = p["dados"]
    st.markdown(f"<span class='discreto'>Motivo do pedido: {html.escape(lg.humanizar(d.get('params', {}).get('motivo', '')) or p['resumo'])}</span>",
                unsafe_allow_html=True)
    texto = st.text_input("Comentário (interno)", key=f"j{k}")
    c1, c2, _ = st.columns([1, 1, 3])
    if c1.button("Aprovar", key=f"a{k}", type="primary"):
        r = supervisor.aprovar(amb, p["pendencia_id"], comentario=texto or None)
        avisar(r["ok"], f"Aprovado: {lg.acao(d['acao'])} feito e cliente avisado." if r["ok"]
               else f"Não foi possível: {r.get('erro') or r.get('execucao', {}).get('erro')}")
        st.rerun()
    if c2.button("Rejeitar", key=f"r{k}"):
        if not texto:
            st.warning("Escreva o motivo da rejeição (fica só para a equipe).")
        else:
            r = supervisor.rejeitar(amb, p["pendencia_id"], texto)
            avisar(r["ok"], "Rejeitado: o cliente recebeu uma resposta cordial.")
            st.rerun()


def _form_suspeita(amb, p, k, s) -> None:
    decisao = st.radio("A reclamação procede?", ["Sim, procede", "Não procede"], horizontal=True, key=f"d{k}")
    texto = st.text_input("Por quê (interno)", key=f"j{k}")
    if st.button("Confirmar decisão", key=f"b{k}", type="primary", disabled=not texto):
        procede = decisao.startswith("Sim")
        ia = s["usar_ia"]
        r = supervisor.decidir_suspeita(amb, p["pendencia_id"], procede, texto,
                                        redigir=llm.redigir if ia else None, julgar=llm.julgar if ia else None)
        avisar(r["ok"], "O agente retomou o atendimento." if procede else "O cliente recebeu uma resposta neutra.")
        st.rerun()


def _form_publicacao(amb, p, k) -> None:
    d = p["dados"]
    motivos = "; ".join(agente._simples(m) for m in d.get("motivos") or [])
    st.markdown(f"<span class='discreto'>Por que esperou: {html.escape(motivos)}</span>", unsafe_allow_html=True)
    for ret in d.get("retidos") or []:
        st.markdown(selo(f"Também será enviada: {lg.acao(ret['ferramenta'])}", "azul"), unsafe_allow_html=True)
    texto = st.text_area("Texto para o cliente (pode editar; passa pela revisão automática)", d.get("texto") or "",
                         key=f"t{k}", height=150)
    c1, c2, _ = st.columns([1, 1, 3])
    if c1.button("Liberar e publicar", key=f"l{k}", type="primary"):
        editado = texto if texto != (d.get("texto") or "") else None
        r = supervisor.liberar(amb, p["pendencia_id"], texto=editado if texto else None)
        avisar(r["ok"], "Publicado." if r["ok"] else lg.humanizar(r["erro"]))
        st.rerun()
    if c2.button("Descartar", key=f"x{k}"):
        supervisor.descartar(amb, p["pendencia_id"], "descartado na interface")
        avisar(True, "Descartado: nada foi publicado.")
        st.rerun()


def _form_cosmeto(amb, p, k) -> None:
    d = p["dados"]
    st.markdown(f"<span class='discreto'>Produto {d['sku']} · lote {d.get('lote') or 'não informado'} · "
                f"fotos: {d.get('fotos') or 'nenhuma'}</span>", unsafe_allow_html=True)
    texto = st.text_area("Relato do cliente", d["descricao"], key=f"t{k}", height=100)
    if st.button("Registrar", key=f"c{k}", type="primary"):
        r = supervisor.registrar_cosmetovigilancia(amb, p["pendencia_id"], descricao=texto)
        if r["ok"]:
            avisar(True, f"Registrado ({r.get('registro_id')}) {'no prazo' if r.get('dentro_do_prazo') else 'FORA do prazo'}."
                   + (f" Atenção: {lg.humanizar(', '.join(r['pendencias']))}." if r.get("pendencias") else ""))
        else:
            avisar(False, r["erro"])
        st.rerun()


def _form_negocio(amb, p, k) -> None:
    d = p["dados"]
    st.markdown(f"{len(d['order_ids'])} clientes afetados já passaram do prazo de 7 dias para reposição. "
                "Se estender o prazo, o analista envia este aviso:")
    st.info(d["texto_aviso"])
    texto = st.text_input("Por quê (interno)", key=f"j{k}")
    c1, c2, _ = st.columns([1, 1, 3])
    if c1.button("Estender e avisar", key=f"e{k}", type="primary", disabled=not texto):
        r = supervisor.decidir_negocio(amb, p["pendencia_id"], True, texto)
        avisar(True, f"{r.get('enviados', 0)} clientes avisados.")
        st.rerun()
    if c2.button("Manter a regra", key=f"m{k}", disabled=not texto):
        supervisor.decidir_negocio(amb, p["pendencia_id"], False, texto)
        avisar(True, "Regra mantida.")
        st.rerun()


def aba_decidir(s: dict) -> None:
    amb = s["amb"]
    abertas = sorted(amb.estado.pendencias_abertas(), key=lambda p: p["prazo"] or "9999")
    st.caption("Você faz o papel da supervisão. Cada decisão é registrada, e o agente continua o atendimento sozinho.")
    if not abertas:
        st.success("Nada esperando decisão.")
    for p in abertas:
        prazo, cor = prazo_info(p["prazo"], amb)
        with st.container(border=True):
            st.markdown(selo(lg.PENDENCIA.get(p["tipo"], p["tipo"]), "azul") + selo(prazo, cor)
                        + f"<span class='discreto'>{p['conversation_id'] or ''} · {p['pendencia_id']}</span>",
                        unsafe_allow_html=True)
            st.markdown(f"**{html.escape(_titulo_pendencia(p))}**")
            k = p["pendencia_id"]
            {"aprovacao": lambda: _form_aprovacao(amb, p, k), "suspeita": lambda: _form_suspeita(amb, p, k, s),
             "publicacao": lambda: _form_publicacao(amb, p, k), "cosmetovigilancia": lambda: _form_cosmeto(amb, p, k),
             "decisao_negocio": lambda: _form_negocio(amb, p, k)}[p["tipo"]]()
    resolvidas = [p for p in amb.estado.pendencias if p["status"] != "aberta"]
    if resolvidas:
        with st.expander(f"Já decididas ({len(resolvidas)})"):
            st.dataframe(pd.DataFrame([{"Tipo": lg.PENDENCIA.get(p["tipo"], p["tipo"]), "Conversa": p["conversation_id"],
                                        "Quem decidiu": p["resolvido_por"], "Quando": p["resolvido_em"],
                                        "Decisão": lg.humanizar(p["resolucao"])} for p in resolvidas]),
                         hide_index=True, width="stretch")


def aba_recorrentes(s: dict) -> None:
    amb = s["amb"]
    st.caption("O analista procura muitas reclamações com a mesma causa (impressora, transportadora, centro de "
               "distribuição, lote, pagamento), abre um incidente para a área responsável e avisa quem ainda não "
               "reclamou.")
    if not amb.estado.incidentes:
        st.info("Nenhum problema recorrente encontrado.")
    for inc in amb.estado.incidentes:
        chave = str(inc.get("chave", "")).replace("brinde ", "")
        avisos = [n for n in amb.estado.notificacoes if chave and chave in n["tipo_aviso"]]
        with st.container(border=True):
            st.markdown(selo(TIPO_INCIDENTE.get(inc["tipo"], inc["tipo"]), "azul")
                        + selo(f"Área: {lg.AREA.get(inc['area_responsavel'], inc['area_responsavel'])}", "cinza")
                        + selo(f"{len(avisos)} clientes avisados antes de reclamar", "verde" if avisos else "cinza"),
                        unsafe_allow_html=True)
            st.markdown(f"**{html.escape(inc['escopo'])}**")
            origem = str(inc.get("origem") or "")
            st.markdown(f"<span class='discreto'>{'Percebido pelo atendente' if 'atendente' in origem else 'Encontrado na varredura do analista'}"
                        f" · {inc['incidente_id']}</span>", unsafe_allow_html=True)
            for e in str(inc["evidencias"]).split(" | "):
                st.markdown(f"- {lg.humanizar(e)}")
            if avisos:
                st.info(avisos[0]["texto"])
    _, notas = analista.detectar(0)
    with st.expander("Verificado e sem problema"):
        for n in notas:
            st.markdown(f"- {n}")


def aba_canal(s: dict) -> None:
    msgs = s["amb"].estado.canal_do_time
    st.markdown("<div class='cartao' style='--borda:#1D4ED8'><div class='titulo'>O que é o canal do time</div>"
                "<div class='campo'>É o canal da equipe (como um grupo no Slack). Sempre que um agente precisa de "
                "uma pessoa, ele posta aqui: <b>para qual área</b>, <b>com que urgência</b>, <b>o que aconteceu</b> e "
                "<b>o que espera</b> dessa pessoa. As decisões em si são tomadas na aba “Para decidir”.</div></div>",
                unsafe_allow_html=True)
    areas = sorted({m["area"] for m in msgs})
    c1, c2 = st.columns(2)
    f_area = c1.multiselect("Área", areas, format_func=lambda a: lg.AREA.get(a, a))
    f_urg = c2.multiselect("Urgência", list(lg.URGENCIA), format_func=lg.URGENCIA.get)
    filtradas = [m for m in msgs if (not f_area or m["area"] in f_area) and (not f_urg or m["urgencia"] in f_urg)]
    st.caption(f"{len(filtradas)} mensagens, mais recentes primeiro.")
    for m in reversed(filtradas[-60:]):
        cor = COR_URGENCIA.get(m["urgencia"], "azul")
        campos = dict(m.get("campos") or {})
        espera = campos.pop("O que esperamos", None)
        corpo = "".join(f"<div class='campo'><b>{html.escape(k)}:</b> {html.escape(lg.humanizar(v))}</div>"
                        for k, v in campos.items() if k != "Por que")
        st.markdown(
            f"<div class='cartao' style='--borda:{CORES[cor][1]}'>"
            + selo(TIPO_CANAL.get(m["tipo"], m["tipo"].title()), "azul") + selo(lg.AREA.get(m["area"], m["area"]), "cinza")
            + selo(f"Urgência {lg.URGENCIA.get(m['urgencia'], m['urgencia']).lower()}", cor)
            + f"<span class='discreto'>{m['data_hora'][8:10]}/{m['data_hora'][5:7]} {m['data_hora'][11:16]}</span>"
            + f"<div class='titulo'>{html.escape(lg.humanizar(m.get('titulo') or m['tipo']))}</div>{corpo}"
            + (f"<div class='espera'><b>O que esperamos:</b> {html.escape(lg.humanizar(espera))}</div>" if espera else "")
            + "</div>", unsafe_allow_html=True)


def aba_semana(s: dict) -> None:
    amb = s["amb"]
    st.markdown(f"**Relógio do ambiente:** {amb.agora:%d/%m/%Y %H:%M} · a semana vai até 08/11 23:00")
    st.caption("Avançar o tempo cobra os prazos (aviso quando falta pouco e quando vence), avisa sobre chamados "
               "sem resposta da transportadora e roda a rotina diária do analista às 08:00.")
    c = st.columns([1, 1, 1, 3])
    for col, rotulo, horas in ((c[0], "+1 hora", 1), (c[1], "+1 dia", 24), (c[2], "Até o fim", None)):
        if col.button(rotulo, disabled=amb.tique >= dados.ULTIMO_TIQUE, width="stretch"):
            alvo = dados.ULTIMO_TIQUE if horas is None else min(amb.tique + horas, dados.ULTIMO_TIQUE)
            ev = semana.simular(amb, ate_tique=alvo)
            avisar(True, f"Agora: {amb.agora:%d/%m %H:%M} · {len(ev)} acontecimentos.")
            st.rerun()
    for e in reversed(amb.estado.eventos[-60:]):
        cor = "vermelho" if e["tipo"] in ("prazo_vencido", "chamados_sem_resposta") else \
              "ambar" if e["tipo"] == "prazo_75" else "verde" if e["tipo"] == "retomada" else "azul"
        st.markdown(f"{selo(TIPO_EVENTO.get(e['tipo'], e['tipo']), cor)}"
                    f"<span class='discreto'>{e['data_hora'][8:10]}/{e['data_hora'][5:7]} {e['data_hora'][11:16]}</span> "
                    f"{html.escape(lg.humanizar(e['descricao']))}", unsafe_allow_html=True)


def aba_auditoria(s: dict) -> None:
    amb = s["amb"]
    st.caption("Tudo o que foi feito, por quem e por quê. Os arquivos completos ficam na pasta de saída.")
    st.markdown("#### Ações")
    st.dataframe(pd.DataFrame([{"Quando": a["data_hora"], "Ação": lg.acao(a["acao"]), "Conversa": a["conversation_id"],
                                "Quem fez": a["executado_por"], "Quem aprovou": a["aprovado_por"],
                                "Por quê": lg.humanizar(a["trilha"].get("por_que")), "Registro": a["acao_id"]}
                               for a in amb.estado.acoes]), hide_index=True, width="stretch")
    if amb.estado.validacoes:
        st.markdown("#### Revisão dos textos (regras automáticas + revisor de qualidade)")
        v = pd.DataFrame(amb.estado.validacoes)
        v["notas"] = v["juiz_notas"].apply(lambda x: ", ".join(f"{k} {n}" for k, n in x.items()) if x else "")
        st.dataframe(v.rename(columns={"conversation_id": "Conversa", "tentativa": "Tentativa", "resultado": "Resultado",
                                       "notas": "Notas do revisor", "texto": "Texto"})
                     [["Conversa", "Tentativa", "Resultado", "Notas do revisor", "Texto"]], hide_index=True, width="stretch")
    st.markdown("#### Custo de IA")
    st.json(llm.CUSTOS.resumo())
    if s["pasta"]:
        st.caption(f"Arquivos completos (planilhas, registro de chamadas e resumos) em `{Path(s['pasta'])}`")


# ---------------------------------------------------------------------------

def main() -> None:
    st.markdown(CSS, unsafe_allow_html=True)
    s = sessao()
    barra_lateral(s)
    mostrar_aviso()
    if s["amb"] is None:
        st.markdown("## Agente de Atendimento")
        st.markdown("Lê a fila, consulta os sistemas, decide pela regra, **resolve** e **chama uma pessoa** quando não "
                    "pode decidir sozinho. Um **analista** encontra problemas que se repetem e avisa os clientes antes "
                    "de eles reclamarem.")
        st.info("Clique em **Rodar a fila**, na barra lateral, para começar.")
        return
    painel(s)
    abas = st.tabs(["Fila", f"Para decidir ({len(s['amb'].estado.pendencias_abertas())})", "Problemas recorrentes",
                    "Canal do time", "Semana", "Auditoria"])
    for aba, f in zip(abas, (aba_fila, aba_decidir, aba_recorrentes, aba_canal, aba_semana, aba_auditoria)):
        with aba:
            f(s)


main()

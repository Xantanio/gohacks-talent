"""Decisão por custo esperado: junta política (`politicas.py`), veracidade (`veracidade.py`) e o valor do cliente.

Ordem (cada etapa fica registrada em `Decisao.motivos` e na trilha):
1. A POLÍTICA manda primeiro: se o caso não é elegível, falta informação, já está em andamento ou deve ser
   escalado, seguimos os passos dela. O agente não negocia a política (nem para VIP, nem a pedido de colaborador).
2. Caso elegível SEM dinheiro, ou com prova nos sistemas (gateway, rastreio, direito de arrependimento):
   segue a política (que já inclui solicitar_aprovacao quando passa da alçada).
3. Caso elegível COM dinheiro e baseado em foto (avaria, item faltante, personalização):
   a. evidência forte de fraude -> sinalizar_suspeita, sempre (CLAUDE.md);
   b. freios de cliente novo (teto e 1 resolução automática) -> solicitar_aprovacao, sem tom de suspeita;
   c. novo sem evidência positiva -> pessoa; demais -> menor custo esperado entre executar e levar à supervisão.

Custos (parametros.md; CUSTO_RA lido de politicas.csv):
  executar      = (1 - p) x valor x 0,45                       (reenviar/reembolsar reclamação falsa)
  supervisor    = R$ 6 + p x FRACAO_ESPERA x custo_negar         (tempo do supervisor + até 24h de espera)
  negar         = p x (½ LTV + 0,30 x R$ 42)                     (só informativo: o agente NÃO nega por suspeita)
LTV (D-017): receita futura esperada = receita_acumulada x r/(1-r), r = 28% (70% acima de R$ 1.500).
Cliente novo: LTV potencial = ticket médio x RECOMPRA_ESPERADA_NOVO, quando maior que o histórico.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pandas as pd

from . import dados
from .politicas import POL, Avaliacao, Fatos, Passo, avaliar, coletar_fatos
from .veracidade import CATEGORIAS_COM_FOTO, Veracidade, avaliar_veracidade

if TYPE_CHECKING:
    from .ferramentas import Ambiente

# ---------------------------------------------------------------------------
# Parâmetros (parametros.md + escolhas de negócio explícitas; ver D-017)
# ---------------------------------------------------------------------------

PARAMETROS: dict[str, float] = {
    "FRACAO_CUSTO_FALSA": 0.45,          # parametros.md: valor x 45% (produto, frete, manuseio)
    "P_VIRA_RECLAME_AQUI": 0.30,         # parametros.md
    "CUSTO_RECLAME_AQUI": float(POL["custo_por_conversa_rs"]["reclame_aqui"]),  # R$ 42 (politicas.csv)
    "CUSTO_SUPERVISOR": 6.0,             # parametros.md: R$ 6 por caso sinalizado
    "FRACAO_ESPERA": 0.10,               # escolha: esperar até 24h custa 10% do custo de negar
    "RECOMPRA_ANO": 0.28,                # parametros.md
    "RECOMPRA_ANO_ALTA": 0.70,           # parametros.md: acima de R$ 1.500 de receita acumulada
    "LIMIAR_RECEITA_ALTA": 1500.0,
    "TETO_AUTOMATICO_NOVO": 150.0,       # CLAUDE.md (configurável), abaixo da alçada de R$ 200
    "MAX_RESOLUCOES_AUTOMATICAS_NOVO": 1,  # CLAUDE.md: a 2ª reclamação com dinheiro vai para pessoa
    "RECOMPRA_ESPERADA_NOVO": 2.0,       # escolha: novato bem atendido compra +2 vezes (mediana recorrente = 3 pedidos)
    "LR_SINAL_FORTE": 8.0,               # sinais com LR >= 8 são "evidência forte" (ver veracidade.PARAMETROS)
}
P = PARAMETROS

# Sinais que, sozinhos, obrigam a sinalizar (CLAUDE.md: "foto reaproveitada -> sinalizar_suspeita sempre").
SINAIS_SEMPRE_SINALIZAR = {"foto_reaproveitada_outro_cliente", "foto_reaproveitada_outro_pedido",
                           "etiqueta_de_outro_pedido", "identidade_compartilhada", "suspeita_improcedente_anterior"}


# ---------------------------------------------------------------------------
# Valor do cliente
# ---------------------------------------------------------------------------

@dataclass
class ValorCliente:
    customer_id: str
    segmento: str
    novo: bool
    n_pedidos: int
    receita_acumulada: float
    ticket_medio: float
    taxa_recompra: float
    ltv_historico: float
    ltv_potencial: float | None
    ltv: float                       # o que entra no custo
    criterio: str                    # frase para a trilha

    @property
    def custo_negar_verdadeira(self) -> float:
        return round(0.5 * self.ltv + P["P_VIRA_RECLAME_AQUI"] * P["CUSTO_RECLAME_AQUI"], 2)


def valor_do_cliente(cliente: dict[str, Any], valor_pedido: float | None = None) -> ValorCliente:
    receita = float(cliente.get("receita_acumulada") or 0)
    n = int(cliente.get("n_pedidos") or 0)
    ticket = receita / n if n else float(valor_pedido or 0)
    r = P["RECOMPRA_ANO_ALTA"] if receita > P["LIMIAR_RECEITA_ALTA"] else P["RECOMPRA_ANO"]
    ltv_hist = round(receita * r / (1 - r), 2)
    novo = cliente.get("segmento") == "novo" or n <= 1
    potencial = round(ticket * P["RECOMPRA_ESPERADA_NOVO"], 2) if novo else None
    if potencial is not None and potencial > ltv_hist:
        ltv, criterio = potencial, (f"cliente novo: LTV potencial = ticket R$ {ticket:.2f} x "
                                    f"{P['RECOMPRA_ESPERADA_NOVO']:g} recompras = R$ {potencial:.2f} "
                                    f"(histórico daria R$ {ltv_hist:.2f})")
    else:
        ltv, criterio = ltv_hist, (f"LTV = receita R$ {receita:.2f} x {r:.0%}/(1-{r:.0%}) = R$ {ltv_hist:.2f}")
    return ValorCliente(cliente["customer_id"], cliente.get("segmento") or "?", novo, n, receita, round(ticket, 2),
                        r, ltv_hist, potencial, ltv, criterio)


def resolucoes_automaticas(amb: Ambiente, customer_id: str, trocas_historico: list[dict[str, Any]]) -> list[str]:
    """Reposições/estornos do cliente feitos SEM aprovação de supervisor (histórico + esta execução)."""
    ids = [t["solicitacao_id"] for t in trocas_historico if not t.get("aprovado_por")]
    ids += [o["solicitacao_id"] for o in amb.estado.oms if o["customer_id"] == customer_id and not o["aprovado_por"]]
    peds = dados.tabela("pedidos")
    do_cliente = set(peds.loc[peds["customer_id"] == customer_id, "order_id"])
    ids += [e["estorno_id"] for e in amb.estado.estornos if e["order_id"] in do_cliente and not e["aprovado_por"]]
    return ids


# ---------------------------------------------------------------------------
# Resultado
# ---------------------------------------------------------------------------

@dataclass
class Decisao:
    conversation_id: str
    categoria: str
    caminho: str                     # seguir_politica | executar | solicitar_aprovacao | sinalizar_suspeita
    passos: list[Passo]
    resumo: str
    motivos: list[str] = field(default_factory=list)
    alternativas: dict[str, float] = field(default_factory=dict)   # caminho -> custo esperado (R$)
    custo_esperado: float | None = None
    p_verdadeira: float | None = None
    requer_humano: bool = False
    intencao_resposta: str = ""      # o que o texto ao cliente deve comunicar (llm.py redige)
    avaliacao: Avaliacao | None = None
    veracidade: Veracidade | None = None
    cliente: ValorCliente | None = None

    def trilha(self) -> dict[str, Any]:
        evid = list(self.avaliacao.fatos) if self.avaliacao else []
        if self.veracidade and self.veracidade.aplicavel:
            evid += self.veracidade.evidencias()
        if self.cliente:
            evid.append(self.cliente.criterio)
        t: dict[str, Any] = {"por_que": self.resumo, "regra": self.avaliacao.regra if self.avaliacao else None,
                             "evidencias": evid + [f"decisão: {m}" for m in self.motivos], "origem": "regra"}
        if self.alternativas:
            t["alternativas"] = self.alternativas
        if self.p_verdadeira is not None:
            t["p_verdadeira"] = self.p_verdadeira
        if self.custo_esperado is not None:
            t["custo_esperado"] = self.custo_esperado
        return t


# ---------------------------------------------------------------------------
# Decisão
# ---------------------------------------------------------------------------

def custos(p: float, valor: float, cliente: ValorCliente) -> dict[str, float]:
    negar = cliente.custo_negar_verdadeira
    return {
        "executar": round((1 - p) * valor * P["FRACAO_CUSTO_FALSA"], 2),
        "supervisor": round(P["CUSTO_SUPERVISOR"] + p * P["FRACAO_ESPERA"] * negar, 2),
        "negar (não permitido ao agente)": round(p * negar, 2),
    }


def _passos_aprovacao(av: Avaliacao, justificativa: str) -> list[Passo]:
    """Converte o passo de dinheiro da política em solicitar_aprovacao + ação com aprovacao_id."""
    if av.passos and av.passos[0].ferramenta == "solicitar_aprovacao":
        return av.passos
    acao = av.passos[0]
    return [Passo("solicitar_aprovacao", {"acao": acao.ferramenta, "order_id": acao.params["order_id"],
                                          "valor": acao.params["valor"], "justificativa": justificativa}),
            Passo(acao.ferramenta, {**acao.params, "aprovacao_id": "<da aprovação>"}, nota="depois de aprovado")]


def _passos_suspeita(f: Fatos, v: Veracidade, grupo: bool) -> list[Passo]:
    passos = [Passo("sinalizar_suspeita", {"conversation_id": f.conversation_id, **v.para_sinalizar()}),
              Passo("responder_cliente", {"conversation_id": f.conversation_id},
                    nota="informar apenas que a solicitação está em análise; nunca acusar")]
    if grupo:
        passos.insert(1, Passo("acionar_pessoa", {"area": "supervisão", "urgencia": "alta",
                                                  "assunto": f"Possível grupo — {f.customer_id}",
                                                  "contexto": "; ".join(f"{g['campo']} com {g['customer_id']}"
                                                                        for g in v.grupo_suspeito)}))
    return passos


def decidir(amb: Ambiente, f: Fatos, av: Avaliacao, v: Veracidade) -> Decisao:
    cli = amb.consultar_cliente(f.customer_id)
    vc = valor_do_cliente(cli["cliente"], (f.pedido or {}).get("valor")) if cli["ok"] else None
    base = dict(conversation_id=f.conversation_id, categoria=av.categoria, avaliacao=av, veracidade=v, cliente=vc)

    # 1-2. A política manda; dinheiro sem foto tem prova nos sistemas.
    if av.situacao != "elegivel" or not av.envolve_dinheiro or av.alternativas or av.categoria not in CATEGORIAS_COM_FOTO:
        motivo = {"elegivel": "elegível com prova nos sistemas ou sem dinheiro: segue a política",
                  "nao_elegivel": "fora da política: segue a política (o agente não abre exceção)",
                  "falta_informacao": "falta informação exigida pela política",
                  "em_andamento": "já há reposição em andamento",
                  "escalar": "a política manda escalar"}[av.situacao]
        intencao = {"elegivel": "confirmar o que foi feito e o próximo passo com prazo",
                    "nao_elegivel": "explicar a regra com empatia e oferecer o que for possível dentro dela",
                    "falta_informacao": "pedir a informação que falta, com gentileza e sem tom de suspeita",
                    "em_andamento": "informar o andamento da reposição já aberta",
                    "escalar": "acolher e informar que um especialista está cuidando do caso"}[av.situacao]
        return Decisao(**base, caminho="seguir_politica", passos=av.passos, resumo=av.resumo, motivos=[motivo],
                       requer_humano=av.situacao == "escalar" or av.acima_da_alcada, intencao_resposta=intencao,
                       p_verdadeira=v.p_verdadeira if v.aplicavel else None)

    # 3. Dinheiro com base em foto.
    p, valor = v.p_verdadeira, float(av.valor_acao)
    c = custos(p, valor, vc)
    motivos = [f"p_verdadeira={p:.2f}; valor R$ {valor:.2f}; custo de negar verdadeira R$ {vc.custo_negar_verdadeira:.2f}"]

    suspeita = amb.estado.suspeitas.get(f.conversation_id)
    if suspeita and suspeita["status"] == "procedente":  # retomada após decisão humana (D-027): não sinaliza de novo
        motivos.append(f"supervisor {suspeita['decidido_por']} julgou procedente: {suspeita['justificativa']}")
        if av.acima_da_alcada:
            return Decisao(**base, caminho="solicitar_aprovacao", passos=av.passos, resumo=av.resumo, motivos=motivos,
                           alternativas=c, custo_esperado=c["supervisor"], p_verdadeira=p, requer_humano=True,
                           intencao_resposta="informar que a solução foi encaminhada")
        return Decisao(**base, caminho="executar", passos=av.passos, resumo=f"Procedente pelo supervisor: {av.resumo}",
                       motivos=motivos, alternativas=c, custo_esperado=c["executar"], p_verdadeira=p,
                       intencao_resposta="confirmar a solução concreta, sem mencionar a análise interna")

    fortes = [s for s in v.contra if s.nome in SINAIS_SEMPRE_SINALIZAR or s.lr >= P["LR_SINAL_FORTE"]]
    if fortes:
        motivos.append("evidência forte: " + ", ".join(s.nome for s in fortes) + " -> sinalizar sempre")
        return Decisao(**base, caminho="sinalizar_suspeita", passos=_passos_suspeita(f, v, bool(v.grupo_suspeito)),
                       resumo=f"Evidência forte contra a reclamação ({fortes[0].nome}): supervisor decide.",
                       motivos=motivos, alternativas=c, custo_esperado=c["supervisor"], p_verdadeira=p,
                       requer_humano=True, intencao_resposta="informar que a solicitação está em análise, sem acusar")

    if av.acima_da_alcada:
        motivos.append("acima da alçada: aprovação obrigatória")
        return Decisao(**base, caminho="solicitar_aprovacao", passos=av.passos, resumo=av.resumo, motivos=motivos,
                       alternativas=c, custo_esperado=c["supervisor"], p_verdadeira=p, requer_humano=True,
                       intencao_resposta="informar que a solução foi encaminhada e o prazo de retorno")

    if vc.novo:
        autos = resolucoes_automaticas(amb, f.customer_id, cli.get("trocas_reenvios", []))
        evidencia_positiva = v.tem_foto and not [s for s in v.contra if s.grupo == "foto" and s.lr > 1.2]
        freio = None
        if valor > P["TETO_AUTOMATICO_NOVO"]:
            freio = f"cliente novo: R$ {valor:.2f} acima do teto automático de R$ {P['TETO_AUTOMATICO_NOVO']:.2f}"
        elif len(autos) >= P["MAX_RESOLUCOES_AUTOMATICAS_NOVO"]:
            freio = f"cliente novo já teve resolução automática ({', '.join(autos)}): a 2ª vai para pessoa"
        elif not evidencia_positiva:
            freio = "cliente novo sem evidência positiva na foto"
        if freio:
            motivos.append(freio)
            return Decisao(**base, caminho="solicitar_aprovacao", passos=_passos_aprovacao(av, freio),
                           resumo=f"{freio}: uma pessoa aprova (sem tratar como suspeita).", motivos=motivos,
                           alternativas=c, custo_esperado=c["supervisor"], p_verdadeira=p, requer_humano=True,
                           intencao_resposta="acolher calorosamente e informar que a solução está sendo providenciada")
        motivos.append("cliente novo com evidência positiva, dentro do teto e sem resolução automática anterior")

    if c["executar"] <= c["supervisor"]:
        motivos.append(f"executar (R$ {c['executar']:.2f}) custa menos que levar à supervisão (R$ {c['supervisor']:.2f})")
        return Decisao(**base, caminho="executar", passos=av.passos, resumo=av.resumo, motivos=motivos,
                       alternativas=c, custo_esperado=c["executar"], p_verdadeira=p,
                       intencao_resposta="confirmar a solução concreta e o prazo")

    motivos.append(f"levar à supervisão (R$ {c['supervisor']:.2f}) custa menos que executar (R$ {c['executar']:.2f})")
    if v.contra:  # há evidência verificável para sinalizar
        return Decisao(**base, caminho="sinalizar_suspeita", passos=_passos_suspeita(f, v, bool(v.grupo_suspeito)),
                       resumo="Custo esperado indica revisão: há sinais contra, supervisor decide.", motivos=motivos,
                       alternativas=c, custo_esperado=c["supervisor"], p_verdadeira=p, requer_humano=True,
                       intencao_resposta="informar que a solicitação está em análise, sem acusar")
    return Decisao(**base, caminho="solicitar_aprovacao", passos=_passos_aprovacao(av, "valor alto para a incerteza"),
                   resumo="Custo esperado indica revisão humana, sem sinal de fraude: aprovação do supervisor.",
                   motivos=motivos, alternativas=c, custo_esperado=c["supervisor"], p_verdadeira=p,
                   requer_humano=True, intencao_resposta="informar que a solução foi encaminhada e o prazo de retorno")


def decidir_ticket(amb: Ambiente, conversation_id: str, categoria: str, order_id: str | None = None,
                   **kw_politica: Any) -> Decisao:
    """Atalho: coleta fatos -> política -> veracidade -> decisão."""
    f = coletar_fatos(amb, conversation_id, order_id)
    av = avaliar(categoria, f, **kw_politica)
    v = avaliar_veracidade(amb, f, categoria)
    return decidir(amb, f, av, v)

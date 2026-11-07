"""A semana simulada: avança o relógio de hora em hora (tique 0 = 02/11 08:00 até o tique 159 = 08/11 23:00). (D-027)

O que muda com o tempo nos dados (conferido): 330 pedidos têm a data prometida vencendo durante a semana e 5 chamados
com transportadora têm o prazo de resposta vencendo. Não há mensagens, fotos nem eventos de rastreio novos, e os 273
pedidos criados na semana nunca são separados (o ambiente não simula separação): por isso o loop NÃO trata pedido
novo sem separação como atraso (seria incidente falso).

A cada tique:
- prazos: pendência chegando a 75% do prazo -> alerta (alta); vencida -> alerta (crítica); chamado com transportadora
  sem resposta no prazo -> aciona logística. Cada alerta acontece uma vez.
- às 08:00 de cada dia: o analista trata de novo os incidentes conhecidos (novo limite diário de avisos, chamados
  pendentes). Não redetecta atraso: os dados não evoluem entregas, e todo pedido em trânsito pareceria atrasado (D-027).
Tudo vai para Estado.eventos (exportado como eventos_semana.csv).
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from . import dados
from .ferramentas import Ambiente

ATOR = "agente_ia"


def _duracao(horas: float) -> str:
    return f"{horas:.0f}h" if horas >= 1 else f"{horas * 60:.0f} min"


def _alertar(amb: Ambiente, area: str, urgencia: str, assunto: str, contexto: str, por_que: str) -> None:
    atend = amb.como("atendente", ATOR)
    atend.acionar_pessoa(area=area, urgencia=urgencia, assunto=assunto, contexto=contexto,
                         trilha={"por_que": por_que, "origem": "regra (semana simulada)"})


def verificar_prazos(amb: Ambiente) -> None:
    agora = pd.Timestamp(amb.agora)
    for p in amb.estado.pendencias_abertas():
        if not p["prazo"]:
            continue
        prazo, criado = pd.Timestamp(p["prazo"]), pd.Timestamp(p["criado_em"])
        restante_h = (prazo - agora).total_seconds() / 3600
        total_h = max((prazo - criado).total_seconds() / 3600, 1)
        if restante_h <= 0 and "vencida" not in p["alertas"]:
            p["alertas"].append("vencida")
            _alertar(amb, "supervisão", "critica", f"VENCIDA: {p['pendencia_id']} ({p['tipo']}) — {p['conversation_id']}",
                     f"{p['resumo']}. Prazo era {p['prazo']}.", "pendência passou do prazo sem decisão")
            amb.estado.registrar_evento("prazo_vencido", f"{p['pendencia_id']} ({p['tipo']}) venceu sem decisão",
                                        pendencia_id=p["pendencia_id"])
        elif 0 < restante_h <= 0.25 * total_h and "75%" not in p["alertas"]:
            p["alertas"].append("75%")
            _alertar(amb, "supervisão", "alta", f"Prazo acabando: {p['pendencia_id']} ({p['tipo']}) — {p['conversation_id']}",
                     f"{p['resumo']}. Restam {_duracao(restante_h)} (prazo {p['prazo']}).",
                     "pendência com 75% do prazo consumido")
            amb.estado.registrar_evento("prazo_75", f"{p['pendencia_id']} ({p['tipo']}): restam {_duracao(restante_h)}",
                                        pendencia_id=p["pendencia_id"])

    alertados: set[str] = amb.estado._seq.setdefault("chamados_alertados", set())
    hist = dados.tabela("chamados_transportadora")
    hist = hist[(hist["status"] != "encerrado") & hist["data_resposta"].isna()][["chamado_id", "order_id", "transportadora",
                                                                                   "prazo_resposta"]]
    chamados = dados.registros(hist) + [c for c in amb.estado.chamados if c["status"] == "aberto"]
    vencidos = [ch for ch in chamados if ch["chamado_id"] not in alertados
                and pd.Timestamp(ch["prazo_resposta"]) <= agora]
    # um alerta por transportadora por tique (72 chamados vencendo juntos não viram 72 mãos levantadas)
    for transp in sorted({ch["transportadora"] for ch in vencidos}):
        grupo = [ch for ch in vencidos if ch["transportadora"] == transp]
        alertados.update(ch["chamado_id"] for ch in grupo)
        ids = ", ".join(ch["chamado_id"] for ch in grupo[:10]) + (" ..." if len(grupo) > 10 else "")
        _alertar(amb, "logística", "alta", f"{transp}: {len(grupo)} chamados sem resposta no prazo",
                 f"chamados vencidos: {ids}", "chamados com transportadora passaram do prazo de resposta")
        amb.estado.registrar_evento("chamados_sem_resposta", f"{transp}: {len(grupo)} chamados sem resposta no prazo",
                                    quantidade=len(grupo))


def simular(amb: Ambiente, ate_tique: int = dados.ULTIMO_TIQUE, com_analista: bool = True) -> list[dict[str, Any]]:
    """Avança do tique atual até `ate_tique`. Retorna os eventos gerados nesse intervalo."""
    from . import analista
    inicio = len(amb.estado.eventos)
    for t in range(amb.tique + 1, ate_tique + 1):
        amb.tique = t
        verificar_prazos(amb)
        if com_analista and t % 24 == 0:  # 08:00 de cada dia
            antes = len(amb.estado.notificacoes)
            rel = analista.varrer(amb, tique_deteccao=0)
            novos = len(amb.estado.notificacoes) - antes
            amb.estado.registrar_evento(
                "analista_diario", f"varredura das 08:00: {len(rel.tratamentos)} incidentes, {novos} novos avisos",
                novos_avisos=novos)
    return amb.estado.eventos[inicio:]


def resumo(eventos: list[dict[str, Any]], amb: Ambiente) -> str:
    L = ["# Semana simulada", "",
         f"De 02/11 08:00 até {amb.estado.agora_txt()} (tique {amb.tique}) · {len(eventos)} eventos · "
         f"pendências ainda abertas: {len(amb.estado.pendencias_abertas())}", "",
         "| Quando | Evento | Descrição |", "|---|---|---|"]
    L += [f"| {e['data_hora']} | {e['tipo']} | {e['descricao']} |" for e in eventos]
    return "\n".join(L) + "\n"

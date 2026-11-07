"""Fecha o ciclo da mão levantada (pendência -> decisão humana -> retomada) e a semana simulada. (D-027)

Rodar:  .\\venv\\Scripts\\python.exe -m pytest tests\\test_supervisor_semana.py -q
IA simulada (mesmos falsos de test_agente.py): não chama o proxy.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src import agente as A  # noqa: E402
from src import semana as SEM  # noqa: E402
from src import supervisor as S  # noqa: E402
from src.ferramentas import Ambiente  # noqa: E402
from test_agente import classificador_falso, juiz_falso, redator_falso  # noqa: E402


@pytest.fixture
def amb(tmp_path):
    a = Ambiente()
    A.rodar(["reclame_aqui"], amb=a, pasta_saida=tmp_path, classificar=classificador_falso, redigir=redator_falso,
            julgar=juiz_falso)
    return a


def pendente(amb, tipo):
    return amb.estado.pendencias_abertas(tipo)[0]


def test_agentes_registram_pendencias_com_prazo(amb):
    tipos = {p["tipo"] for p in amb.estado.pendencias}
    assert {"aprovacao", "publicacao", "cosmetovigilancia", "decisao_negocio"} <= tipos
    assert pendente(amb, "cosmetovigilancia")["prazo"] == "2026-11-02 11:02:29"   # 24h do 1º relato
    assert pendente(amb, "aprovacao")["prazo"]                                      # herda o SLA do ticket


def test_aprovar_retoma_e_executa(amb):
    p = pendente(amb, "aprovacao")
    r = S.aprovar(amb, p["pendencia_id"], comentario="arrependimento no prazo")
    assert r["ok"] and r["mensagem"]["ok"] and "155,81" in r["mensagem"]["texto"]
    reemb = [a for a in amb.estado.acoes if a["acao"] == "reembolsar"]
    assert reemb and reemb[-1]["aprovado_por"] == S.SUPERVISOR_PADRAO and reemb[-1]["executado_por"] == S.ATOR_ATENDENTE
    assert amb.estado.pendencia(p["pendencia_id"])["status"] == "resolvida"
    with pytest.raises(ValueError):
        S.aprovar(amb, p["pendencia_id"])  # não resolve duas vezes


def test_rejeitar_responde_neutro_sem_executar(amb):
    r = S.rejeitar(amb, pendente(amb, "aprovacao")["pendencia_id"], "sem coleta reversa possível")
    assert r["ok"] and "não foi possível seguir com o reembolso" in r["mensagem"]["texto"]
    assert not [a for a in amb.estado.acoes if a["acao"] == "reembolsar"]
    assert "coleta" not in r["mensagem"]["texto"]  # justificativa interna não vai ao cliente


def test_liberar_publicacao_retida_e_texto_editado_passa_no_validador(amb):
    p = pendente(amb, "publicacao")  # AT116568: orientação de saúde retida (caso sensível)
    ruim = S.liberar(amb, p["pendencia_id"], texto="Ligue para (11) 98765-4321 e ganhe um cupom.")
    assert not ruim["ok"] and "validador" in ruim["erro"]
    r = S.liberar(amb, p["pendencia_id"], texto="Sentimos muito. Suspenda o uso e procure atendimento médico; "
                                                 "um especialista já está cuidando do seu caso.")
    assert r["ok"] and r["retidos_executados"][0]["ferramenta"] == "orientar_suspensao_uso"
    assert [m for m in amb.estado.mensagens if m["conversation_id"] == "AT116568"]


def test_registrar_cosmetovigilancia_no_prazo(amb):
    r = S.registrar_cosmetovigilancia(amb, pendente(amb, "cosmetovigilancia")["pendencia_id"])
    assert r["ok"] and r["dentro_do_prazo"] and amb.estado.cosmetovigilancia[-1]["sku"] == "RTU-5007"


def test_decisao_de_negocio(amb):
    a, b = amb.estado.pendencias_abertas("decisao_negocio")[:2]
    assert S.decidir_negocio(amb, a["pendencia_id"], True, "a falha foi nossa")["enviados"] > 0
    antes = len(amb.estado.notificacoes)
    S.decidir_negocio(amb, b["pendencia_id"], False, "mantém a política")
    assert len(amb.estado.notificacoes) == antes


def test_suspeita_procedente_retoma_sem_sinalizar_de_novo(amb):
    t = next(t for t in A.carregar_tickets(amb, ["reclame_aqui"]) if t.conversation_id == "AT116556")
    t.classificacao = classificador_falso(amb.ler_conversa("AT116556"))
    atend = amb.como("atendente", S.ATOR_ATENDENTE)
    with atend.atendendo("AT116556"):
        atend.sinalizar_suspeita(conversation_id="AT116556", evidencias="teste", trilha={"por_que": "teste"})
    pid = amb.estado.abrir_pendencia("suspeita", conversation_id="AT116556", resumo="teste", _ticket=t)
    r = S.decidir_suspeita(amb, pid, True, "foto conferida", redigir=redator_falso, julgar=juiz_falso)
    assert r["ok"] and r["reprocesso"].decisao.caminho != "sinalizar_suspeita"
    assert not amb.estado.pendencias_abertas("suspeita")


def test_semana_cobra_prazos_uma_vez_e_agrupa_chamados(amb):
    eventos = SEM.simular(amb)
    assert amb.tique == 159
    aprov = amb.estado.pendencia(pendente(amb, "aprovacao")["pendencia_id"])
    assert aprov["alertas"] == ["75%", "vencida"]                          # cada alerta uma vez
    chamados = [e for e in eventos if e["tipo"] == "chamados_sem_resposta"]
    assert chamados and len(chamados) == len({(e["tique"], e["descricao"].split(":")[0]) for e in chamados})
    diarios = [e for e in eventos if e["tipo"] == "analista_diario"]
    assert len(diarios) == 6 and len(amb.estado.incidentes) == 4          # sem incidente duplicado na semana

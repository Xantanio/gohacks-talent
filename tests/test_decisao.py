"""Testes da decisão por custo esperado (decisao.py).

Rodar:  .\\venv\\Scripts\\python.exe -m pytest tests\\test_decisao.py -q
Na fila do Reclame Aqui de hoje nenhum caso chega ao cálculo de custo (todos caem antes numa regra da política);
os casos com foto são montados a partir de tickets reais.
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import decisao as D  # noqa: E402
from src import politicas as Pol  # noqa: E402
from src import veracidade as V  # noqa: E402
from src.ferramentas import Ambiente  # noqa: E402

HASH_DA_INTERNET = "9aa32ada3d19b04b"  # IMG000021/IMG000188/IMG000292: 3 clientes diferentes


def avaria(amb, **mudancas) -> Pol.Fatos:
    """AT116700 (body mist vazando, foto coerente) sem a reposição que já existe para o pedido."""
    f = replace(Pol.coletar_fatos(amb, "AT116700"), trocas_pedido=[])
    return replace(f, **mudancas) if mudancas else f


def decidir(amb, f, categoria="avaria_vazamento", v=None):
    av = Pol.avaliar(categoria, f)
    return D.decidir(amb, f, av, v or V.avaliar_veracidade(amb, f, categoria))


def como_novo(amb, monkeypatch, trocas=()):
    real = amb.consultar_cliente

    def falso(customer_id):
        r = real(customer_id)
        r["cliente"] = {**r["cliente"], "segmento": "novo", "n_pedidos": 1, "receita_acumulada": 95.0}
        r["trocas_reenvios"] = list(trocas)
        return r
    monkeypatch.setattr(amb, "consultar_cliente", falso)


# ---- valor do cliente e custos ----------------------------------------------

def test_ltv_por_faixa_de_receita():
    fiel = D.valor_do_cliente({"customer_id": "A", "segmento": "fiel", "n_pedidos": 5, "receita_acumulada": 620.0})
    assert fiel.ltv == pytest.approx(620 * 0.28 / 0.72, abs=0.01) and not fiel.novo
    vip = D.valor_do_cliente({"customer_id": "B", "segmento": "vip", "n_pedidos": 15, "receita_acumulada": 1909.0})
    assert vip.taxa_recompra == 0.70 and vip.ltv == pytest.approx(1909 * 0.7 / 0.3, abs=0.01)
    assert vip.custo_negar_verdadeira > fiel.custo_negar_verdadeira  # VIP: negar é mais caro


def test_novo_usa_ltv_potencial():
    novo = D.valor_do_cliente({"customer_id": "C", "segmento": "novo", "n_pedidos": 1, "receita_acumulada": 85.0})
    assert novo.novo and novo.ltv == pytest.approx(85 * 2.0) and novo.ltv > novo.ltv_historico
    assert "potencial" in novo.criterio


def test_formula_dos_custos():
    vc = D.valor_do_cliente({"customer_id": "A", "segmento": "fiel", "n_pedidos": 5, "receita_acumulada": 620.0})
    c = D.custos(0.9, 100.0, vc)
    assert c["executar"] == pytest.approx(0.1 * 100 * 0.45)
    assert c["supervisor"] == pytest.approx(6 + 0.9 * 0.10 * vc.custo_negar_verdadeira, abs=0.01)
    assert c["negar (não permitido ao agente)"] == pytest.approx(0.9 * vc.custo_negar_verdadeira, abs=0.01)


# ---- a política manda primeiro ----------------------------------------------

def test_politica_manda_primeiro_nos_tickets_do_reclame_aqui():
    amb = Ambiente()
    d = D.decidir_ticket(amb, "AT116524", "erro_personalizacao")  # reposição já aberta
    assert d.caminho == "seguir_politica" and [p.ferramenta for p in d.passos] == ["responder_cliente"]
    d = D.decidir_ticket(amb, "AT116520", "arrependimento_cancelamento")  # R$ 155,81 > alçada
    assert d.caminho == "seguir_politica" and d.requer_humano
    assert [p.ferramenta for p in d.passos] == ["solicitar_aprovacao", "reembolsar"]
    d = D.decidir_ticket(amb, "AT116568", "reacao_adversa")
    assert d.requer_humano and d.passos[0].ferramenta == "orientar_suspensao_uso"
    d = D.decidir_ticket(amb, "AT116614", "item_faltante", sku_alvo="GBT-9001")  # brinde no pedido (D-024)
    assert [p.ferramenta for p in d.passos] == ["solicitar_foto"]


# ---- dinheiro com base em foto ----------------------------------------------

def test_reclamacao_coerente_de_cliente_fiel_executa():
    amb = Ambiente()
    d = decidir(amb, avaria(amb))
    assert d.caminho == "executar" and [p.ferramenta for p in d.passos] == ["reenviar_produto"]
    assert d.alternativas["executar"] < d.alternativas["supervisor"]
    assert not d.requer_humano


def test_foto_da_internet_sinaliza_sempre():
    amb = Ambiente()
    f = avaria(amb)
    falsa = {**f.anexos[0], "anexo_id": "IMG-TESTE", "hash_perceptual": HASH_DA_INTERNET,
             "descricao_visual": "Body Mist Baunilha 200ml com o pote rachado."}  # descrição até coerente
    d = decidir(amb, replace(f, anexos=[falsa]))
    assert d.caminho == "sinalizar_suspeita" and d.requer_humano
    assert [p.ferramenta for p in d.passos] == ["sinalizar_suspeita", "responder_cliente"]
    assert "IMG000" in d.passos[0].params["evidencias"]  # evidência verificável: os anexos iguais
    assert "acus" in d.passos[1].nota  # nunca acusar


def test_novo_dentro_do_teto_com_evidencia_executa(monkeypatch):
    amb = Ambiente()
    como_novo(amb, monkeypatch)
    d = decidir(amb, avaria(amb))
    assert d.caminho == "executar" and d.cliente.novo


def test_novo_acima_do_teto_vai_para_pessoa_sem_suspeita(monkeypatch):
    amb = Ambiente()
    como_novo(amb, monkeypatch)
    f = avaria(amb)
    d = decidir(amb, replace(f, pedido={**f.pedido, "valor_produtos": 180.0}))
    assert d.caminho == "solicitar_aprovacao"
    assert [p.ferramenta for p in d.passos] == ["solicitar_aprovacao", "reenviar_produto"]
    assert "teto" in d.resumo and "sinalizar_suspeita" not in [p.ferramenta for p in d.passos]
    # e a ferramenta aceita o pedido de aprovação abaixo da alçada (D-016)
    with amb.atendendo("AT116700"):
        r = amb.chamar("solicitar_aprovacao", trilha=d.trilha(), **d.passos[0].params)
    assert r["ok"], r


def test_novo_segunda_resolucao_vai_para_pessoa(monkeypatch):
    amb = Ambiente()
    anterior = {"solicitacao_id": "OMS000999", "conversation_id": "AT000001", "order_id_original": "GBH000001",
                "tipo": "reenvio", "motivo_registrado": "avaria", "data_solicitacao": "2026-09-01 10:00:00",
                "solicitado_por": "atendente_01", "aprovado_por": None, "status": "entregue", "custo_total": 40.0}
    como_novo(amb, monkeypatch, trocas=[anterior])
    d = decidir(amb, avaria(amb))
    assert d.caminho == "solicitar_aprovacao" and "OMS000999" in d.resumo


def test_custo_alto_com_sinais_fracos_sinaliza_e_sem_sinais_pede_aprovacao():
    amb = Ambiente()
    f = avaria(amb)
    fraco = V.Sinal("muitas_trocas_12m", "historico", 2.5, "n_trocas_12m = 6")
    incerta = V.Veracidade(p_verdadeira=0.5, prior_falsa=0.07, sinais=[fraco], tem_foto=True)
    d = decidir(amb, f, v=incerta)
    assert d.alternativas["supervisor"] < d.alternativas["executar"]
    assert d.caminho == "sinalizar_suspeita"  # há evidência verificável para sinalizar
    sem_sinais = V.Veracidade(p_verdadeira=0.5, prior_falsa=0.07, sinais=[], tem_foto=True)
    d = decidir(amb, f, v=sem_sinais)
    assert d.caminho == "solicitar_aprovacao"  # sem evidência não se sinaliza: pede aprovação


# ---- trilha e execução --------------------------------------------------------

def test_trilha_completa_e_passos_executaveis():
    amb = Ambiente()
    d = decidir(amb, avaria(amb))
    t = d.trilha()
    assert t["por_que"] and t["regra"] == "categorias.avaria_vazamento" and t["origem"] == "regra"
    assert set(t["alternativas"]) >= {"executar", "supervisor"} and 0 < t["p_verdadeira"] <= 1
    assert any(e.startswith("decisão:") for e in t["evidencias"])
    assert any("LTV" in e for e in t["evidencias"])
    with amb.atendendo(d.conversation_id):
        r = amb.chamar(d.passos[0].ferramenta, trilha=t, **d.passos[0].params)
    assert r["ok"], r
    assert amb.estado.acoes[-1]["trilha"]["alternativas"] == d.alternativas

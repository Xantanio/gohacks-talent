"""Testes da política determinística (politicas.py) com tickets reais da fila e casos-limite sintéticos.

Rodar:  .\\venv\\Scripts\\python.exe -m pytest tests\\test_politicas.py -q
"""
from __future__ import annotations

import sys
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import politicas as P  # noqa: E402
from src.ferramentas import Ambiente  # noqa: E402


@pytest.fixture(scope="module")
def amb() -> Ambiente:
    return Ambiente()


def avaliar(amb: Ambiente, cid: str, categoria: str, **kw) -> P.Avaliacao:
    return P.avaliar(categoria, P.coletar_fatos(amb, cid), **kw)


def ferramentas(a: P.Avaliacao) -> list[str]:
    return [p.ferramenta for p in a.passos]


# ---- leitura de politicas.csv -----------------------------------------------

def test_numeros_vem_do_csv():
    assert P.ALCADA_ATENDENTE["reembolsar"] == 100.0 and P.ALCADA_ATENDENTE["reenviar_produto"] == 200.0
    assert (P.DIAS_UTEIS_RASTREIO_PARADO, P.PIX_MINUTOS, P.BOLETO_DIAS_UTEIS) == (3, 30, 2)
    assert P.regra("avaria_vazamento", "prazo_dias_corridos_do_recebimento") == 7
    assert len(P.TAGS_OFICIAIS) == 13 and P.tag_da_categoria("erro_personalizacao") in P.TAGS_OFICIAIS
    assert all(P.tag_da_categoria(c) in P.TAGS_OFICIAIS for c in P.CATEGORIAS)


def test_dias_uteis_com_feriado():
    assert date(2026, 11, 2) in P.feriados()
    assert P.dias_uteis_entre(date(2026, 10, 30), date(2026, 11, 2)) == 0   # sáb, dom, feriado
    assert P.dias_uteis_entre(date(2026, 10, 30), date(2026, 11, 3)) == 1
    assert P.dias_uteis_entre(date(2026, 11, 19), date(2026, 11, 23)) == 1  # 20/11 (sexta) é feriado; só seg 23
    assert P.dias_uteis_entre(date(2026, 11, 5), date(2026, 11, 1)) == 0


# ---- tickets reais ----------------------------------------------------------

def test_endereco_antes_da_separacao(amb):
    a = avaliar(amb, "AT116563", "alteracao_endereco")
    assert a.situacao == "elegivel" and ferramentas(a) == ["alterar_endereco"]


def test_brinde_faltante_sem_foto_pede_foto(amb):
    a = avaliar(amb, "AT116501", "item_faltante", sku_alvo="GBT-9001")
    assert a.situacao == "falta_informacao" and ferramentas(a) == ["solicitar_foto"]
    assert a.valor_acao == 24.90  # brinde vale o custo unitário
    assert any("campanha" in f for f in a.fatos)


def test_reposicao_ja_aberta_nao_repete_o_gasto(amb):
    # AT116524: a nova produção OMS000651 já foi aberta em 30/10 pela conversa anterior AT116157
    a = avaliar(amb, "AT116524", "erro_personalizacao")
    assert a.situacao == "em_andamento" and ferramentas(a) == ["responder_cliente"]
    assert any("OMS000651" in f for f in a.fatos) and not a.envolve_dinheiro


def test_reposicao_entregue_e_nova_reclamacao_escala(amb):
    f = P.coletar_fatos(amb, "AT116524")
    f = replace(f, trocas_pedido=[{**f.trocas_pedido[0], "status": "entregue"}])
    a = P.avaliar("erro_personalizacao", f)
    assert a.situacao == "escalar" and ferramentas(a) == ["acionar_pessoa"]


def test_personalizacao_com_foto_e_prazo_contado_do_primeiro_contato(amb):
    f = replace(P.coletar_fatos(amb, "AT116524"), trocas_pedido=[])  # mesmo caso, sem a reposição já aberta
    a = P.avaliar("erro_personalizacao", f)
    assert a.situacao == "elegivel" and ferramentas(a) == ["reimprimir_personalizacao"]
    assert a.passos[0].params["personalizacao_correta"] == "C.T."
    assert a.prazo["primeiro_relato"].startswith("2026-10-29")  # conversa anterior AT116157, não a de 31/10
    assert not a.acima_da_alcada


def test_duplicidade_acima_da_alcada_pede_aprovacao(amb):
    a = avaliar(amb, "AT116677", "cobranca_duplicada")
    assert a.situacao == "elegivel" and a.acima_da_alcada and a.valor_acao == 118.80
    assert ferramentas(a) == ["solicitar_aprovacao", "reembolsar"]


def test_reacao_adversa_e_sempre_escalada_com_prazo(amb):
    a = avaliar(amb, "AT116568", "reacao_adversa")
    assert a.situacao == "escalar" and ferramentas(a)[:2] == ["orientar_suspensao_uso", "acionar_pessoa"]
    assert a.prazo["horas_decorridas"] == 21.0 and a.escalar["urgencia"] == "critica"
    troca = a.passos[2]
    assert troca.ferramenta == "trocar_produto" and troca.opcional  # troca não substitui o registro


def test_atraso_acima_de_7_dias_uteis_oferece_reenvio_ou_reembolso(amb):
    a = avaliar(amb, "AT116538", "atraso_entrega")
    assert a.situacao == "elegivel" and len(a.alternativas) == 2
    assert [p.ferramenta for p in a.alternativas[0]] == ["reenviar_produto"]
    assert [p.ferramenta for p in a.alternativas[1]] == ["solicitar_aprovacao", "reembolsar"]


def test_atraso_curto_com_chamado_aberto_so_informa(amb):
    a = avaliar(amb, "AT116486", "atraso_entrega")
    assert ferramentas(a) == ["informar_status"] and any("chamado já aberto" in f for f in a.fatos)


def test_arrependimento_dentro_e_fora_do_prazo(amb):
    dentro = avaliar(amb, "AT116520", "arrependimento_cancelamento")
    assert dentro.situacao == "elegivel" and dentro.acima_da_alcada and "combinar coleta reversa" in dentro.pendencias
    fora = avaliar(amb, "AT116705", "arrependimento_cancelamento")
    assert fora.situacao == "nao_elegivel" and ferramentas(fora) == ["negar_por_politica"]


def test_troca_de_fragrancia(amb):
    f = P.coletar_fatos(amb, "AT116679")
    assert P.avaliar("troca_fragrancia", f).situacao == "falta_informacao"
    assert P.avaliar("troca_fragrancia", f, uso_pct=50).situacao == "nao_elegivel"
    ok = P.avaliar("troca_fragrancia", f, uso_pct=10)
    assert ok.situacao == "elegivel" and ferramentas(ok) == ["trocar_produto"]


def test_avaria_com_reenvio_em_transito_so_informa(amb):
    a = avaliar(amb, "AT116700", "avaria_vazamento")  # OMS000587 em trânsito desde 26/10
    assert a.situacao == "em_andamento" and any("OMS000587" in f for f in a.fatos)


def avaria_sem_reposicao(amb) -> P.Fatos:
    """AT116700 sem a reposição já aberta: base para os casos-limite de avaria."""
    return replace(P.coletar_fatos(amb, "AT116700"), trocas_pedido=[])


def test_avaria_com_foto_de_conversa_anterior(amb):
    a = P.avaliar("avaria_vazamento", avaria_sem_reposicao(amb))
    assert a.situacao == "elegivel" and ferramentas(a) == ["reenviar_produto"]
    assert any("IMG000321" in f for f in a.fatos)


def test_sem_interacao_encerra(amb):
    assert ferramentas(avaliar(amb, "AT116664", "sem_interacao")) == ["encerrar_conversa"]


def test_trilha_pronta_para_as_ferramentas():
    amb = Ambiente()  # isolado: este teste executa um reenvio de verdade no estado
    a = P.avaliar("avaria_vazamento", avaria_sem_reposicao(amb))
    t = a.trilha(p_verdadeira=0.9)
    assert t["por_que"] and t["regra"] == "categorias.avaria_vazamento" and t["origem"] == "regra"
    assert t["evidencias"] == a.fatos and t["p_verdadeira"] == 0.9
    # e a ferramenta aceita a trilha e executa o passo sugerido
    with amb.atendendo("AT116700"):
        r = amb.chamar(a.passos[0].ferramenta, trilha=t, **a.passos[0].params)
    assert r["ok"], r


# ---- casos-limite sintéticos (a partir de fatos reais) ----------------------

def test_avaria_em_pedido_nao_entregue_nao_se_aplica(amb):
    f = avaria_sem_reposicao(amb)
    f = replace(f, pedido={**f.pedido, "status_entrega": "em_transito", "data_entrega": None})
    a = P.avaliar("avaria_vazamento", f)
    assert a.situacao == "nao_elegivel" and "não entregue" in a.resumo


def test_avaria_fora_do_prazo_nega(amb):
    f = avaria_sem_reposicao(amb)
    f = replace(f, primeiro_relato=datetime(2026, 10, 30, 10, 0))  # 10 dias após a entrega
    a = P.avaliar("avaria_vazamento", f)
    assert a.situacao == "nao_elegivel" and ferramentas(a) == ["negar_por_politica"]


def test_foto_anterior_a_entrega_nao_conta(amb):
    f = avaria_sem_reposicao(amb)
    f = replace(f, anexos=[{**f.anexos[0], "enviado_em": "2026-10-01 10:00:00"}])
    assert P.avaliar("avaria_vazamento", f).situacao == "falta_informacao"


def test_avaria_cara_pede_aprovacao(amb):
    f = avaria_sem_reposicao(amb)
    f = replace(f, pedido={**f.pedido, "valor_produtos": 350.0, "valor": 362.9})
    a = P.avaliar("avaria_vazamento", f)
    assert a.acima_da_alcada and ferramentas(a) == ["solicitar_aprovacao", "reenviar_produto"]


def test_brinde_nao_devido_abaixo_do_minimo(amb):
    f = P.coletar_fatos(amb, "AT116501")
    f = replace(f, pedido={**f.pedido, "valor_produtos": 99.9}, itens=[i for i in f.itens if i["tipo_item"] != "brinde"])
    a = P.avaliar("item_faltante", f, sku_alvo="GBT-9001")
    assert a.situacao == "nao_elegivel" and "abaixo do mínimo" in a.resumo


def test_endereco_depois_da_separacao_nega(amb):
    f = P.coletar_fatos(amb, "AT116563")
    f = replace(f, pedido={**f.pedido, "ja_separado": True, "data_separacao": "2026-11-01 10:00:00"})
    assert ferramentas(P.avaliar("alteracao_endereco", f)) == ["negar_por_politica"]


def test_sem_pedido_pede_numero(amb):
    f = replace(P.coletar_fatos(amb, "AT116597"), pedido=None)
    a = P.avaliar("avaria_vazamento", f)
    assert a.situacao == "falta_informacao" and "número do pedido" in a.pendencias


def test_categoria_invalida():
    with pytest.raises(ValueError):
        P.avaliar("cupom_de_compensacao", None)  # não existe na política

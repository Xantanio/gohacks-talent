"""Testes do agente analista (D-025/D-026): detecção com taxa e janela, aviso proativo com travas, passagem do atendente.

Rodar:  .\\venv\\Scripts\\python.exe -m pytest tests\\test_analista.py -q
Não usa IA: a detecção é determinística e o texto do aviso é fixo (validado pelo mesmo validador das respostas).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import analista as AN  # noqa: E402
from src import dados, llm  # noqa: E402
from src.ferramentas import CATALOGO, Ambiente  # noqa: E402


@pytest.fixture(scope="module")
def candidatos():
    cands, notas = AN.detectar(0)
    return {c.tipo: c for c in cands}, list(notas)


def analista() -> Ambiente:
    return Ambiente(papel="analista", ator=AN.ATOR_ANALISTA)


# ---- detecção -------------------------------------------------------------------

def test_detecta_os_incidentes_reais(candidatos):
    cands, _ = candidatos
    assert set(cands) == {"maquina", "transportadora_uf", "turno", "gateway"}
    assert "PRN-02" in cands["maquina"].escopo and "Expressa Já" in cands["transportadora_uf"].escopo
    assert "CD2" in cands["turno"].escopo


def test_janelas_vem_das_reclamacoes(candidatos):
    cands, _ = candidatos
    assert cands["maquina"].janela == ("20/10", "28/10")   # inclui a semana de 26/10 (extensão do incidente)
    assert cands["turno"].janela == ("14/10", "24/10")     # 26/10 já volta ao normal e fica de fora
    assert len(cands["maquina"].monitorar) == 13          # impressos depois de 28/10: sem dado, só monitorar


def test_afetados_nao_incluem_quem_ja_reclamou(candidatos):
    for c in candidatos[0].values():
        assert not set(c.afetados) & set(c.reclamaram)


def test_sem_falso_incidente_de_lote(candidatos):
    cands, notas = candidatos
    assert "lote" not in cands  # D-025: o lote do pedido apontava LT2608-F, mas quem vazou era outro produto
    assert any(n.startswith("lotes:") and "sem incidente" in n for n in notas)
    assert any(n.startswith("PRN-01") and "sem incidente" in n for n in notas)  # sabe dizer "não há incidente"


def test_textos_de_aviso_passam_no_validador(candidatos):
    for c in candidatos[0].values():
        if c.texto_aviso:
            permitidas = {"reimprimir_personalizacao", "reenviar_produto"} if c.exige_prazo_reposicao else set()
            v = llm.validar_resposta(c.texto_aviso, publica=True, ferramentas_permitidas=permitidas,
                                     transportadora=c.transportadora, fatos=[" ".join(f"{d}/2026" for d in c.janela or ())])
            assert v.ok, (c.tipo, v.motivos)


# ---- tratamento -------------------------------------------------------------------

def test_tratar_abre_incidente_avisa_e_respeita_prazo(candidatos):
    an = analista()
    c = candidatos[0]["maquina"]
    t = AN.tratar(an, c)
    assert t.incidente_id and an.estado.incidentes[0]["area_responsavel"] == "produção"
    avisados = {n["order_id"] for n in an.estado.notificacoes}
    assert len(avisados) == t.notificados > 0 and avisados <= set(c.afetados)
    assert not avisados & set(t.fora_do_prazo)              # fora dos 7 dias não recebe a promessa
    clis = dados.tabela("clientes").set_index("customer_id")
    for n in an.estado.notificacoes:                        # consentimento (LGPD)
        assert clis.loc[n["customer_id"], f"aceita_comunicacao_{n['canal']}"] == "sim"
        assert "GCH" not in n["texto"] and "GBH" not in n["texto"]
    if t.fora_do_prazo:
        assert any("fora do prazo" in m["texto_slack"] for m in an.estado.canal_do_time)


def test_nao_avisa_duas_vezes(candidatos):
    an = analista()
    c = candidatos[0]["turno"]
    primeira = AN.tratar(an, c)
    segunda = AN.tratar(an, c)
    assert segunda.ja_existia and segunda.notificados == 0
    assert len(an.estado.incidentes) == 1 and len(an.estado.notificacoes) == primeira.notificados
    assert segunda.total_avisados == primeira.notificados
    escalacoes = [m for m in an.estado.canal_do_time if "fora do prazo" in m["texto_slack"]]
    assert len(escalacoes) <= 1  # a mão levantada de "fora do prazo" não se repete


def test_transportadora_abre_chamados_sem_duplicar(candidatos):
    an = analista()
    t = AN.tratar(an, candidatos[0]["transportadora_uf"])
    pedidos = [ch["order_id"] for ch in an.estado.chamados]
    assert t.chamados_abertos == len(pedidos) == len(set(pedidos)) > 0
    assert all(ch["aberto_por"] == AN.ATOR_ANALISTA for ch in an.estado.chamados)


def test_limite_diario_de_avisos(candidatos, monkeypatch):
    monkeypatch.setitem(CATALOGO["notificar_clientes"].limites, "max_por_dia", 5)
    an = analista()
    t = AN.tratar(an, candidatos[0]["transportadora_uf"])
    assert t.notificados == 5 and any("limite diário" in m for m in t.ignorados_por_motivo)


# ---- formas de trabalho -------------------------------------------------------------

def test_chamado_pelo_atendente_so_quando_ha_padrao():
    atend = Ambiente()
    # AT116160: Reclame Aqui, Expressa Já / BA, atraso -> pertence ao incidente
    trs = AN.chamado_pelo_atendente(atend, "AT116160", "GBH473217", "atraso_entrega")
    assert len(trs) == 1 and trs[0].candidato.tipo == "transportadora_uf" and trs[0].notificados > 0
    assert atend.estado.incidentes[0]["aberto_por"] == AN.ATOR_ANALISTA  # quem abre é o analista, mesmo estado
    assert "atendente" in atend.estado.incidentes[0]["origem"]
    # categoria não repetível ou pedido fora de padrão: o analista não faz nada
    assert AN.chamado_pelo_atendente(atend, "AT116520", "GBH469163", "arrependimento_cancelamento") == []
    assert AN.chamado_pelo_atendente(atend, "AT116668", "GBH493520", "atraso_entrega") == []


def test_varredura_completa_e_resumo():
    rel = AN.varrer(analista())
    texto = AN.resumo(rel)
    assert len(rel.tratamentos) == 4 and "Verificado sem incidente" in texto
    assert any(t.notificados for t in rel.tratamentos)


# ---- ferramentas ------------------------------------------------------------------------

def test_ferramentas_do_analista():
    an = analista()
    r = an.buscar_conversas(lote="LT2608-F")              # lote POR ITEM (itens_pedido.lote)
    itens = dados.tabela("itens_pedido")
    com_lote = set(itens.loc[itens["lote"] == "LT2608-F", "order_id"])
    assert r["ok"] and all(c["order_id"] in com_lote for c in r["conversas"])
    assert not an.consultar_metricas(agrupar_por="cor")["ok"]
    assert an.consultar_metricas(agrupar_por="transportadora;uf")["grupos"][0]["n_conversas"] > 0
    assert not an.abrir_incidente(tipo="cupom", escopo="x", evidencias="x", trilha={"por_que": "x"})["ok"]
    assert not Ambiente().chamar("abrir_incidente", tipo="maquina", escopo="x", evidencias="x",
                                 trilha={"por_que": "x"})["ok"]  # atendente não abre incidente


def test_buscar_conversas_respeita_limite(monkeypatch):
    monkeypatch.setitem(CATALOGO["buscar_conversas"].limites, "max_resultados", 5)
    r = analista().buscar_conversas(transportadora="Expressa Já")
    assert len(r["conversas"]) == 5 and r["truncado"] and r["total"] > 5

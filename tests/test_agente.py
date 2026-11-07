"""Testes do agente com IA simulada (classificador e redator falsos): roda a fila inteira do Reclame Aqui sem rede.

Rodar:  .\\venv\\Scripts\\python.exe -m pytest tests\\test_agente.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import agente as A  # noqa: E402
from src import llm  # noqa: E402
from src.ferramentas import Ambiente  # noqa: E402

# Classificação conferida à mão (o Jev ao vivo bateu 23/24, D-018).
MANUAL = {"AT116160": "atraso_entrega", "AT116171": "onde_esta_pedido", "AT116473": "atraso_entrega",
          "AT116486": "atraso_entrega", "AT116501": "item_faltante", "AT116507": "atraso_entrega",
          "AT116513": "onde_esta_pedido", "AT116520": "arrependimento_cancelamento", "AT116521": "onde_esta_pedido",
          "AT116524": "erro_personalizacao", "AT116538": "atraso_entrega", "AT116556": "avaria_vazamento",
          "AT116559": "atraso_entrega", "AT116560": "item_faltante", "AT116568": "reacao_adversa",
          "AT116573": "item_faltante", "AT116589": "atraso_entrega", "AT116614": "item_faltante",
          "AT116624": "item_faltante", "AT116647": "item_faltante", "AT116668": "pagamento_pendente",
          "AT116671": "atraso_entrega", "AT116686": "item_faltante", "AT116697": "onde_esta_pedido"}
BRINDE = {"AT116501", "AT116560", "AT116573", "AT116614", "AT116624", "AT116647"}


def classificador_falso(conversa, pedido=None):
    cid = conversa["conversa"]["conversation_id"]
    cat = MANUAL[cid]
    return llm.Classificacao(cat, 0.95, {cat: 0.95}, urgencia=1, irritacao=1, ameaca_externa=0.02,
                             caso_sensivel=0.98 if cat == "reacao_adversa" else 0.02,
                             falta_brinde=0.97 if cid in BRINDE else 0.05, preferencia="nao_disse",
                             origem="teste", modelo="falso")


def redator_falso(mensagens):
    return llm.Redacao(texto="Olá! Sentimos muito pelo transtorno. Já estamos cuidando do seu caso e retornamos em breve.",
                       justificativa="texto de teste", modelo="falso")


@pytest.fixture(scope="module")
def execucao(tmp_path_factory):
    amb = Ambiente()
    resultados, pasta = A.rodar(["reclame_aqui"], usar_ia=True, pasta_saida=tmp_path_factory.mktemp("saida"), amb=amb,
                                classificar=classificador_falso, redigir=redator_falso)
    return amb, {r.ticket.conversation_id: r for r in resultados}, pasta


def test_processa_os_24_tickets_do_reclame_aqui(execucao):
    amb, res, pasta = execucao
    assert len(res) == 24 and set(res) == set(MANUAL)
    assert not [r for r in res.values() if r.erro]
    assert all(r.publicada or r.motivos_retencao for r in res.values())  # todo ticket tem desfecho


def test_toda_acao_tem_trilha_e_nenhuma_falhou(execucao):
    amb, res, _ = execucao
    assert amb.estado.acoes and all(a["trilha"].get("por_que") for a in amb.estado.acoes)
    falhas = [(r.ticket.conversation_id, e) for r in res.values() for e in r.executados if not e["ok"]]
    assert not falhas, falhas


def test_prioridade_coloca_reacao_adversa_no_topo(execucao):
    _, res, _ = execucao
    ordem = sorted(res.values(), key=lambda r: -r.ticket.prioridade)
    assert ordem[0].ticket.conversation_id == "AT116568"


def test_reacao_adversa_fica_retida_e_aciona_supervisao(execucao):
    amb, res, _ = execucao
    r = res["AT116568"]
    assert not r.publicada and any("sensível" in m for m in r.motivos_retencao)
    assert "orientar_suspensao_uso retido para revisão humana" in r.pendentes
    assert any(e["ferramenta"] == "acionar_pessoa" and e["ok"] for e in r.executados)
    assert not [m for m in amb.estado.mensagens if m["conversation_id"] == "AT116568"]  # nada publicado


def test_mensagens_publicas_sem_pedido_nem_rastreio(execucao):
    amb, _, _ = execucao
    publicas = [m for m in amb.estado.mensagens if m["publica"]]
    assert publicas
    from src.ferramentas import mascarar_publico
    for m in publicas:  # nada a mascarar = nenhum pedido completo, rastreio ou dado pessoal no texto publicado
        assert mascarar_publico(m["texto"]) == m["texto"], m["texto"]


def test_casos_chave(execucao):
    _, res, _ = execucao
    feitas = lambda cid: [e["ferramenta"] for e in res[cid].executados if e["ok"]]  # noqa: E731
    assert "solicitar_foto" in feitas("AT116501")                  # brinde sem foto: pede foto
    assert "negar_por_politica" in feitas("AT116614")              # brinde não devido
    assert "solicitar_aprovacao" in feitas("AT116520")             # reembolso acima da alçada
    assert "reembolsar" not in feitas("AT116520")                  # ...e não executa antes da aprovação
    assert any("aprovacao_id" in p for p in res["AT116520"].pendentes)
    assert "abrir_chamado_transportadora" in feitas("AT116507")    # atraso curto sem chamado
    assert "reimprimir_personalizacao" not in feitas("AT116524")   # reposição já aberta (D-016)
    assert res["AT116524"].resposta_origem == "llm" and res["AT116524"].publicada


def test_dossie_e_exportacao(execucao):
    _, res, pasta = execucao
    d = (pasta / "dossies" / "AT116520.md").read_text(encoding="utf-8")
    for secao in ("## 1. O que o cliente escreveu", "## 3. Fatos", "## 4. Política", "## 6. Decisão", "## 7. Ações"):
        assert secao in d
    assert "GBH469163" not in d  # dossiê do Reclame Aqui também mascara o pedido
    assert (pasta / "resumo.md").exists() and (pasta / "acoes_executadas.csv").exists()
    assert (pasta / "canal_do_time.md").read_text(encoding="utf-8").count("[") > 0


def test_sem_ia_tudo_vai_para_revisao(tmp_path):
    resultados, _ = A.rodar(["reclame_aqui"], limite=3, usar_ia=False, pasta_saida=tmp_path)
    assert all(not r.publicada and r.motivos_retencao for r in resultados)


def test_falha_da_ia_nao_publica(tmp_path):
    def falha(conversa, pedido=None):
        return llm.Classificacao(None, revisao_humana=True, erro="HTTP 503")
    resultados, _ = A.rodar(["reclame_aqui"], limite=2, pasta_saida=tmp_path, classificar=falha,
                            redigir=redator_falso)
    assert all(not r.publicada and "503" in (r.erro or "") for r in resultados)

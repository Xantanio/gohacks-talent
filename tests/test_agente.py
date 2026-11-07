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


def juiz_falso(mensagens):
    return llm.Julgamento(True, {"fidelidade": 5, "veracidade": 5, "seguranca": 5, "empatia": 4, "clareza": 4},
                          {"fidelidade": "ok"}, modelo="juiz-falso")


def redator_falso(mensagens):
    return llm.Redacao(texto="Olá! Sentimos muito pelo transtorno. Já estamos cuidando do seu caso e retornamos em breve.",
                       justificativa="texto de teste", modelo="falso")


@pytest.fixture(scope="module")
def execucao(tmp_path_factory):
    amb = Ambiente()
    resultados, pasta = A.rodar(["reclame_aqui"], usar_ia=True, pasta_saida=tmp_path_factory.mktemp("saida"), amb=amb,
                                classificar=classificador_falso, redigir=redator_falso, julgar=juiz_falso)
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
    assert "solicitar_foto" in feitas("AT116614")                  # brinde consta no pedido: devido (D-024)
    assert "negar_por_politica" not in feitas("AT116614")
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


# ---- D-020: automação quando errar é barato --------------------------------------

def processar_um(cid, classificacao, redigir=redator_falso, julgar=None, amb=None):
    amb = amb or Ambiente()
    t = next(t for t in A.carregar_tickets(amb, ["reclame_aqui"]) if t.conversation_id == cid)
    t.classificacao = classificacao
    A.priorizar(amb, [t])
    return A.processar(amb, t, redigir, julgar)


def duvida(cats: dict[str, float]) -> llm.Classificacao:
    top = max(cats, key=cats.get)
    return llm.Classificacao(top, cats[top], cats, revisao_humana=True,
                             motivo_revisao=f"confiança {cats[top]:.2f} < 0.7", origem="teste")


def test_confianca_baixa_sem_risco_segue_automatico():
    # AT116521 ao vivo: Jev em dúvida entre "onde está" e "atraso"; as duas levam só a informar status
    amb = Ambiente()
    t = next(t for t in A.carregar_tickets(amb, ["reclame_aqui"]) if t.conversation_id == "AT116521")
    t.classificacao = duvida({"onde_esta_pedido": 0.62, "atraso_entrega": 0.36})
    A.priorizar(amb, [t])
    r = A.processar(amb, t, redator_falso)
    assert r.publicada and not r.motivos_retencao
    assert "sem risco" in r.confianca_baixa
    # a ação executada carrega na trilha o porquê de ter seguido sem humano
    acoes = [a for a in amb.estado.acoes if a["conversation_id"] == "AT116521" and a["acao"] != "classificar_conversa"]
    assert acoes and all(any("seguiu automático" in e for e in a["trilha"]["evidencias"]) for a in acoes)


def test_confianca_baixa_com_dinheiro_vai_para_pessoa():
    # se a categoria certa for arrependimento, a ação seria reembolso: errar é caro
    r = processar_um("AT116520", duvida({"onde_esta_pedido": 0.55, "arrependimento_cancelamento": 0.40}))
    assert not r.publicada and any("confiança" in m for m in r.motivos_retencao)
    assert "custo de errar alto" in r.confianca_baixa


def test_texto_reprovado_e_reescrito_automaticamente():
    textos = iter(["Como compensação, enviamos um cupom de 10%.",                     # reprovado
                   "Sentimos muito! Já registramos seu caso e seguimos acompanhando."])  # aprovado

    def redator(msgs):
        if len(msgs) > 2:  # 2ª chamada recebe o motivo da reprovação
            assert "compensação" in msgs[-1]["content"]
        return llm.Redacao(texto=next(textos), justificativa="teste", modelo="falso")

    r = processar_um("AT116524", llm.Classificacao("erro_personalizacao", 0.95, {"erro_personalizacao": 0.95},
                                                   origem="teste"), redigir=redator)
    assert r.publicada and len(r.tentativas) == 1 and "compensação" in r.tentativas[0]


def test_texto_reprovado_duas_vezes_vai_para_pessoa():
    ruim = lambda msgs: llm.Redacao(texto="Ganhe um cupom de desconto!", modelo="falso")  # noqa: E731
    r = processar_um("AT116524", llm.Classificacao("erro_personalizacao", 0.95, {"erro_personalizacao": 0.95},
                                                   origem="teste"), redigir=ruim)
    assert not r.publicada and any("validador" in m for m in r.motivos_retencao)


def test_sem_ia_tudo_vai_para_revisao(tmp_path):
    resultados, _ = A.rodar(["reclame_aqui"], limite=3, usar_ia=False, pasta_saida=tmp_path)
    assert all(not r.publicada and r.motivos_retencao for r in resultados)


def test_falha_da_ia_nao_publica(tmp_path):
    def falha(conversa, pedido=None):
        return llm.Classificacao(None, revisao_humana=True, erro="HTTP 503")
    resultados, _ = A.rodar(["reclame_aqui"], limite=2, pasta_saida=tmp_path, classificar=falha,
                            redigir=redator_falso, julgar=juiz_falso)
    assert all(not r.publicada and "503" in (r.erro or "") for r in resultados)


# ---- D-022: juiz ------------------------------------------------------------------

PERSONALIZACAO_OK = llm.Classificacao("erro_personalizacao", 0.95, {"erro_personalizacao": 0.95}, origem="teste")


def test_juiz_reprova_e_texto_e_reescrito_com_a_critica():
    notas_ruins = {"fidelidade": 5, "veracidade": 2, "seguranca": 5, "empatia": 4, "clareza": 4}
    pareceres = iter([llm.Julgamento(False, notas_ruins, {"veracidade": "diz que já está pronta"},
                                     ["'já está pronta' não consta nos fatos"], "dizer 'aguardando expedição'",
                                     ["veracidade"], modelo="juiz-falso"),
                      juiz_falso(None)])
    vistos = []

    def redator(msgs):
        vistos.append(msgs[-1]["content"])
        return llm.Redacao(texto="Sentimos muito! A reposição do seu pedido está aguardando expedição.",
                           justificativa="t", modelo="falso")

    amb = Ambiente()
    r = processar_um("AT116524", PERSONALIZACAO_OK, redigir=redator, julgar=lambda m: next(pareceres), amb=amb)
    assert r.publicada and len(r.tentativas) == 1 and "revisor de qualidade" in r.tentativas[0]
    assert "já está pronta" in vistos[-1]                      # a crítica do juiz chegou ao redator
    linhas = [v for v in amb.estado.validacoes if v["conversation_id"] == "AT116524"]
    assert [v["resultado"] for v in linhas] == ["reescrever", "aprovado"]
    assert linhas[0]["juiz_reprovados"] == ["veracidade"] and linhas[0]["juiz_justificativas"]
    acao = [a for a in amb.estado.acoes if a["acao"] == "responder_cliente"][-1]
    assert "juiz" in acao["trilha"]["origem"] and any(e.startswith("juiz:") for e in acao["trilha"]["evidencias"])


def test_juiz_reprova_tres_vezes_vai_para_pessoa():
    ruim = llm.Julgamento(False, {"fidelidade": 2, "veracidade": 5, "seguranca": 5, "empatia": 4, "clareza": 4},
                          {"fidelidade": "sugere troca"}, reprovados=["fidelidade"], modelo="juiz-falso")
    r = processar_um("AT116524", PERSONALIZACAO_OK, julgar=lambda m: ruim)
    assert not r.publicada and any("juiz reprovou" in m for m in r.motivos_retencao)
    assert len(r.tentativas) == A.TENTATIVAS_REDACAO - 1


def test_juiz_fora_do_ar_vale_o_validador():
    r = processar_um("AT116524", PERSONALIZACAO_OK, julgar=lambda m: llm.Julgamento(False, erro="HTTP 503"))
    assert r.publicada  # texto já aprovado pelas regras determinísticas


def test_validacoes_exportadas(execucao):
    amb, _, pasta = execucao
    assert amb.estado.validacoes and (pasta / "validacoes.csv").exists()
    d = (pasta / "dossies" / "AT116524.md").read_text(encoding="utf-8")
    assert "juiz juiz-falso" in d

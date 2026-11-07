"""Testes das ferramentas de AÇÃO (travas do catálogo, trilha de decisão, papéis, estado compartilhado).

Rodar:  .\\venv\\Scripts\\python.exe tests\\test_acoes.py
   ou:  .\\venv\\Scripts\\python.exe -m pytest tests\\test_acoes.py
Casos reais da fila:
  AT116677  WhatsApp, cobrança duplicada de R$ 118,80 (acima da alçada de reembolso), pedido ainda não separado
  AT116524  Reclame Aqui, personalização borrada em copo Gocase de R$ 108,80 (dentro da alçada)
  AT116568  Reclame Aqui, reação adversa ao Greens (RTU-5007), relato em 01/11 11:02
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ferramentas import Ambiente  # noqa: E402

DUPLICADA, PERSONALIZACAO, REACAO = "AT116677", "AT116524", "AT116568"
PED_DUP, PED_PERS, PED_REACAO = "GBH491723", "GCH556292", "GBH471000"


def T(por_que: str, **extra) -> dict:
    return {"por_que": por_que, "origem": "teste", **extra}


def test_acao_sem_trilha_e_recusada():
    amb = Ambiente()
    r = amb.reenviar_produto(order_id=PED_PERS, valor=50, motivo="avaria")
    assert not r["ok"] and "trilha" in r["erro"]
    r = amb.reenviar_produto(order_id=PED_PERS, valor=50, motivo="avaria", trilha={"por_que": "  "})
    assert not r["ok"]
    assert amb.consultar_pedido(PED_PERS)["ok"]  # leitura não exige trilha
    assert amb.estado.acoes == []


def test_reembolso_acima_da_alcada_exige_aprovacao_de_outra_pessoa():
    atend = Ambiente()
    sup = atend.como("supervisor", "supervisor_01")
    with atend.atendendo(DUPLICADA):
        r = atend.reembolsar(order_id=PED_DUP, valor=118.80, motivo="cobranca_duplicada", trilha=T("duplicidade"))
        assert not r["ok"] and "alçada" in r["erro"]
        # ação sem aprovação possível não pode ser pedida
        assert not atend.solicitar_aprovacao(acao="cancelar_pedido", order_id=PED_DUP, valor=90,
                                             justificativa="x", trilha=T("x"))["ok"]
        apr = atend.solicitar_aprovacao(acao="reembolsar", order_id=PED_DUP, valor=118.80,
                                        justificativa="2 cobranças efetivas, R$ 118,80 a mais",
                                        trilha=T("Cobrança duplicada confirmada no gateway",
                                                 regra="categorias.cobranca_duplicada"))
        assert apr["ok"], apr
        apr_id = apr["aprovacao_id"]
        # sem decisão ainda: não executa
        r = atend.reembolsar(order_id=PED_DUP, valor=118.80, motivo="cobranca_duplicada",
                             aprovacao_id=apr_id, trilha=T("duplicidade"))
        assert not r["ok"] and "pendente" in r["erro"]

    assert not atend.aprovar_acao(aprovacao_id=apr_id, trilha=T("x"))["ok"]  # atendente não aprova
    assert sup.aprovar_acao(aprovacao_id=apr_id, comentario="ok, gateway confirma", trilha=T("conferi"))["ok"]

    with atend.atendendo(DUPLICADA):
        # aprovação vale só para a ação/pedido/valor aprovados
        assert not atend.reembolsar(order_id=PED_DUP, valor=130, motivo="x", aprovacao_id=apr_id,
                                    trilha=T("x"))["ok"]
        r = atend.reembolsar(order_id=PED_DUP, valor=118.80, motivo="cobranca_duplicada", aprovacao_id=apr_id,
                             trilha=T("Aprovado pelo supervisor"))
        assert r["ok"], r
        assert not atend.reembolsar(order_id=PED_DUP, valor=118.80, motivo="x", aprovacao_id=apr_id,
                                    trilha=T("x"))["ok"]  # aprovação é de uso único

    # teto: nunca estornar mais do que foi efetivamente cobrado (GCH556292: 1 cobrança de R$ 108,80)
    with atend.atendendo(PERSONALIZACAO):
        assert atend.reembolsar(order_id=PED_PERS, valor=100, motivo="x", trilha=T("x"))["ok"]
        r = atend.reembolsar(order_id=PED_PERS, valor=50, motivo="x", trilha=T("x"))
        assert not r["ok"] and "total cobrado" in r["erro"]

    reemb = [a for a in atend.estado.acoes if a["acao"] == "reembolsar" and a["order_id"] == PED_DUP]
    assert len(reemb) == 1 and reemb[0]["aprovado_por"] == "supervisor_01"
    assert reemb[0]["trilha"]["por_que"] == "Aprovado pelo supervisor"
    assert any(m["tipo"] == "APROVAÇÃO" for m in atend.estado.canal_do_time)


def test_supervisor_nao_aprova_a_propria_acao():
    analista = Ambiente(papel="analista", ator="pessoa_x")
    with analista.atendendo(DUPLICADA):
        apr = analista.solicitar_aprovacao(acao="reembolsar", order_id=PED_DUP, valor=118.80,
                                           justificativa="dup", trilha=T("dup"))
    mesmo = analista.como("supervisor", "pessoa_x")
    r = mesmo.aprovar_acao(aprovacao_id=apr["aprovacao_id"], trilha=T("x"))
    assert not r["ok"] and "própria" in r["erro"]


def test_pedido_de_outro_cliente_e_bloqueado():
    amb = Ambiente()
    with amb.atendendo(DUPLICADA):
        r = amb.reembolsar(order_id=PED_PERS, valor=10, motivo="x", trilha=T("cliente pediu"))
    assert not r["ok"] and "não pertence" in r["erro"]


def test_reimpressao_e_suspeita():
    atend = Ambiente()
    sup_a, sup_b = atend.como("supervisor", "supervisor_01"), atend.como("supervisor", "supervisor_02")
    with atend.atendendo(PERSONALIZACAO):
        assert not atend.reimprimir_personalizacao(order_id=PED_DUP, valor=50, trilha=T("x"))["ok"]  # outro cliente
        s = atend.sinalizar_suspeita(conversation_id=PERSONALIZACAO, sinais="exif_antes_da_entrega",
                                     evidencias="IMG000367 com captura 2026-10-29", trilha=T("teste de bloqueio"))
        assert s["ok"]
        r = atend.reimprimir_personalizacao(order_id=PED_PERS, valor=108.80, personalizacao_correta="C.T.",
                                            trilha=T("x"))
        assert not r["ok"] and "suspeita" in r["erro"]
        assert not atend.encerrar_conversa(conversation_id=PERSONALIZACAO, trilha=T("x"))["ok"]
    assert not any(m["ferramenta"] == "sinalizar_suspeita" for m in atend.estado.mensagens)  # nada ao cliente

    # ator diferente decide; quem sinalizou não decide
    atend_como_sup = atend.como("supervisor", "agente_ia")
    assert not atend_como_sup.decidir_suspeita(conversation_id=PERSONALIZACAO, decisao="procedente",
                                               justificativa="x", trilha=T("x"))["ok"]
    assert sup_a.decidir_suspeita(conversation_id=PERSONALIZACAO, decisao="procedente",
                                  justificativa="foto coerente com o pedido", trilha=T("conferi"))["ok"]
    with atend.atendendo(PERSONALIZACAO):
        r = atend.reimprimir_personalizacao(order_id=PED_PERS, valor=108.80, personalizacao_correta="C.T.",
                                            trilha=T("Impressão borrada com foto; dentro da alçada",
                                                     regra="categorias.erro_personalizacao"))
        assert r["ok"], r
    assert atend.estado.oms[-1]["tipo"] == "nova_producao"
    assert sup_b.decidir_suspeita(conversation_id=PERSONALIZACAO, decisao="procedente", justificativa="x",
                                  trilha=T("x"))["ok"] is False  # já decidida


def test_suspeita_improcedente_responde_neutro_e_encerra():
    atend = Ambiente()
    atend.sinalizar_suspeita(conversation_id=PERSONALIZACAO, evidencias="hash repetido", trilha=T("x"))
    r = atend.como("supervisor", "supervisor_01").decidir_suspeita(
        conversation_id=PERSONALIZACAO, decisao="improcedente", justificativa="hash igual a outro cliente",
        trilha=T("x"))
    assert r["ok"]
    texto = atend.estado.mensagens[-1]["texto"].lower()
    assert "fraude" not in texto and "golpe" not in texto
    assert atend.estado.conversa(PERSONALIZACAO)["encerrada"]
    assert not atend.reenviar_produto(order_id=PED_PERS, valor=10, motivo="avaria", trilha=T("x"))["ok"]


def test_reacao_adversa_cosmetovigilancia():
    atend = Ambiente()
    sup = atend.como("supervisor", "supervisor_01")
    assert atend.orientar_suspensao_uso(conversation_id=REACAO, trilha=T("reação adversa relatada",
                                                                        regra="categorias.reacao_adversa"))["ok"]
    r = atend.registrar_cosmetovigilancia(conversation_id=REACAO, sku="RTU-5007", descricao="rosto inchado",
                                          trilha=T("x"))
    assert not r["ok"] and "papel" in r["erro"]  # só supervisor registra
    lote = atend.consultar_pedido(PED_REACAO)["pedido"]["lote_produto"]
    r = sup.registrar_cosmetovigilancia(conversation_id=REACAO, sku="RTU-5007", lote=lote,
                                        descricao="Rosto inchado após uso do Greens", trilha=T("obrigatório em 24h"))
    assert r["ok"], r
    assert r["dentro_do_prazo"] and r["horas_desde_relato"] < 24
    assert "sem fotos anexadas" in r["pendencias"]
    assert not any("orientar" in p for p in r["pendencias"])
    assert any(m["area"] == "qualidade" for m in atend.estado.canal_do_time)
    # Gocase não é cosmético
    assert not sup.registrar_cosmetovigilancia(conversation_id=REACAO, sku="GOC-5001", descricao="x",
                                               trilha=T("x"))["ok"]


def test_cosmetovigilancia_fora_do_prazo_vira_critica():
    sup = Ambiente(papel="supervisor", ator="supervisor_01")
    sup.tique = 10  # 02/11 18:00 -> 31h desde o relato
    r = sup.registrar_cosmetovigilancia(conversation_id=REACAO, sku="RTU-5007", descricao="x", trilha=T("x"))
    assert r["ok"] and not r["dentro_do_prazo"]
    assert sup.estado.canal_do_time[-1]["urgencia"] == "critica"


def test_cancelar_e_endereco_so_antes_da_separacao():
    amb = Ambiente()
    assert not amb.cancelar_pedido(order_id=PED_PERS, trilha=T("x"))["ok"]                 # já entregue
    assert not amb.alterar_endereco(order_id=PED_PERS, novo_endereco="Rua A", trilha=T("x"))["ok"]
    assert amb.alterar_endereco(order_id=PED_DUP, novo_endereco="Rua B, 10", trilha=T("cliente pediu"))["ok"]
    r = amb.cancelar_pedido(order_id=PED_DUP, motivo="arrependimento", trilha=T("antes da separação"))
    assert r["ok"] and r["estorno_integral"] and r["valor"] == 118.80                       # sem alçada
    assert amb.consultar_pedido(PED_DUP)["pedido"]["status_entrega"] == "cancelado"
    assert not amb.cancelar_pedido(order_id=PED_DUP, trilha=T("x"))["ok"]
    assert not amb.reenviar_produto(order_id=PED_DUP, valor=10, motivo="avaria", trilha=T("x"))["ok"]


def test_uma_troca_por_pedido():
    amb = Ambiente()
    assert amb.trocar_produto(order_id=PED_REACAO, valor=99.9, trilha=T("troca de sabor"))["ok"]
    r = amb.trocar_produto(order_id=PED_REACAO, valor=99.9, trilha=T("de novo"))
    assert not r["ok"] and "1 troca" in r["erro"]


def test_chamado_transportadora():
    amb = Ambiente()
    assert not amb.abrir_chamado_transportadora(order_id=PED_DUP, motivo="atraso_na_entrega", trilha=T("x"))["ok"]
    assert not amb.abrir_chamado_transportadora(order_id=PED_PERS, motivo="atraso_na_entrega", trilha=T("x"))["ok"]
    assert not amb.abrir_chamado_transportadora(order_id=PED_PERS, motivo="inventado", trilha=T("x"))["ok"]


def test_mensagens_ao_cliente():
    amb = Ambiente()
    r = amb.responder_cliente(conversation_id=DUPLICADA, texto="Seu CPF 123.456.789-09 foi verificado.",
                              trilha=T("teste de máscara"))
    assert r["ok"] and r["pii_mascarado"] and "123.456" not in amb.estado.mensagens[-1]["texto"]
    st = amb.informar_status(conversation_id=DUPLICADA, order_id=PED_DUP, trilha=T("cliente perguntou"))
    assert st["ok"] and PED_DUP in st["texto"]
    assert not amb.informar_status(conversation_id=DUPLICADA, order_id=PED_PERS, trilha=T("x"))["ok"]
    assert not amb.negar_por_politica(conversation_id=DUPLICADA, regra="regra_que_nao_existe", trilha=T("x"))["ok"]
    assert amb.negar_por_politica(conversation_id=DUPLICADA, regra="alteracao_endereco", trilha=T("x"))["ok"]
    assert not amb.classificar_conversa(conversation_id=DUPLICADA, categoria="Golpe", trilha=T("x"))["ok"]
    assert amb.classificar_conversa(conversation_id=DUPLICADA, categoria="Financeiro", trilha=T("jev 0.93"))["ok"]
    assert amb.responder_duvida(conversation_id=REACAO, trilha=T("x"))["ficha"]["sku"] == "RTU-5007"
    assert amb.encerrar_conversa(conversation_id=DUPLICADA, motivo="resolvido", trilha=T("x"))["ok"]
    assert not amb.responder_cliente(conversation_id=DUPLICADA, texto="oi", trilha=T("x"))["ok"]
    conversa = amb.ler_conversa(DUPLICADA)
    assert conversa["estado_agente"]["encerrada"]
    assert any(m["autor"] == "agente" for m in conversa["mensagens"])


def test_mascarar_publico():
    from src.ferramentas import mascarar_publico
    t = mascarar_publico("Pedido GBH473217, rastreio EJ998156173BR, VL59160032895, RPS572758581, TN6482811065, "
                         "CPF 123.456.789-09")
    assert "GBH***217" in t and "473217" not in t
    assert not any(c in t for c in ("EJ998156173BR", "VL59160032895", "RPS572758581", "TN6482811065", "123.456"))


def test_modo_publico_no_reclame_aqui():
    amb = Ambiente()
    ra, ped_ra = "AT116160", "GBH473217"  # Reclame Aqui, pedido em trânsito com código de rastreio
    st = amb.informar_status(conversation_id=ra, order_id=ped_ra, trilha=T("cliente perguntou"))
    assert st["ok"] and "GBH***217" in st["texto"]
    assert ped_ra not in st["texto"] and "rastreio:" not in st["texto"].lower()
    assert amb.estado.mensagens[-1]["publica"] is True

    r = amb.responder_cliente(conversation_id=ra, texto=f"Seu pedido {ped_ra} (EJ998156173BR) já foi localizado.",
                              trilha=T("teste"))
    assert r["publica"] and ped_ra not in r["texto"] and "EJ998156173BR" not in r["texto"]

    foto = amb.solicitar_foto(conversation_id=PERSONALIZACAO, trilha=T("exige foto"))
    assert "canal privado" in foto["texto"]

    # canal privado (WhatsApp) continua com o número completo e o rastreio
    st = amb.informar_status(conversation_id=DUPLICADA, order_id=PED_DUP, trilha=T("x"))
    assert PED_DUP in st["texto"] and amb.estado.mensagens[-1]["publica"] is False


def test_acionar_pessoa_e_cupom():
    amb = Ambiente()
    assert not amb.acionar_pessoa(area="marketing", assunto="x", contexto="x", trilha=T("x"))["ok"]
    r = amb.acionar_pessoa(area="logistica", assunto="3 atrasos ViaLog/SP", contexto="pedidos A, B, C",
                           urgencia="alta", trilha=T("padrão repetido"))
    assert r["ok"] and "logística" in r["texto_slack"]
    assert amb.validar_cupom(codigo="NEWS5")["valido"]
    assert not amb.validar_cupom(codigo="NAOEXISTE")["valido"]
    assert amb.consultar_politicas(categoria="avaria_vazamento")["regras"]["exige_foto"] is True


def test_estado_compartilhado_e_exportacao():
    atend = Ambiente()
    sup = atend.como("supervisor", "supervisor_01")
    atend.avancar(3)
    assert sup.tique == 3
    atend.agradecer(conversation_id=DUPLICADA, trilha=T("teste"))
    sup.acionar_pessoa(area="jurídico", assunto="x", contexto="y", trilha=T("teste"))
    assert len(sup.log) == len(atend.log) == 2
    assert all(a["trilha"]["por_que"] for a in atend.estado.acoes)
    assert atend.estado.acoes[0]["data_hora"] == "2026-11-02 11:00:00"
    with tempfile.TemporaryDirectory() as d:
        arquivos = atend.estado.exportar(d)
        assert arquivos["acoes_executadas"].read_text(encoding="utf-8").count("\n") == 3
        assert "jurídico" in arquivos["canal_do_time"].read_text(encoding="utf-8")


if __name__ == "__main__":
    testes = [v for k, v in dict(globals()).items() if k.startswith("test_") and callable(v)]
    for t in testes:
        t()
        print(f"OK  {t.__name__}")
    print(f"\n{len(testes)} testes passaram.")

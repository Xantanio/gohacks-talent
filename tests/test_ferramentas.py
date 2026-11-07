"""Teste das ferramentas de leitura sobre 3 tickets da fila_aberta (sem IA).

Rodar:  .\\venv\\Scripts\\python.exe tests\\test_ferramentas.py      (imprime um resumo por ticket)
   ou:  .\\venv\\Scripts\\python.exe -m pytest tests\\test_ferramentas.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import dados  # noqa: E402
from src.ferramentas import CATALOGO, Ambiente, mascarar_pii  # noqa: E402

# Três casos diferentes da fila: cobrança no WhatsApp, personalização errada e reação adversa (Reclame Aqui).
TICKETS = ["AT116677", "AT116524", "AT116568"]
LEITURA = ["ler_conversa", "consultar_cliente", "consultar_pedido", "ver_anexos", "verificar_pagamento"]


def investigar(amb: Ambiente, conversation_id: str) -> dict:
    """Roteiro fixo de leitura de um ticket (o que o agente fará antes de decidir)."""
    conversa = amb.ler_conversa(conversation_id)
    assert conversa["ok"], conversa
    meta = conversa["conversa"]
    out = {"conversa": conversa, "cliente": amb.consultar_cliente(meta["customer_id"])}
    if meta["order_id"]:
        out["pedido"] = amb.consultar_pedido(meta["order_id"])
        out["pagamento"] = amb.verificar_pagamento(meta["order_id"])
        out["anexos_pedido"] = amb.ver_anexos(order_id=meta["order_id"])
    out["anexos_conversa"] = amb.ver_anexos(conversation_id=conversation_id)
    return out


def test_catalogo_bate_com_csv():
    for nome in LEITURA:
        assert nome in CATALOGO
    assert CATALOGO["ver_anexos"].obrigatorios == ()
    assert CATALOGO["ver_anexos"].opcionais == ("conversation_id", "order_id", "hash_perceptual")
    assert CATALOGO["reembolsar"].limites["valor_max_sem_aprovacao_rs"] == 100.0


def test_tres_tickets_da_fila():
    fila = dados.tabela("fila_aberta")
    amb = Ambiente(papel="atendente")
    for cid in TICKETS:
        assert cid in set(fila["conversation_id"]), cid
        r = investigar(amb, cid)
        meta = r["conversa"]["conversa"]
        assert r["conversa"]["origem"] == "fila_aberta"
        assert r["conversa"]["mensagens"], f"{cid} sem mensagens"
        assert all(m["timestamp"] <= "2026-11-02 08:00:00" for m in r["conversa"]["mensagens"])
        assert r["cliente"]["ok"] and r["cliente"]["cliente"]["customer_id"] == meta["customer_id"]
        assert "cpf" not in r["cliente"]["cliente"] and "telefone" not in r["cliente"]["cliente"]
        assert r["pedido"]["ok"] and r["pedido"]["pedido"]["customer_id"] == meta["customer_id"]
        assert r["pedido"]["itens"], f"{meta['order_id']} sem itens"
        assert r["pagamento"]["ok"]
        assert all("cartao_final" not in t for t in r["pagamento"]["transacoes"])
        assert r["anexos_pedido"]["ok"] and r["anexos_conversa"]["ok"]
    # cada ticket: conversa, cliente, pedido, pagamento, 2x anexos
    assert len(amb.log) == 6 * len(TICKETS)
    assert all(e["ok"] for e in amb.log)


def test_validacoes_do_catalogo():
    amb = Ambiente(papel="atendente")
    assert not amb.chamar("consultar_pedido")["ok"]                                   # obrigatório ausente
    assert not amb.chamar("consultar_pedido", order_id="X", foo=1)["ok"]              # parâmetro desconhecido
    assert not amb.ver_anexos()["ok"]                                                 # nenhum filtro
    assert not amb.chamar("buscar_conversas")["ok"]                                   # papel não permitido
    assert not amb.chamar("ferramenta_de_cupom")["ok"]                                # não existe
    assert not amb.consultar_pedido("GBH000000")["ok"]                                # não encontrado
    assert len(amb.log) == 6 and not any(e["ok"] for e in amb.log)


def test_relogio_esconde_pedido_futuro():
    peds = dados.tabela("pedidos")
    futuro = peds[peds["data_hora_pedido"] > "2026-11-02 08:00:00"].sort_values("data_hora_pedido").iloc[0]
    amb = Ambiente()
    assert not amb.consultar_pedido(futuro["order_id"])["ok"]
    amb.tique = 159
    assert amb.consultar_pedido(futuro["order_id"])["ok"]


def test_mascarar_pii():
    t = mascarar_pii("meu cpf 123.456.789-09, cartão 4111 1111 1111 1111, tel (11) 98765-4321, pedido GBH491723")
    assert "123.456" not in t and "4111" not in t and "98765" not in t
    assert "GBH491723" in t


# ---------------------------------------------------------------------------
# Execução direta: roda os testes e imprime o que o agente "veria" em cada ticket.
# ---------------------------------------------------------------------------

def _resumo(cid: str, r: dict) -> None:
    c = r["conversa"]["conversa"]
    print(f"\n=== {cid} | {c['canal']} | {c['marca']} | status={c['status']} | assunto={c['assunto']}")
    for m in r["conversa"]["mensagens"]:
        print(f"  [{m['timestamp']}] {m['autor']:>7}: {m['texto'][:140]}")
    cli = r["cliente"]["cliente"]
    print(f"  cliente {cli['customer_id']}: {cli['segmento']}, {cli['n_pedidos']} pedidos, "
          f"R$ {cli['receita_acumulada']:.2f}, trocas_12m={cli['n_trocas_12m']}, "
          f"reenvios/trocas registrados={len(r['cliente']['trocas_reenvios'])}")
    if "pedido" in r:
        p = r["pedido"]["pedido"]
        ult = r["pedido"]["ultimo_evento_rastreio"]
        print(f"  pedido {p['order_id']}: R$ {p['valor']:.2f} | {p['descricao_produto']} | "
              f"{p['status_entrega']} | {p['transportadora']}/{p['uf']} | entrega={p['data_entrega']} "
              f"| prometida={p['data_prometida']}")
        for it in r["pedido"]["itens"]:
            print(f"    item: {it['sku']} {it['descricao']} ({it['tipo_item']}) "
                  f"{it['texto_personalizacao'] or ''}")
        if ult:
            print(f"    rastreio: {ult['data_hora_evento']} {ult['status']} @ {ult['local']}")
        res = r["pagamento"]["resumo"]
        print(f"  pagamento: {res}")
        print(f"  anexos do pedido: {r['anexos_pedido']['n_anexos']} | da conversa: {r['anexos_conversa']['n_anexos']}")
        for a in r["anexos_pedido"]["anexos"]:
            print(f"    {a['anexo_id']} hash={a['hash_perceptual']} exif={a['exif_data_captura']} :: {a['descricao_visual']}")


if __name__ == "__main__":
    testes = [v for k, v in dict(globals()).items() if k.startswith("test_") and callable(v)]
    for t in testes:
        t()
        print(f"OK  {t.__name__}")
    amb = Ambiente()
    for cid in TICKETS:
        _resumo(cid, investigar(amb, cid))
    print(f"\n{len(testes)} testes passaram; {len(amb.log)} chamadas de ferramenta registradas no log.")

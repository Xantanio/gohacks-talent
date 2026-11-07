"""Testes de veracidade: sinais verificáveis -> p_verdadeira.

Rodar:  .\\venv\\Scripts\\python.exe -m pytest tests\\test_veracidade.py -q
A fila atual não tem foto reaproveitada; os casos de fraude vêm do histórico (o relógio em 02/11 já os enxerga).
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import politicas as Pol  # noqa: E402
from src import veracidade as V  # noqa: E402
from src.ferramentas import Ambiente  # noqa: E402


@pytest.fixture(scope="module")
def amb() -> Ambiente:
    return Ambiente()


def ver(amb, cid, categoria="avaria_vazamento", **mudancas):
    f = Pol.coletar_fatos(amb, cid)
    if mudancas:
        f = replace(f, **mudancas)
    return V.avaliar_veracidade(amb, f, categoria), f


def nomes(v: V.Veracidade) -> set[str]:
    return {s.nome for s in v.sinais}


# ---- combinação -------------------------------------------------------------

def test_sem_sinais_fica_na_taxa_base():
    assert V.combinar([]) == pytest.approx(1 - V.P["PRIOR_FALSA"], abs=1e-4)


def test_um_sinal_forte_derruba_a_probabilidade():
    s = V.Sinal("x", "foto", V.P["LR_HASH_OUTRO_CLIENTE"], "e")
    assert V.combinar([s]) < 0.3


def test_sinais_correlacionados_sao_amortecidos():
    a = V.Sinal("a", "foto", 30, "e")
    b = V.Sinal("b", "foto", 20, "e")
    c = V.Sinal("c", "historico", 20, "e")
    mesmo_grupo, grupos_diferentes = V.combinar([a, b]), V.combinar([a, c])
    assert mesmo_grupo > grupos_diferentes  # dentro do grupo o 2º sinal pesa menos


def test_sinais_a_favor_sobem_a_probabilidade():
    assert V.combinar([V.Sinal("x", "foto", 0.3, "e")]) > V.combinar([])


# ---- casos reais ------------------------------------------------------------

def test_reclamacao_coerente_da_fila_tem_p_alta(amb):
    v, _ = ver(amb, "AT116524", "erro_personalizacao")
    assert v.p_verdadeira > 0.9 and not v.contra
    assert "descricao_identifica_o_produto" in nomes(v)


def test_foto_da_internet_reaproveitada_tem_p_baixa(amb):
    v, _ = ver(amb, "AT114996")  # IMG000292: mesma foto de 2 outros clientes, EXIF antes da entrega
    assert v.p_verdadeira < 0.2
    assert {"foto_reaproveitada_outro_cliente", "exif_antes_da_entrega", "descricao_generica"} <= nomes(v)
    sinal = V.Veracidade.para_sinalizar(v)
    assert "IMG000021" in sinal["evidencias"] or "IMG000188" in sinal["evidencias"]  # evidência verificável


def test_exif_antes_da_entrega_com_descricao_generica(amb):
    v, _ = ver(amb, "AT109113")  # IMG000123
    assert {"exif_antes_da_entrega", "descricao_generica"} <= nomes(v) and v.p_verdadeira < 0.3


def test_descricao_generica_mesmo_sem_hash_repetido(amb):
    # IMG000126: foto única que não identifica o item; a mesma conversa tem outra foto que identifica.
    # Evidência mista -> zona de dúvida (nem aprova direto, nem conclui fraude).
    v, _ = ver(amb, "AT109160")
    assert {"descricao_generica", "descricao_identifica_o_produto"} <= nomes(v)
    assert "foto_reaproveitada_outro_cliente" not in nomes(v)
    assert 0.4 < v.p_verdadeira < 0.8


def test_categoria_sem_reclamacao_nao_se_aplica(amb):
    v, _ = ver(amb, "AT116597", "duvida_produto")
    assert not v.aplicavel and v.p_verdadeira == 1.0


def test_atraso_ignora_foto_e_usa_so_historico(amb):
    v, _ = ver(amb, "AT116538", "atraso_entrega")
    assert all(s.grupo != "foto" for s in v.sinais)


# ---- casos-limite sintéticos ------------------------------------------------

def test_reclama_de_avaria_antes_da_entrega(amb):
    f = Pol.coletar_fatos(amb, "AT116700")
    v = V.avaliar_veracidade(amb, replace(f, pedido={**f.pedido, "status_entrega": "em_transito"}), "avaria_vazamento")
    assert "reclama_antes_da_entrega" in nomes(v)


def test_foto_de_outro_tipo_de_produto(amb):
    f = Pol.coletar_fatos(amb, "AT116700")  # Body Mist (GoBeauté)
    anexo = {**f.anexos[0], "anexo_id": "IMGX", "hash_perceptual": "ffffffffffffffff",
             "descricao_visual": "Copo térmico de inox com a tampa quebrada.", "exif_data_captura": None}
    v = V.avaliar_veracidade(amb, replace(f, anexos=[anexo]), "avaria_vazamento")
    assert "descricao_de_outro_produto" in nomes(v)


def test_etiqueta_de_outro_pedido(amb):
    f = Pol.coletar_fatos(amb, "AT116700")
    anexo = {**f.anexos[0], "anexo_id": "IMGY", "hash_perceptual": "eeeeeeeeeeeeeeee", "exif_data_captura": None,
             "descricao_visual": "Caixa da transportadora molhada; etiqueta com o número do pedido GBH000001 legível."}
    v = V.avaliar_veracidade(amb, replace(f, anexos=[anexo]), "avaria_vazamento")
    assert "etiqueta_de_outro_pedido" in nomes(v) and v.p_verdadeira < 0.5


def test_suspeita_improcedente_anterior_pesa(amb):
    a = Ambiente()
    f = Pol.coletar_fatos(a, "AT116700")
    a.estado.suspeitas["AT999999"] = {"customer_id": f.customer_id, "status": "improcedente", "order_id": "X",
                                      "justificativa": "foto de outro cliente"}
    v = V.avaliar_veracidade(a, f, "avaria_vazamento")
    assert "suspeita_improcedente_anterior" in nomes(v)


def test_identidade_compartilhada(amb, monkeypatch):
    f = Pol.coletar_fatos(amb, "AT116700")
    original = V.dados.tabela

    def tabela_com_clone(nome):
        df = original(nome)
        if nome != "clientes":
            return df
        linha = df[df["customer_id"] == f.customer_id].copy()
        linha["customer_id"] = "CH000000"
        return __import__("pandas").concat([df, linha])

    monkeypatch.setattr(V.dados, "tabela", tabela_com_clone)
    grupo = V.detectar_grupo(f.customer_id)
    assert {"cpf", "telefone", "email"} == {g["campo"] for g in grupo}
    assert all("CH000000" == g["customer_id"] for g in grupo)
    assert all(set(g) == {"customer_id", "campo"} for g in grupo)  # nunca devolve o valor (CPF/telefone)


def test_sem_identidade_compartilhada_nos_dados_reais():
    assert V.detectar_grupo("CH389325") == []


def test_falha_sistemica_nao_vale_para_item_faltante(amb):
    f = Pol.coletar_fatos(amb, "AT116686")
    assert V.falha_sistemica(f.pedido, f.agora, "item_faltante") is None


def test_evidencias_para_a_trilha(amb):
    v, _ = ver(amb, "AT114996")
    assert all(e.startswith(("[contra]", "[a_favor]")) for e in v.evidencias())
    assert "p_verdadeira=" in v.resumo()

"""Ferramentas do papel ANALISTA (ferramentas.csv): buscar_conversas, consultar_metricas, abrir_incidente, notificar_clientes.

Mesmo contrato das demais: nomes, parâmetros, papéis e limites do catálogo; relógio respeitado (nada depois do tique);
ações exigem trilha. Travas aplicadas aqui (D-026):
- buscar_conversas: no máximo 500 resultados (limite do catálogo); filtro de lote é POR ITEM (itens_pedido.lote), não o
  lote do produto principal do pedido (D-025: o lote do pedido gera falso incidente de vazamento).
- notificar_clientes: no máximo 300 por dia simulado (catálogo); só cliente que aceita comunicação
  (aceita_comunicacao_whatsapp ou _email: LGPD); nunca o mesmo tipo de aviso duas vezes para o mesmo pedido;
  texto com PII, número de pedido e rastreio mascarados (aviso em massa não é personalizado).
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Callable

import pandas as pd

from . import dados
from .acoes import _erro, _postar_no_canal, _registrar
from .ferramentas import CATALOGO, _vazio, mascarar_pii, mascarar_publico

if TYPE_CHECKING:
    from .ferramentas import Ambiente

Resultado = dict[str, Any]

TIPOS_INCIDENTE = {"transportadora_uf": "logística", "maquina": "produção", "lote": "qualidade",
                   "turno": "logística", "gateway": "supervisão"}
DIMENSOES = {"dia", "categoria", "canal", "marca", "transportadora", "uf", "sku", "lote", "maquina", "cd"}
_COL_DIM = {"maquina": "maquina_personalizacao", "cd": "cd_separacao"}


# ---------------------------------------------------------------------------
# Base de conversas (histórico + fila) até o tique, com atributos do pedido e texto do cliente
# ---------------------------------------------------------------------------

@lru_cache(maxsize=8)
def base_conversas(tique: int) -> pd.DataFrame:
    agora = pd.Timestamp(dados.agora(tique))
    h = dados.tabela("historico_conversas").assign(origem="historico")
    f = dados.tabela("fila_aberta").assign(origem="fila")
    conv = pd.concat([h, f], ignore_index=True)
    conv = conv[conv["data_abertura"] <= agora].copy()
    conv["categoria"] = conv.get("tag_agente")
    msgs = dados.tabela("historico_mensagens")
    msgs = msgs[(msgs["autor"] == "cliente") & (msgs["timestamp"] <= agora)]
    texto = msgs.groupby("conversation_id")["texto"].apply(lambda s: " | ".join(s.dropna().astype(str)))
    conv["texto_cliente"] = conv["conversation_id"].map(texto).fillna("")
    peds = dados.tabela("pedidos")[["order_id", "transportadora", "uf", "sku", "maquina_personalizacao",
                                    "cd_separacao", "data_separacao", "lote_produto", "status_entrega"]]
    return conv.merge(peds, on="order_id", how="left")


@lru_cache(maxsize=1)
def lotes_por_pedido() -> pd.DataFrame:
    itens = dados.tabela("itens_pedido")
    return itens[itens["lote"].notna()][["order_id", "sku", "descricao", "lote"]]


def _periodo(df: pd.DataFrame, col: str, inicio: Any, fim: Any) -> pd.DataFrame:
    if not _vazio(inicio):
        df = df[df[col] >= pd.Timestamp(inicio)]
    if not _vazio(fim):
        df = df[df[col] < pd.Timestamp(fim) + pd.Timedelta(days=1)]  # fim inclusivo (dia inteiro)
    return df


# ---------------------------------------------------------------------------
# Leitura
# ---------------------------------------------------------------------------

def _buscar_conversas(amb: Ambiente, inicio=None, fim=None, canal=None, marca=None, transportadora=None, uf=None,
                      sku=None, lote=None, texto=None) -> Resultado:
    df = _periodo(base_conversas(amb.tique), "data_abertura", inicio, fim)
    for col, v in (("canal", canal), ("marca", marca), ("transportadora", transportadora), ("uf", uf)):
        if not _vazio(v):
            df = df[df[col] == v]
    if not _vazio(sku):
        itens = dados.tabela("itens_pedido")
        df = df[(df["sku"] == sku) | df["order_id"].isin(itens.loc[itens["sku"] == sku, "order_id"])]
    if not _vazio(lote):
        lp = lotes_por_pedido()
        df = df[df["order_id"].isin(lp.loc[lp["lote"] == lote, "order_id"])]
    if not _vazio(texto):
        df = df[df["texto_cliente"].str.contains(re.escape(str(texto)), case=False, na=False)]
    limite = CATALOGO["buscar_conversas"].limites.get("max_resultados", 500)
    total = len(df)
    df = df.sort_values("data_abertura").head(limite)
    cols = ["conversation_id", "customer_id", "order_id", "canal", "marca", "data_abertura", "status", "origem",
            "categoria", "transportadora", "uf", "sku", "maquina_personalizacao", "cd_separacao", "data_separacao"]
    linhas = dados.registros(df[cols])
    for linha, txt in zip(linhas, df["texto_cliente"]):
        linha["trecho"] = mascarar_pii(txt[:160])
    return {"ok": True, "total": total, "truncado": total > limite, "conversas": linhas}


def _consultar_metricas(amb: Ambiente, agrupar_por: str, inicio=None, fim=None) -> Resultado:
    dims = [d.strip() for d in re.split(r"[;,]", agrupar_por) if d.strip()]
    invalidas = set(dims) - DIMENSOES
    if not dims or invalidas:
        return _erro(f"agrupar_por inválido: {sorted(invalidas) or agrupar_por} (válidas: {sorted(DIMENSOES)})")
    df = _periodo(base_conversas(amb.tique), "data_abertura", inicio, fim).copy()
    df["dia"] = df["data_abertura"].dt.date.astype(str)
    if "lote" in dims:  # lote por item: um pedido com 2 lotes conta nos 2
        df = df.merge(lotes_por_pedido()[["order_id", "lote"]], on="order_id", how="inner")
    cols = [_COL_DIM.get(d, d) for d in dims]
    g = df.groupby(cols, dropna=True).agg(n_conversas=("conversation_id", "nunique"),
                                          n_pedidos=("order_id", "nunique")).reset_index()
    g = g.rename(columns={v: k for k, v in _COL_DIM.items()}).sort_values("n_conversas", ascending=False)
    return {"ok": True, "agrupar_por": dims, "grupos": dados.registros(g)}


# ---------------------------------------------------------------------------
# Ações
# ---------------------------------------------------------------------------

def _abrir_incidente(amb: Ambiente, trilha: dict, tipo: str, escopo: str, evidencias: str) -> Resultado:
    if tipo not in TIPOS_INCIDENTE:
        return _erro(f"tipo inválido: {tipo} (válidos: {sorted(TIPOS_INCIDENTE)})")
    aberto = next((i for i in amb.estado.incidentes if i["tipo"] == tipo and i["escopo"] == escopo
                   and i["status"] == "aberto"), None)
    if aberto:
        return _erro(f"já existe incidente aberto para {tipo} / {escopo}: {aberto['incidente_id']}")
    inc_id = amb.estado.novo_id("INC")
    acao_id = _registrar(amb, "abrir_incidente", trilha, referencia=inc_id)
    area = TIPOS_INCIDENTE[tipo]
    amb.estado.incidentes.append({
        "incidente_id": inc_id, "acao_id": acao_id, "tipo": tipo, "escopo": escopo, "evidencias": evidencias,
        "area_responsavel": area, "aberto_por": amb.ator, "aberto_em": amb.estado.agora_txt(), "tique": amb.tique,
        "status": "aberto", "origem": trilha.get("origem"), "por_que": trilha.get("por_que"),
    })
    slack = _postar_no_canal(amb, "INCIDENTE", f"{inc_id} — {tipo}: {escopo}", {
        "Evidências": evidencias, "Por que": trilha.get("por_que"),
        "O que esperamos": f"{area} confirmar a causa e corrigir na origem",
    }, urgencia="alta", area=area)
    return {"ok": True, "acao_id": acao_id, "incidente_id": inc_id, "area": area, "slack_id": slack["id"]}


def _aceita(cliente: pd.Series) -> str | None:
    """Canal permitido para aviso proativo (LGPD): WhatsApp se aceitar, senão e-mail; None se não aceita nenhum."""
    if str(cliente.get("aceita_comunicacao_whatsapp")).lower() in ("sim", "true", "1"):
        return "whatsapp"
    if str(cliente.get("aceita_comunicacao_email")).lower() in ("sim", "true", "1"):
        return "email"
    return None


def _notificar_clientes(amb: Ambiente, trilha: dict, order_ids: Any, tipo_aviso: str, texto: str) -> Resultado:
    ids = order_ids if isinstance(order_ids, (list, tuple, set)) else re.split(r"[;,\s]+", str(order_ids))
    ids = list(dict.fromkeys(i.strip() for i in ids if str(i).strip()))
    if not ids:
        return _erro("nenhum order_id")
    limite = CATALOGO["notificar_clientes"].limites.get("max_por_dia", 300)
    hoje = amb.agora.date().isoformat()
    usados = sum(1 for n in amb.estado.notificacoes if n["data"] == hoje)
    peds = dados.tabela("pedidos").set_index("order_id")
    clis = dados.tabela("clientes").set_index("customer_id")
    agora = pd.Timestamp(amb.agora)
    ja = {(n["order_id"], n["tipo_aviso"]) for n in amb.estado.notificacoes}
    texto_final = mascarar_publico(texto)  # aviso em massa: sem PII, número de pedido ou rastreio
    lote_id = amb.estado.novo_id("NOT")
    enviados, ignorados = [], {}
    for oid in ids:
        if oid not in peds.index or peds.loc[oid, "data_hora_pedido"] > agora:
            ignorados[oid] = "pedido inexistente nesta data"
            continue
        if (oid, tipo_aviso) in ja:
            ignorados[oid] = "já avisado com este tipo de aviso"
            continue
        canal = _aceita(clis.loc[peds.loc[oid, "customer_id"]])
        if canal is None:
            ignorados[oid] = "cliente não aceita comunicação"
            continue
        if usados + len(enviados) >= limite:
            ignorados[oid] = f"limite diário de {limite} avisos atingido"
            continue
        enviados.append((oid, canal))
    acao_id = _registrar(amb, "notificar_clientes", trilha, referencia=lote_id) if enviados else None
    for oid, canal in enviados:
        amb.estado.notificacoes.append({
            "notificacao_id": f"{lote_id}-{len(amb.estado.notificacoes) + 1:04d}", "lote": lote_id, "acao_id": acao_id,
            "order_id": oid, "customer_id": peds.loc[oid, "customer_id"], "canal": canal, "tipo_aviso": tipo_aviso,
            "texto": texto_final, "data": hoje, "data_hora": amb.estado.agora_txt(), "enviado_por": amb.ator,
        })
    motivos = pd.Series(ignorados).value_counts().to_dict() if ignorados else {}
    return {"ok": True, "acao_id": acao_id, "lote": lote_id, "enviados": len(enviados), "ignorados": ignorados,
            "ignorados_por_motivo": motivos, "restante_hoje": limite - usados - len(enviados), "texto": texto_final}


IMPLEMENTACOES: dict[str, Callable[..., Resultado]] = {
    "buscar_conversas": _buscar_conversas,
    "consultar_metricas": _consultar_metricas,
    "abrir_incidente": _abrir_incidente,
    "notificar_clientes": _notificar_clientes,
}

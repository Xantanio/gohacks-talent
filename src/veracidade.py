"""Veracidade da reclamação: sinais verificáveis -> `p_verdadeira`.

Como funciona (ver D-012 a D-014 em docs/DECISOES.md):
- Começa da taxa-base (`PRIOR_FALSA`, medida nos dados) e aplica razões de verossimilhança (LR) de cada sinal:
  LR = P(sinal | reclamação falsa) / P(sinal | verdadeira). LR > 1 pesa contra; LR < 1 pesa a favor.
- Sinais do mesmo GRUPO são correlacionados (a foto da internet costuma vir com EXIF antigo E descrição genérica):
  dentro do grupo conta o sinal mais forte inteiro e os demais com peso `PESO_SINAIS_SECUNDARIOS` (em log).
- Só fatos verificáveis nos dados (politicas: "impressão subjetiva não basta"). NÃO usamos tom da mensagem
  nem segmento do cliente como sinal (CLAUDE.md: novatos não fraudam mais; tom não indica veracidade).

Este módulo NÃO decide nada: entrega `p_verdadeira` + sinais com evidência para `decisao.py` e para a trilha.
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pandas as pd

from . import dados
from .politicas import Fatos

if TYPE_CHECKING:
    from .ferramentas import Ambiente

# ---------------------------------------------------------------------------
# Parâmetros (explícitos e ajustáveis; origem de cada número em D-013)
# ---------------------------------------------------------------------------

PARAMETROS: dict[str, float] = {
    # 17 de 280 conversas com foto no histórico (6,1%) usaram foto que aparece em outro cliente; +1 descrição genérica.
    "PRIOR_FALSA": 0.07,
    "PESO_SINAIS_SECUNDARIOS": 0.25,
    # grupo "foto" — calibrados no histórico (17 fotos reaproveitadas x 365 únicas), com teto conservador
    "LR_HASH_OUTRO_CLIENTE": 40.0,      # 17/17 nas reaproveitadas, 0/365 nas únicas
    "LR_HASH_OUTRO_PEDIDO_MESMO_CLIENTE": 8.0,
    "LR_EXIF_ANTES_DA_ENTREGA": 30.0,   # 11/17 x 0/365
    "LR_DESCRICAO_GENERICA": 20.0,      # 16/17 x 1/365 (foto de produto que não identifica o item do pedido)
    "LR_DESCRICAO_OUTRA_CATEGORIA": 20.0,
    "LR_ETIQUETA_OUTRO_PEDIDO": 30.0,
    "LR_SOFTWARE_EDICAO": 1.2,          # nos dados aparece só em fotos legítimas: mantido fraco de propósito
    "LR_DESCRICAO_IDENTIFICA_PRODUTO": 0.5,
    "LR_ETIQUETA_MESMO_PEDIDO": 0.3,
    "LR_EXIF_DEPOIS_DA_ENTREGA": 0.7,
    # grupo "historico"
    "LR_TROCAS_12M_ALTO": 1.5,          # n_trocas_12m >= 3
    "LR_TROCAS_12M_MUITO_ALTO": 2.5,    # n_trocas_12m >= 5
    "LR_REENVIOS_ANTERIORES": 1.5,      # >= 2 reenvios/trocas registrados em 12 meses
    "LR_REINCIDENTE_FOTO": 8.0,         # já usou foto reaproveitada em outra conversa
    "LR_SUSPEITA_IMPROCEDENTE_ANTERIOR": 10.0,
    "LR_GRUPO_IDENTIDADE": 15.0,        # compartilha CPF/telefone/e-mail com outro cliente
    # grupo "contexto"
    "LR_RECLAMA_ANTES_DA_ENTREGA": 10.0,
    "LR_FALHA_SISTEMICA": 0.5,          # mesma máquina/lote com várias ocorrências recentes
}
P = PARAMETROS
LIMIAR_FALHA_SISTEMICA = 3      # ocorrências de outros pedidos na mesma máquina/lote
JANELA_FALHA_SISTEMICA_DIAS = 30
SOBREPOSICAO_MIN_IDENTIFICA = 0.5
SOBREPOSICAO_MAX_GENERICA = 0.3

CATEGORIAS_COM_FOTO = ("avaria_vazamento", "item_faltante", "erro_personalizacao")

# Palavras que denunciam o TIPO de produto na descrição visual (para checar coerência com o pedido).
_TIPOS = {
    "capinha": ("capinha", "case", "capa"),
    "copo_garrafa": ("copo", "garrafa"),
    "necessaire": ("necessaire",),
    "bolsa_mala": ("bolsa", "mala"),
    "cosmetico": ("frasco", "pote", "serum", "shampoo", "condicionador", "perfume", "body", "hidratante", "oleo",
                  "creme", "mascara", "ampola", "balm", "gel", "esfoliante", "sabonete", "batom", "base", "corretivo",
                  "capsulas", "suplemento", "leave"),
}
_FOTO_DE_CAIXA = re.compile(r"^(caixa|itens do pedido|embalagem da transportadora)", re.I)


# ---------------------------------------------------------------------------
# Resultado
# ---------------------------------------------------------------------------

@dataclass
class Sinal:
    nome: str
    grupo: str           # foto | historico | contexto
    lr: float
    evidencia: str       # fato verificável (vai para a trilha e para sinalizar_suspeita)

    @property
    def direcao(self) -> str:
        return "contra" if self.lr > 1 else "a_favor"


@dataclass
class Veracidade:
    p_verdadeira: float
    prior_falsa: float
    sinais: list[Sinal] = field(default_factory=list)
    aplicavel: bool = True          # False: categoria sem reclamação a verificar (dúvida, status...)
    tem_foto: bool = False
    grupo_suspeito: list[dict[str, Any]] = field(default_factory=list)

    @property
    def contra(self) -> list[Sinal]:
        return sorted((s for s in self.sinais if s.lr > 1), key=lambda s: -s.lr)

    @property
    def a_favor(self) -> list[Sinal]:
        return [s for s in self.sinais if s.lr < 1]

    @property
    def sinais_fortes(self) -> list[Sinal]:
        return [s for s in self.contra if s.lr >= 8]

    def resumo(self) -> str:
        if not self.aplicavel:
            return "veracidade não se aplica a esta categoria"
        base = f"p_verdadeira={self.p_verdadeira:.2f}"
        if self.contra:
            base += " | contra: " + "; ".join(f"{s.nome} (LR {s.lr:g})" for s in self.contra)
        if self.a_favor:
            base += " | a favor: " + "; ".join(f"{s.nome} (LR {s.lr:g})" for s in self.a_favor)
        if not self.tem_foto:
            base += " | sem foto"
        return base

    def evidencias(self) -> list[str]:
        return [f"[{s.direcao}] {s.nome}: {s.evidencia}" for s in self.sinais]

    def para_sinalizar(self) -> dict[str, str]:
        """Parâmetros prontos para `sinalizar_suspeita` (só fatos verificáveis, nada de impressão)."""
        return {"sinais": "; ".join(s.nome for s in self.contra),
                "evidencias": " | ".join(f"{s.nome}: {s.evidencia}" for s in self.contra)}


# ---------------------------------------------------------------------------
# Texto
# ---------------------------------------------------------------------------

def _norm(texto: Any) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", str(texto or "").lower()) if unicodedata.category(c) != "Mn")


def _tokens(texto: Any) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", _norm(texto)) if len(t) > 2}


def sobreposicao(descricao: str, nome_item: str) -> float:
    """Fração das palavras do nome do item que aparecem na descrição visual."""
    nome = _tokens(nome_item)
    return len(nome & _tokens(descricao)) / len(nome) if nome else 0.0


def tipos_mencionados(texto: str) -> set[str]:
    palavras = set(re.findall(r"[a-z]+", _norm(texto)))
    return {tipo for tipo, chaves in _TIPOS.items() if palavras & set(chaves)}


# ---------------------------------------------------------------------------
# Sinais
# ---------------------------------------------------------------------------

def _sinais_da_foto(amb: Ambiente, f: Fatos, anexo: dict[str, Any]) -> list[Sinal]:
    s: list[Sinal] = []
    aid, desc = anexo["anexo_id"], anexo.get("descricao_visual") or ""
    order_id = f.pedido["order_id"] if f.pedido else None

    # 1. mesma imagem em outro pedido/cliente (busca pela ferramenta: fica no log e respeita o relógio)
    iguais = [a for a in amb.ver_anexos(hash_perceptual=anexo["hash_perceptual"])["anexos"]
              if a["anexo_id"] != aid]
    outros_clientes = sorted({a["customer_id"] for a in iguais if a["customer_id"] != f.customer_id})
    outros_pedidos = sorted({a["order_id"] for a in iguais if a["customer_id"] == f.customer_id
                             and a["order_id"] != order_id})
    if outros_clientes:
        s.append(Sinal("foto_reaproveitada_outro_cliente", "foto", P["LR_HASH_OUTRO_CLIENTE"],
                       f"{aid} tem o mesmo hash_perceptual de foto(s) de {len(outros_clientes)} outro(s) cliente(s): "
                       + ", ".join(sorted({a['anexo_id'] for a in iguais if a['customer_id'] != f.customer_id}))))
    elif outros_pedidos:
        s.append(Sinal("foto_reaproveitada_outro_pedido", "foto", P["LR_HASH_OUTRO_PEDIDO_MESMO_CLIENTE"],
                       f"{aid} é a mesma imagem já enviada para o(s) pedido(s) {', '.join(outros_pedidos)}"))

    # 2. metadados
    entrega = pd.Timestamp(f.pedido["data_entrega"]) if f.pedido and f.pedido.get("data_entrega") else None
    if anexo.get("exif_data_captura") and entrega is not None:
        captura = pd.Timestamp(anexo["exif_data_captura"])
        if captura < entrega:
            s.append(Sinal("exif_antes_da_entrega", "foto", P["LR_EXIF_ANTES_DA_ENTREGA"],
                           f"{aid} capturada em {captura:%Y-%m-%d %H:%M}, antes da entrega ({entrega:%Y-%m-%d})"))
        else:
            s.append(Sinal("exif_depois_da_entrega", "foto", P["LR_EXIF_DEPOIS_DA_ENTREGA"],
                           f"{aid} capturada em {captura:%Y-%m-%d %H:%M}, depois da entrega ({entrega:%Y-%m-%d})"))
    if anexo.get("exif_software"):
        s.append(Sinal("software_de_edicao", "foto", P["LR_SOFTWARE_EDICAO"],
                       f"{aid} passou por '{anexo['exif_software']}' (fraco: comum em fotos legítimas)"))

    # 3. etiqueta com número de pedido
    citados = set(re.findall(r"G[BC]H\d{6}", desc))
    if citados and order_id:
        if order_id in citados:
            s.append(Sinal("etiqueta_do_pedido_visivel", "foto", P["LR_ETIQUETA_MESMO_PEDIDO"],
                           f"{aid} mostra a etiqueta do próprio pedido {order_id}"))
        else:
            s.append(Sinal("etiqueta_de_outro_pedido", "foto", P["LR_ETIQUETA_OUTRO_PEDIDO"],
                           f"{aid} mostra etiqueta de {', '.join(sorted(citados))}, não de {order_id}"))

    # 4. coerência da descrição visual com os itens do pedido (conteúdo, não tom)
    if f.itens and not _FOTO_DE_CAIXA.match(desc.strip()):
        nomes = [i["descricao"] for i in f.itens]
        melhor = max(nomes, key=lambda n: sobreposicao(desc, n))
        ov = sobreposicao(desc, melhor)
        tipos_pedido = set().union(*(tipos_mencionados(n) for n in nomes))
        if any(i.get("tipo_item") == "brinde" for i in f.itens):
            tipos_pedido.add("necessaire")
        tipos_foto = tipos_mencionados(desc)
        if tipos_foto and tipos_pedido and not (tipos_foto & tipos_pedido):
            s.append(Sinal("descricao_de_outro_produto", "foto", P["LR_DESCRICAO_OUTRA_CATEGORIA"],
                           f"{aid} mostra {'/'.join(sorted(tipos_foto))}, mas o pedido é de {'/'.join(sorted(tipos_pedido))}"))
        elif ov >= SOBREPOSICAO_MIN_IDENTIFICA:
            s.append(Sinal("descricao_identifica_o_produto", "foto", P["LR_DESCRICAO_IDENTIFICA_PRODUTO"],
                           f"{aid} descreve '{melhor}' ({ov:.0%} do nome do item)"))
        elif ov < SOBREPOSICAO_MAX_GENERICA:
            s.append(Sinal("descricao_generica", "foto", P["LR_DESCRICAO_GENERICA"],
                           f"{aid} ('{desc[:80]}') não identifica nenhum item do pedido "
                           f"(melhor: '{melhor}', {ov:.0%} do nome)"))
    return s


def _sinais_do_historico(amb: Ambiente, f: Fatos) -> tuple[list[Sinal], list[dict[str, Any]]]:
    s: list[Sinal] = []
    cli = amb.consultar_cliente(f.customer_id)
    if cli["ok"]:
        n = int(cli["cliente"].get("n_trocas_12m") or 0)
        if n >= 5:
            s.append(Sinal("muitas_trocas_12m", "historico", P["LR_TROCAS_12M_MUITO_ALTO"], f"n_trocas_12m = {n}"))
        elif n >= 3:
            s.append(Sinal("trocas_12m_acima_do_normal", "historico", P["LR_TROCAS_12M_ALTO"], f"n_trocas_12m = {n}"))
        um_ano = pd.Timestamp(f.agora) - pd.Timedelta(days=365)
        anteriores = [t for t in cli["trocas_reenvios"] if pd.Timestamp(t["data_solicitacao"]) >= um_ano
                      and t.get("order_id_original") != (f.pedido or {}).get("order_id")]
        if len(anteriores) >= 2:
            s.append(Sinal("reenvios_anteriores", "historico", P["LR_REENVIOS_ANTERIORES"],
                           f"{len(anteriores)} reenvios/trocas em outros pedidos nos últimos 12 meses: "
                           + ", ".join(t["solicitacao_id"] for t in anteriores[:5])))

    # reincidência: o cliente já mandou foto reaproveitada de outro cliente em OUTRO pedido
    # (fotos deste pedido já entram no grupo "foto"; contar de novo aqui seria contagem dupla)
    anexos_cliente = dados.tabela("anexos_conversas")
    anexos_cliente = anexos_cliente[(anexos_cliente["customer_id"] == f.customer_id)
                                    & (anexos_cliente["order_id"] != (f.pedido or {}).get("order_id"))
                                    & (anexos_cliente["enviado_em"] <= pd.Timestamp(f.agora))]
    if not anexos_cliente.empty:
        todos = dados.tabela("anexos_conversas")
        todos = todos[todos["enviado_em"] <= pd.Timestamp(f.agora)]
        hashes_outros = set(todos.loc[todos["customer_id"] != f.customer_id, "hash_perceptual"])
        reincid = anexos_cliente[anexos_cliente["hash_perceptual"].isin(hashes_outros)]
        if not reincid.empty:
            s.append(Sinal("reincidente_foto_reaproveitada", "historico", P["LR_REINCIDENTE_FOTO"],
                           f"em conversa anterior ({', '.join(sorted(set(reincid['conversation_id'])))}) usou foto "
                           "que também aparece em outro cliente"))

    for cid, sus in amb.estado.suspeitas.items():
        if sus["customer_id"] == f.customer_id and sus["status"] == "improcedente" and cid != f.conversation_id:
            s.append(Sinal("suspeita_improcedente_anterior", "historico", P["LR_SUSPEITA_IMPROCEDENTE_ANTERIOR"],
                           f"supervisor julgou improcedente a conversa {cid} ({sus['justificativa']})"))
            break

    grupo = detectar_grupo(f.customer_id)
    if grupo:
        s.append(Sinal("identidade_compartilhada", "historico", P["LR_GRUPO_IDENTIDADE"],
                       "compartilha " + "; ".join(f"{g['campo']} com {g['customer_id']}" for g in grupo)))
    return s, grupo


def detectar_grupo(customer_id: str) -> list[dict[str, str]]:
    """Outros clientes com o mesmo CPF, telefone ou e-mail (contas múltiplas).

    Lê o CRM direto (análise interna) mas devolve só QUAL campo coincide e com QUEM, nunca o valor.
    CEP sozinho não entra: 22 pares de clientes dividem CEP por coincidência nos dados (D-014).
    """
    cli = dados.tabela("clientes")
    linha = cli[cli["customer_id"] == customer_id]
    if linha.empty:
        return []
    out = []
    for campo in ("cpf", "telefone", "email"):
        valor = linha.iloc[0][campo]
        if pd.isna(valor) or not str(valor).strip():
            continue
        outros = cli[(cli[campo] == valor) & (cli["customer_id"] != customer_id)]["customer_id"]
        out += [{"customer_id": o, "campo": campo} for o in outros]
    return out


def _sinais_de_contexto(f: Fatos, categoria: str) -> list[Sinal]:
    s: list[Sinal] = []
    p = f.pedido
    if p is None:
        return s
    if categoria in CATEGORIAS_COM_FOTO and p["status_entrega"] != "entregue":
        s.append(Sinal("reclama_antes_da_entrega", "contexto", P["LR_RECLAMA_ANTES_DA_ENTREGA"],
                       f"reclamação de {categoria} com pedido {p['status_entrega']}"))
    falha = falha_sistemica(p, f.agora, categoria)
    if falha:
        s.append(Sinal("falha_sistemica_plausivel", "contexto", P["LR_FALHA_SISTEMICA"], falha))
    return s


# Origem comum que torna o defeito plausível, por categoria: impressora para personalização, lote para avaria
# (vazamento/tampa de um lote). Item faltante é erro de separação: nem máquina nem lote explicam (D-013).
_ORIGEM_POR_CATEGORIA = {
    "erro_personalizacao": ("maquina_personalizacao", lambda t: t["tipo"] == "nova_producao"),
    "avaria_vazamento": ("lote_produto", lambda t: t["motivo_registrado"].isin(["avaria", "defeito_produto"])),
}


def falha_sistemica(pedido: dict[str, Any], agora: Any, categoria: str) -> str | None:
    """Outros pedidos da mesma origem (impressora ou lote) com ocorrência do mesmo tipo nos últimos 30 dias.

    Muitas ocorrências na mesma origem tornam o defeito plausível (e são a semente da visão 360° / incidente).
    """
    if categoria not in _ORIGEM_POR_CATEGORIA:
        return None
    campo, do_tipo = _ORIGEM_POR_CATEGORIA[categoria]
    valor = pedido.get(campo)
    if not valor:
        return None
    trocas = dados.tabela("trocas_reenvios")
    fim = pd.Timestamp(agora)
    ini = fim - timedelta(days=JANELA_FALHA_SISTEMICA_DIAS)
    trocas = trocas[(trocas["data_solicitacao"] >= ini) & (trocas["data_solicitacao"] <= fim) & do_tipo(trocas)]
    peds = dados.tabela("pedidos")
    mesmos = set(peds.loc[(peds[campo] == valor) & (peds["order_id"] != pedido["order_id"]), "order_id"])
    n = int(trocas["order_id_original"].isin(mesmos).sum())
    if n >= LIMIAR_FALHA_SISTEMICA:
        return (f"{n} ocorrências nos últimos {JANELA_FALHA_SISTEMICA_DIAS} dias em outros pedidos "
                f"de {campo} = {valor}")
    return None


# ---------------------------------------------------------------------------
# Combinação
# ---------------------------------------------------------------------------

def combinar(sinais: list[Sinal], prior_falsa: float = P["PRIOR_FALSA"]) -> float:
    """p_verdadeira a partir da taxa-base e dos LRs, amortecendo sinais correlacionados do mesmo grupo."""
    log_odds_falsa = math.log(prior_falsa / (1 - prior_falsa))
    grupos: dict[str, list[float]] = {}
    for s in sinais:
        grupos.setdefault(s.grupo, []).append(math.log(s.lr))
    for logs in grupos.values():
        contra = sorted((x for x in logs if x > 0), reverse=True)
        favor = sorted(x for x in logs if x < 0)
        for lado in (contra, favor):
            if lado:
                log_odds_falsa += lado[0] + P["PESO_SINAIS_SECUNDARIOS"] * sum(lado[1:])
    p_falsa = 1 / (1 + math.exp(-log_odds_falsa))
    return round(1 - p_falsa, 4)


def avaliar_veracidade(amb: Ambiente, f: Fatos, categoria: str) -> Veracidade:
    """Sinais de veracidade de uma reclamação. Para categorias sem reclamação a verificar, aplicavel=False."""
    if categoria not in CATEGORIAS_COM_FOTO + ("arrependimento_cancelamento", "troca_fragrancia", "atraso_entrega",
                                               "cobranca_duplicada"):
        return Veracidade(p_verdadeira=1.0, prior_falsa=0.0, aplicavel=False)
    sinais: list[Sinal] = []
    fotos = [a for a in f.anexos if a.get("tipo") in ("foto_produto", "foto_embalagem", None)]
    vistos: set[str] = set()
    for anexo in fotos:
        for s in _sinais_da_foto(amb, f, anexo):
            if (s.nome, s.evidencia) not in vistos:
                vistos.add((s.nome, s.evidencia))
                sinais.append(s)
    hist, grupo = _sinais_do_historico(amb, f)
    sinais += hist + _sinais_de_contexto(f, categoria)
    if categoria in ("atraso_entrega", "cobranca_duplicada"):
        # a prova está nos sistemas (rastreio, gateway): só o histórico do cliente importa aqui
        sinais = [s for s in sinais if s.grupo != "foto"]
    return Veracidade(p_verdadeira=combinar(sinais), prior_falsa=P["PRIOR_FALSA"], sinais=sinais,
                      tem_foto=bool(fotos), grupo_suspeito=grupo)

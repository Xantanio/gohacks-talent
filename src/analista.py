"""Agente ANALISTA: acha contatos repetidos com causa comum, abre incidente e avisa quem ainda não reclamou. (D-026)

Duas formas de trabalho (proposta do usuário, validada nos dados em D-025):
- chamado pelo atendente: ao atender um ticket de categoria repetível, o atendente pergunta ao analista se o pedido
  pertence a um padrão (`chamado_pelo_atendente`). Se sim, o analista confirma, abre o incidente e avisa os afetados.
  A passagem é orquestração interna (não há ferramenta de passagem no catálogo) e fica registrada na trilha.
- por conta própria: `varrer` roda todos os detectores e trata cada incidente.

Cada detector compara a TAXA da origem suspeita com a das demais (não basta "3 reclamações"), define a JANELA do
incidente pelas próprias reclamações e separa: quem já reclamou DAQUELE defeito, os afetados que ainda não reclamaram
e os pedidos só para monitorar (ex.: impressos depois da última ocorrência). Também sabe concluir "não há incidente".

Aviso proativo: só com consentimento (a ferramenta confere), sem repetir, priorizado dentro de 300/dia, texto fixo
validado pelo mesmo validador das respostas e sem prometer nada fora da política. Defeito e item faltante só têm
reposição até 7 dias do recebimento: quem já passou disso não recebe a promessa; vai para decisão humana.

Uso:  .\\venv\\Scripts\\python.exe -m src.analista        (varredura por conta própria, no tique 0)
"""
from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd

from . import dados, llm
from .acoes_analista import base_conversas, lotes_por_pedido
from .ferramentas import Ambiente
from .politicas import CAMPANHA, POL, dias_uteis_entre, regra

ATOR_ANALISTA = "agente_analista"
JANELA_ANALISE_DIAS = 30      # reclamações consideradas
JANELA_CLUSTER_DIAS = 21      # separação dos pedidos reclamados (cluster recente)
MIN_RECLAMACOES = 3           # "terceira reclamação"
RAZAO_MIN = 3.0               # taxa da origem >= 3x a das demais
PRAZO_REPOSICAO = int(regra("avaria_vazamento", "prazo_dias_corridos_do_recebimento"))
CUSTO_CANAL = {k: float(v) for k, v in POL["custo_por_conversa_rs"].items()}
CATEGORIAS_REPETIVEIS = {"atraso_entrega", "onde_esta_pedido", "erro_personalizacao", "item_faltante",
                         "avaria_vazamento"}

# Só o DEFEITO em questão (D-026: "nome", "impress" casavam com qualquer conversa e escondiam a PRN-02)
RE_PERS = r"personaliza|inicia|borr|tort|pixel|escorr|cortad|saiu errad|escreveram|letra|nome de outr|cor das"
RE_ATRASO = r"atras|n[ãa]o cheg|rastreio|parad|transfer|prazo|cad[êe]|onde est|n[ãa]o recebi|n[ãa]o andou|entrega"
RE_BRINDE = r"brinde|necessaire|mimo"
RE_AVARIA = r"vaz|tampa|rach|derram"   # defeito de PRODUTO; "amassado/quebrado" costuma ser transporte
PISO_RUIDO = 0.05                        # taxa de reclamação considerada normal quando as demais origens estão em ~0


@dataclass
class Candidato:
    tipo: str                    # maquina | transportadora_uf | turno | lote | gateway
    escopo: str
    titulo: str
    evidencias: list[str]
    reclamaram: list[str]        # pedidos que já reclamaram DESTE problema
    afetados: list[str]          # pedidos no escopo que ainda não reclamaram
    monitorar: list[str] = field(default_factory=list)
    janela: tuple[str, str] | None = None
    tipo_aviso: str | None = None
    texto_aviso: str | None = None
    exige_prazo_reposicao: bool = False   # aviso promete reposição: só dentro de 7 dias do recebimento
    transportadora: str | None = None     # citada no aviso (o validador barra transportadora diferente)
    abrir_chamados: bool = False
    custo_medio_contato: float = 0.0
    taxa_reclamacao: float = 0.0

    @property
    def gravidade(self) -> float:
        """Custo esperado dos contatos que o aviso evita (R$): afetados x taxa observada x custo médio."""
        return round(len(self.afetados) * self.taxa_reclamacao * self.custo_medio_contato, 2)


# ---------------------------------------------------------------------------
# Apoio
# ---------------------------------------------------------------------------

def _reclamacoes(tique: int, padrao: str) -> pd.DataFrame:
    conv = base_conversas(tique)
    agora = pd.Timestamp(dados.agora(tique))
    conv = conv[conv["data_abertura"] >= agora - pd.Timedelta(days=JANELA_ANALISE_DIAS)]
    return conv[conv["texto_cliente"].str.contains(padrao, case=False, na=False) & conv["order_id"].notna()]


def _custo_medio(df: pd.DataFrame) -> float:
    return round(float(df["canal"].map(CUSTO_CANAL).mean()), 2) if not df.empty else 0.0


def _janela(datas: pd.Series, agora: pd.Timestamp) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    datas = datas.dropna()
    datas = datas[datas >= agora - pd.Timedelta(days=JANELA_CLUSTER_DIAS)]
    if datas.empty:
        return None
    return datas.min().normalize(), datas.max().normalize()


def _fmt(d: pd.Timestamp) -> str:
    return d.strftime("%d/%m")


def _continua(g: pd.DataFrame, base: float) -> bool:
    """Semana vizinha ainda faz parte do incidente já confirmado."""
    return g["rec"].sum() >= 2 and g["rec"].mean() >= 2 * base


def semanas_concentradas(peds: pd.DataFrame, origem_col: str, reclamados: set[str], agora: pd.Timestamp
                         ) -> dict[str, dict[str, Any]]:
    """Por origem (impressora, CD): semanas de separação em que a taxa de reclamação ENTRE OS ENTREGUES é >= 3x a das
    demais origens no PERÍODO TODO (piso de ruído 5%). Quem não recebeu ainda não teve como reclamar.
    Base do período todo, e não da mesma semana: com poucas origens (2 CDs), a base semanal oscila e escondia
    a 1ª semana da falha do CD2 (D-026).

    Retorna {origem: {janela: (ini, fim), reclamaram, entregues, taxa, base}} só para origens com >= 3 reclamações
    nas semanas concentradas. A janela vai da 1ª à última data de separação de pedido RECLAMADO nessas semanas.
    """
    df = peds.copy()
    df["entregue"] = df["data_entrega"].notna() & (df["data_entrega"] <= agora)
    df["rec"] = df["order_id"].isin(reclamados)
    df["semana"] = df["data_separacao"].dt.to_period("W")
    ent = df[df["entregue"]]
    out: dict[str, dict[str, Any]] = {}
    for origem in df[origem_col].dropna().unique():
        outras_total = ent[ent[origem_col] != origem]
        base_real = outras_total["rec"].mean() if len(outras_total) else 0.0
        base = max(base_real, PISO_RUIDO)
        semanas = {sem: g for sem, g in ent[ent[origem_col] == origem].groupby("semana")}
        # confirmar: semana com >= 3x a base; delimitar: vizinhas contíguas com >= 2x a base e >= 2 reclamações
        flag = [s for s, g in semanas.items() if g["rec"].sum() and g["rec"].mean() >= RAZAO_MIN * base]
        if flag:
            ordem = sorted(semanas)
            i0, i1 = ordem.index(min(flag)), ordem.index(max(flag))
            while i0 > 0 and _continua(semanas[ordem[i0 - 1]], base):
                i0 -= 1
            while i1 < len(ordem) - 1 and _continua(semanas[ordem[i1 + 1]], base):
                i1 += 1
            flag = ordem[i0:i1 + 1]
        sel = ent[(ent[origem_col] == origem) & ent["semana"].isin(flag)]
        if sel["rec"].sum() < MIN_RECLAMACOES:
            continue
        datas = sel.loc[sel["rec"], "data_separacao"]
        out[origem] = {"janela": (datas.min().normalize(), datas.max().normalize()), "reclamaram": int(sel["rec"].sum()),
                       "entregues": len(sel), "taxa": sel["rec"].mean(), "base": base_real,
                       "semanas": [str(w) for w in flag]}
    return out


# ---------------------------------------------------------------------------
# Detectores
# ---------------------------------------------------------------------------

def _detectar_maquina(tique: int) -> tuple[list[Candidato], list[str]]:
    agora = pd.Timestamp(dados.agora(tique))
    peds = dados.tabela("pedidos")
    impressos = peds[peds["maquina_personalizacao"].notna() & (peds["data_separacao"] <= agora)
                     & (peds["data_separacao"] >= agora - pd.Timedelta(days=JANELA_ANALISE_DIAS))]
    rec = _reclamacoes(tique, RE_PERS)
    rec = rec[rec["order_id"].isin(impressos["order_id"])]
    conc = semanas_concentradas(impressos, "maquina_personalizacao", set(rec["order_id"]), agora)
    maquinas = dados.tabela("maquinas_personalizacao").set_index("maquina_id")
    candidatos, notas = [], []
    for maq in sorted(impressos["maquina_personalizacao"].unique()):
        if maq not in conc:
            n = rec[rec["maquina_personalizacao"] == maq]["order_id"].nunique()
            notas.append(f"{maq}: {n} reclamações de personalização em 30 dias, sem semana concentrada → sem incidente")
            continue
        k = conc[maq]
        ini, fim = k["janela"]
        no_escopo = impressos[(impressos["maquina_personalizacao"] == maq) & (impressos["data_separacao"] >= ini)
                              & (impressos["data_separacao"] < fim + pd.Timedelta(days=1))]
        recl_maq = rec[rec["order_id"].isin(no_escopo["order_id"])]
        reclamaram = sorted(set(recl_maq["order_id"]))
        afetados = sorted(set(no_escopo["order_id"]) - set(reclamaram))
        depois = impressos[(impressos["maquina_personalizacao"] == maq)
                           & (impressos["data_separacao"] >= fim + pd.Timedelta(days=1))]
        m = maquinas.loc[maq] if maq in maquinas.index else {}
        candidatos.append(Candidato(
            tipo="maquina", escopo=f"{maq} ({m.get('cd', '?')}) impressos de {_fmt(ini)} a {_fmt(fim)}",
            titulo=f"Falha de impressão na {maq}",
            evidencias=[f"{k['reclamaram']} de {k['entregues']} pedidos entregues reclamaram de personalização "
                        f"({k['taxa']:.0%}) nas semanas concentradas; demais impressoras no mesmo período: {k['base']:.0%}",
                        f"pedidos reclamados impressos de {_fmt(ini)} a {_fmt(fim)}: {', '.join(reclamaram[:8])}",
                        f"última preventiva {m.get('ultima_manutencao_preventiva', '?')}; cartucho trocado em "
                        f"{m.get('ultima_troca_cartucho', '?')}",
                        f"{len(depois)} pedidos impressos depois de {_fmt(fim)}: só monitorar (sem dado suficiente)"],
            reclamaram=reclamaram, afetados=afetados, monitorar=sorted(depois["order_id"]),
            janela=(_fmt(ini), _fmt(fim)), tipo_aviso=f"falha_impressao_{maq}",
            texto_aviso=(f"Olá! Identificamos uma falha de impressão em parte dos produtos personalizados produzidos "
                         f"entre {_fmt(ini)} e {_fmt(fim)}. Se a personalização do seu pedido chegou (ou chegar) com "
                         "defeito, como borrada, torta, cortada ou com letras faltando, responda esta mensagem com uma "
                         "foto e fazemos uma nova produção sem custo. Se estiver tudo certo, pode desconsiderar este aviso."),
            exige_prazo_reposicao=True, custo_medio_contato=_custo_medio(recl_maq), taxa_reclamacao=k["taxa"]))
    return candidatos, notas


def _detectar_transportadora(tique: int) -> tuple[list[Candidato], list[str]]:
    agora = pd.Timestamp(dados.agora(tique))
    peds = dados.tabela("pedidos")
    transito = peds[(peds["status_entrega"] == "em_transito") & (peds["data_hora_pedido"] <= agora)].copy()
    transito["atrasado"] = transito["data_prometida"] < agora.normalize()
    g = transito.groupby(["transportadora", "uf"]).agg(n=("order_id", "size"), atrasados=("atrasado", "sum"))
    candidatos, notas = [], []
    for transp, sub in g.groupby(level=0):
        outras = transito[transito["transportadora"] != transp]
        base = outras["atrasado"].mean() if len(outras) else 0.0
        sub = sub.droplevel(0)
        sub["taxa"] = sub["atrasados"] / sub["n"]
        ufs = sub[(sub["atrasados"] >= 5) & (sub["taxa"] >= max(0.5, RAZAO_MIN * base * 0.67))].index.tolist()
        if not ufs:
            notas.append(f"{transp}: nenhuma UF com atraso concentrado (taxa geral {sub['atrasados'].sum()}/{sub['n'].sum()}, "
                         f"demais transportadoras {base:.0%}) → sem incidente")
            continue
        escopo_peds = transito[(transito["transportadora"] == transp) & transito["uf"].isin(ufs) & transito["atrasado"]]
        rec = _reclamacoes(tique, RE_ATRASO)
        rec = rec[rec["order_id"].isin(escopo_peds["order_id"])]
        reclamaram = sorted(set(rec["order_id"]))
        afetados = sorted(set(escopo_peds["order_id"]) - set(reclamaram))
        det = ", ".join(f"{u} {int(sub.loc[u, 'atrasados'])}/{int(sub.loc[u, 'n'])}" for u in sorted(ufs))
        candidatos.append(Candidato(
            tipo="transportadora_uf", escopo=f"{transp} / {', '.join(sorted(ufs))}",
            titulo=f"Atraso concentrado na {transp}",
            evidencias=[f"pedidos em trânsito atrasados por UF: {det}",
                        f"taxa de atraso nas demais transportadoras: {base:.0%}",
                        f"{len(reclamaram)} desses pedidos já geraram contato sobre atraso; {len(afetados)} ainda não"],
            reclamaram=reclamaram, afetados=afetados, tipo_aviso=f"atraso_{transp}",
            texto_aviso=(f"Olá! Seu pedido está com atraso na entrega pela transportadora {transp} na sua região. "
                         "Já abrimos um chamado com a transportadora para resolver, e você não precisa fazer nada agora. "
                         "Se tiver qualquer dúvida, é só responder esta mensagem."),
            abrir_chamados=True, transportadora=transp, custo_medio_contato=_custo_medio(rec),
            taxa_reclamacao=len(reclamaram) / max(len(escopo_peds), 1)))
    return candidatos, notas


def _detectar_brinde_cd(tique: int) -> tuple[list[Candidato], list[str]]:
    agora = pd.Timestamp(dados.agora(tique))
    peds = dados.tabela("pedidos")
    itens = dados.tabela("itens_pedido")
    com_brinde = peds[peds["order_id"].isin(itens.loc[itens["tipo_item"] == "brinde", "order_id"])
                      & (peds["data_separacao"] <= agora)
                      & (peds["data_separacao"] >= agora - pd.Timedelta(days=JANELA_ANALISE_DIAS))]
    rec = _reclamacoes(tique, RE_BRINDE)
    rec = rec[rec["order_id"].isin(com_brinde["order_id"])]
    conc = semanas_concentradas(com_brinde, "cd_separacao", set(rec["order_id"]), agora)
    brinde_nome = CAMPANHA.get("brinde", "brinde")
    candidatos, notas = [], []
    for cd in sorted(com_brinde["cd_separacao"].dropna().unique()):
        if cd not in conc:
            n = rec[rec["cd_separacao"] == cd]["order_id"].nunique()
            notas.append(f"brinde {cd}: {n} reclamações em 30 dias, sem semana concentrada → sem incidente")
            continue
        k = conc[cd]
        ini, fim = k["janela"]
        no_escopo = com_brinde[(com_brinde["cd_separacao"] == cd) & (com_brinde["data_separacao"] >= ini)
                               & (com_brinde["data_separacao"] < fim + pd.Timedelta(days=1))]
        recl = rec[rec["order_id"].isin(no_escopo["order_id"])]
        reclamaram = sorted(set(recl["order_id"]))
        afetados = sorted(set(no_escopo["order_id"]) - set(reclamaram))
        candidatos.append(Candidato(
            tipo="turno", escopo=f"separação do brinde no {cd} de {_fmt(ini)} a {_fmt(fim)}",
            titulo=f"Brinde faltando nos pedidos separados no {cd}",
            evidencias=[f"{k['reclamaram']} de {k['entregues']} pedidos entregues reclamaram do brinde ({k['taxa']:.0%}) "
                        f"nas semanas concentradas; outro CD no mesmo período: {k['base']:.0%}",
                        f"separados de {_fmt(ini)} a {_fmt(fim)}"],
            reclamaram=reclamaram, afetados=afetados, janela=(_fmt(ini), _fmt(fim)), tipo_aviso=f"brinde_{cd}",
            texto_aviso=(f"Olá! Alguns pedidos da campanha {CAMPANHA.get('nome', '')} separados entre {_fmt(ini)} e "
                         f"{_fmt(fim)} podem ter saído sem o brinde ({brinde_nome}). Se o seu veio sem, responda esta "
                         "mensagem com uma foto do conteúdo da caixa e enviamos o brinde sem custo. Se veio tudo certo, "
                         "pode desconsiderar este aviso."),
            exige_prazo_reposicao=True, custo_medio_contato=_custo_medio(recl), taxa_reclamacao=k["taxa"]))
    return candidatos, notas


def _detectar_lote(tique: int) -> tuple[list[Candidato], list[str]]:
    """Lote POR ITEM e só quando o cliente cita o produto daquele item (D-025: o lote do pedido gera falso incidente)."""
    rec = _reclamacoes(tique, RE_AVARIA)
    lp = lotes_por_pedido()
    lp = lp[lp["order_id"].isin(rec["order_id"])].merge(rec[["order_id", "texto_cliente"]], on="order_id")
    if lp.empty:
        return [], ["lotes: nenhuma reclamação de defeito de produto com lote → sem incidente"]

    def cita(row: pd.Series) -> bool:  # o nome do produto (1ª palavra significativa) aparece no texto do cliente
        palavras = re.findall(r"[a-zà-ú]{4,}", str(row["descricao"]).lower())
        return bool(palavras) and palavras[0] in row["texto_cliente"].lower()

    atrib = lp[lp.apply(cita, axis=1)]
    por_lote = atrib.groupby("lote")["order_id"].nunique()
    vendidos = lotes_por_pedido().groupby("lote")["order_id"].nunique()
    base = max(por_lote.sum() / max(vendidos.sum(), 1), PISO_RUIDO / 2)
    suspeitos = [(lote, int(n)) for lote, n in por_lote.items()
                 if n >= MIN_RECLAMACOES and (n / vendidos.get(lote, 1)) >= RAZAO_MIN * base]
    if not suspeitos:
        top = por_lote.sort_values(ascending=False).head(1)
        quem = (f"{top.index[0]} com {int(top.iloc[0])} de {int(vendidos.get(top.index[0], 0))}"
                if not top.empty else "nenhum")
        return [], [f"lotes: defeito de produto atribuído ao item citado não se concentra (maior: {quem}; "
                    f"{len(lp) - len(atrib)} reclamações não citam o produto do lote) → sem incidente"]
    return [Candidato(tipo="lote", escopo=f"lote {lote}", titulo=f"Defeito concentrado no lote {lote}",
                      evidencias=[f"{n} pedidos com vazamento/tampa/rachadura citando o produto do lote {lote} "
                                  f"de {int(vendidos.get(lote, 0))} vendidos (base {base:.1%})"],
                      reclamaram=sorted(set(atrib.loc[atrib["lote"] == lote, "order_id"])), afetados=[])
            for lote, n in suspeitos], []


def _detectar_gateway(tique: int) -> tuple[list[Candidato], list[str]]:
    """Cobrança duplicada: últimos 7 dias contra a média semanal das 12 semanas anteriores."""
    agora = pd.Timestamp(dados.agora(tique))
    trx = dados.tabela("pagamentos_gateway")
    trx = trx[trx["status"].isin(["capturada", "paga"]) & (trx["data_hora_criacao"] <= agora)]
    dup = trx.groupby("order_id").filter(lambda x: len(x) > 1)
    if dup.empty:
        return [], ["gateway: nenhuma cobrança duplicada → sem incidente"]
    primeira = dup.groupby("order_id")["data_hora_criacao"].min()
    recentes = primeira[primeira >= agora - pd.Timedelta(days=7)]
    antes = primeira[(primeira < agora - pd.Timedelta(days=7)) & (primeira >= agora - pd.Timedelta(days=91))]
    media = len(antes) / 12
    if len(recentes) < MIN_RECLAMACOES or len(recentes) < RAZAO_MIN * max(media, 1.0):
        return [], [f"gateway: {len(recentes)} cobranças duplicadas nos últimos 7 dias (média semanal anterior "
                    f"{media:.1f}) → sem incidente"]
    det = dup[dup["order_id"].isin(recentes.index)]
    metodos = det.groupby("order_id")["metodo"].apply(lambda s: "+".join(sorted(s))).value_counts().to_dict()
    conv = base_conversas(tique)
    reclamaram = sorted(set(recentes.index) & set(conv["order_id"].dropna()))
    return [Candidato(tipo="gateway",
                      escopo=f"cobranças duplicadas de {_fmt(agora - pd.Timedelta(days=7))} a {_fmt(agora)}",
                      titulo="Pico de cobrança duplicada no gateway",
                      evidencias=[f"{len(recentes)} pedidos com 2+ cobranças efetivas em 7 dias (média semanal "
                                  f"anterior {media:.1f})", f"combinações: {metodos}",
                                  f"{len(reclamaram)} já contataram; estorno é por pedido (atendente, com alçada)"],
                      reclamaram=reclamaram, afetados=sorted(set(recentes.index) - set(reclamaram)))], []


@lru_cache(maxsize=8)
def detectar(tique: int) -> tuple[tuple[Candidato, ...], tuple[str, ...]]:
    """Todos os detectores no tique. Determinístico; depende só dos dados até o tique."""
    cands, notas = [], []
    for det in (_detectar_maquina, _detectar_transportadora, _detectar_brinde_cd, _detectar_lote, _detectar_gateway):
        c, n = det(tique)
        cands += c
        notas += n
    return tuple(sorted(cands, key=lambda c: -c.gravidade)), tuple(notas)


# ---------------------------------------------------------------------------
# Tratamento de um incidente
# ---------------------------------------------------------------------------

@dataclass
class Tratamento:
    candidato: Candidato
    incidente_id: str | None = None
    ja_existia: bool = False
    chamados_abertos: int = 0
    notificados: int = 0
    ignorados_por_motivo: dict[str, int] = field(default_factory=dict)
    fora_do_prazo: list[str] = field(default_factory=list)
    texto_reprovado: list[str] = field(default_factory=list)
    origem: str = "varredura"
    total_avisados: int = 0          # avisos do incidente desde a abertura (não só desta passagem)


def _dentro_do_prazo(order_ids: list[str], agora: pd.Timestamp) -> tuple[list[str], list[str]]:
    """Reposição só até 7 dias do recebimento: entregue há mais tempo não pode receber a promessa."""
    peds = dados.tabela("pedidos").set_index("order_id")
    dentro, fora = [], []
    for oid in order_ids:
        p = peds.loc[oid]
        entregue = pd.notna(p["data_entrega"]) and p["data_entrega"] <= agora
        if entregue and (agora.normalize() - p["data_entrega"]).days > PRAZO_REPOSICAO:
            fora.append(oid)
        else:
            dentro.append(oid)
    return dentro, fora


def _prioridade(order_ids: list[str]) -> list[str]:
    """Dentro do limite diário: Reclame Aqui provável primeiro (cliente com histórico no RA), depois maior receita."""
    peds = dados.tabela("pedidos").set_index("order_id")
    clis = dados.tabela("clientes").set_index("customer_id")
    ra = set(dados.tabela("reclame_aqui")["customer_id"])
    chave = lambda o: (peds.loc[o, "customer_id"] not in ra, -float(clis.loc[peds.loc[o, "customer_id"], "receita_acumulada"]))  # noqa: E731
    return sorted(order_ids, key=chave)


def tratar(an: Ambiente, c: Candidato, origem: str = "varredura") -> Tratamento:
    t = Tratamento(c, origem=origem)
    trilha = {"por_que": f"{c.titulo}: {c.evidencias[0]}", "regra": "analista: taxa >= 3x as demais origens",
              "evidencias": c.evidencias, "origem": f"regra (analista, {origem})",
              "custo_esperado": c.gravidade}
    existente = next((i for i in an.estado.incidentes if i["tipo"] == c.tipo and i["escopo"] == c.escopo), None)
    if existente:  # já tratado (ex.: chamado pelo atendente): não repete escalação nem chamados; só avisa quem faltar
        t.incidente_id, t.ja_existia = existente["incidente_id"], True
        t.origem = str(existente.get("origem") or "").removeprefix("regra (analista, ").removesuffix(")")
    else:
        r = an.abrir_incidente(tipo=c.tipo, escopo=c.escopo, evidencias=" | ".join(c.evidencias), trilha=trilha)
        t.incidente_id = r.get("incidente_id")
    if not c.afetados or not c.texto_aviso:
        return t

    if c.abrir_chamados and not t.ja_existia:
        abertos = {ch["order_id"] for ch in an.estado.chamados if ch["status"] == "aberto"}
        hist = dados.tabela("chamados_transportadora")
        abertos |= set(hist.loc[hist["status"] != "encerrado", "order_id"])
        for oid in c.afetados + c.reclamaram:
            if oid not in abertos:
                r = an.abrir_chamado_transportadora(order_id=oid, motivo="atraso_na_entrega",
                                                    trilha={**trilha, "por_que": f"incidente {t.incidente_id}: {c.titulo}"})
                t.chamados_abertos += bool(r.get("ok"))

    alvo = c.afetados
    if c.exige_prazo_reposicao:
        alvo, t.fora_do_prazo = _dentro_do_prazo(c.afetados, pd.Timestamp(an.agora))
    # o texto do aviso passa pelo mesmo validador das respostas (sem prazo inventado, sem cupom, sem acusação)
    datas = [f"{d}/2026" for d in (c.janela or ())]
    permitidas = {"reimprimir_personalizacao", "reenviar_produto"} if c.exige_prazo_reposicao else set()
    v = llm.validar_resposta(c.texto_aviso, publica=True, ferramentas_permitidas=permitidas,
                             transportadora=c.transportadora, fatos=[" ".join(datas)] + c.evidencias)
    if not v.ok:
        t.texto_reprovado = v.motivos
        return t
    ja_avisados = {n["order_id"] for n in an.estado.notificacoes if n["tipo_aviso"] == c.tipo_aviso}
    alvo = [o for o in alvo if o not in ja_avisados]
    if alvo:
        r = an.notificar_clientes(order_ids=_prioridade(alvo), tipo_aviso=c.tipo_aviso, texto=c.texto_aviso,
                                  trilha={**trilha, "por_que": f"incidente {t.incidente_id}: avisar antes de o cliente "
                                                               f"reclamar ({len(alvo)} afetados no prazo)"})
        t.notificados = r.get("enviados", 0)
        t.ignorados_por_motivo = r.get("ignorados_por_motivo", {})
    t.total_avisados = sum(1 for n in an.estado.notificacoes if n["tipo_aviso"] == c.tipo_aviso)
    if t.fora_do_prazo and not t.ja_existia:
        an.acionar_pessoa(area="supervisão", urgencia="media",
                          assunto=f"{t.incidente_id}: {len(t.fora_do_prazo)} clientes afetados fora do prazo de reposição",
                          contexto=(f"{c.titulo}. {len(t.fora_do_prazo)} pedidos foram entregues há mais de "
                                    f"{PRAZO_REPOSICAO} dias: a política não permite prometer reposição. A falha foi "
                                    "nossa: decidir se estende o prazo e avisa esses clientes também."),
                          trilha={**trilha, "por_que": "decisão de negócio fora da política"})
    return t


# ---------------------------------------------------------------------------
# Formas de trabalho
# ---------------------------------------------------------------------------

def cadeira_analista(amb: Ambiente) -> Ambiente:
    return amb if amb.papel == "analista" else amb.como("analista", ATOR_ANALISTA)


def chamado_pelo_atendente(amb: Ambiente, conversation_id: str, order_id: str | None,
                           categoria: str) -> list[Tratamento]:
    """O atendente percebeu um caso repetível: o analista confere se o pedido pertence a um padrão e, se sim, trata."""
    if not order_id or categoria not in CATEGORIAS_REPETIVEIS:
        return []
    an = cadeira_analista(amb)
    cands, _ = detectar(an.tique)
    meus = [c for c in cands if order_id in c.reclamaram or order_id in c.afetados]
    return [tratar(an, c, origem=f"chamado pelo atendente em {conversation_id}") for c in meus]


@dataclass
class Relatorio:
    tratamentos: list[Tratamento]
    sem_incidente: list[str]


def varrer(amb: Ambiente) -> Relatorio:
    """Por conta própria: roda todos os detectores (com métricas registradas no log) e trata cada incidente."""
    an = cadeira_analista(amb)
    for dim in ("maquina;dia", "transportadora;uf", "cd", "lote"):  # consultas registradas no log, para auditoria
        an.consultar_metricas(agrupar_por=dim, inicio=(an.agora - timedelta(days=JANELA_ANALISE_DIAS)).date().isoformat())
    cands, notas = detectar(an.tique)
    return Relatorio([tratar(an, c) for c in cands], list(notas))


def resumo(rel: Relatorio) -> str:
    L = ["# Analista — incidentes e avisos proativos", ""]
    for t in rel.tratamentos:
        c = t.candidato
        L += [f"## {t.incidente_id} — {c.titulo}", "", f"- escopo: {c.escopo} · aberto por: analista, {t.origem}"]
        L += [f"- {e}" for e in c.evidencias]
        L.append(f"- já reclamaram: {len(c.reclamaram)} · afetados sem reclamar: {len(c.afetados)} · "
                 f"monitorar: {len(c.monitorar)} · contatos evitáveis estimados: R$ {c.gravidade:.2f}")
        if c.texto_aviso:
            L.append(f"- clientes avisados antes de reclamar: **{t.total_avisados}** · não avisados: "
                     f"{t.ignorados_por_motivo or '-'} · fora do prazo de reposição (para decisão humana): "
                     f"{len(t.fora_do_prazo)}" + (f" · chamados abertos: {t.chamados_abertos}"
                                                   if c.abrir_chamados and not t.ja_existia else ""))
            if t.texto_reprovado:
                L.append(f"- ⚠️ aviso NÃO enviado: texto reprovado pelo validador ({'; '.join(t.texto_reprovado)})")
            else:
                L += ["", f"> {c.texto_aviso}"]
        L.append("")
    L += ["## Verificado sem incidente", ""] + [f"- {n}" for n in rel.sem_incidente]
    return "\n".join(L) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="Analista: varredura de incidentes e aviso proativo")
    ap.add_argument("--saida", default="saida")
    a = ap.parse_args()
    amb = Ambiente(papel="analista", ator=ATOR_ANALISTA)
    rel = varrer(amb)
    pasta = Path(a.saida) / f"analista-{pd.Timestamp.now():%Y%m%d-%H%M%S}"
    pasta.mkdir(parents=True, exist_ok=True)
    amb.estado.exportar(pasta)
    (pasta / "analista.md").write_text(resumo(rel), encoding="utf-8")
    print(resumo(rel))
    print(f"saída em {pasta}")


if __name__ == "__main__":
    main()

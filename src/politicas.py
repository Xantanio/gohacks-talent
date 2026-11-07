"""Política de atendimento em Python determinístico (sem LLM), lida de `politicas.csv`.

Pergunta que este módulo responde: "pela POLÍTICA, o que este caso permite e o que falta?".
Não decide se a reclamação é verdadeira (`veracidade.py`) nem escolhe entre aprovar, escalar ou pedir
foto pelo custo esperado (`decisao.py`). A saída é uma `Avaliacao` com:

- situacao:  elegivel | nao_elegivel | falta_informacao | escalar
- passos:    ferramentas sugeridas, na ordem, com parâmetros (inclui solicitar_aprovacao quando passa da alçada)
- fatos:     frases verificáveis que justificam a avaliação (vão para trilha.evidencias)
- `trilha()` monta o dict de trilha (por_que, regra, evidencias, origem="regra") pronto para as ferramentas.

Números (prazos, alçadas, valor mínimo da campanha, feriados) vêm de politicas.csv. Os poucos limites que
só existem dentro do TEXTO da regra (ex.: "Rastreio parado há 3 dias úteis", "Pix compensa em até 30 minutos")
são extraídos do próprio texto por regex; se o texto mudar e o número sumir, o import falha (de propósito).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import TYPE_CHECKING, Any

import pandas as pd

from . import dados

if TYPE_CHECKING:
    from .ferramentas import Ambiente

# ---------------------------------------------------------------------------
# Leitura de politicas.csv
# ---------------------------------------------------------------------------

POL = dados.politicas()
_CAT = POL["categorias"]

CATEGORIAS: tuple[str, ...] = tuple(dict.fromkeys(k.split(".", 1)[0] for k in _CAT))
TAGS_OFICIAIS: tuple[str, ...] = tuple(t.strip() for t in POL["tags_agente"]["tags_agente"].split(";"))


def regra(categoria: str, chave: str, padrao: Any = None) -> Any:
    return _CAT.get(f"{categoria}.{chave}", padrao)


def texto_regra(categoria: str) -> str:
    return regra(categoria, "regra") or ""


def tag_da_categoria(categoria: str) -> str:
    """Tag oficial (13 tags) de uma categoria da política. Sem equivalente explícito -> regra abaixo."""
    tag = regra(categoria, "tag_equivalente")
    if tag:
        return tag
    return {"erro_personalizacao": "Reclamação produto", "reacao_adversa": "Reclamação produto"}.get(categoria, "Outros")


def acoes_permitidas(categoria: str) -> list[str]:
    return [a.strip() for a in str(regra(categoria, "acoes_permitidas", "")).split(";") if a.strip()]


def _numero_no_texto(categoria: str, padrao: str) -> float:
    m = re.search(padrao, texto_regra(categoria))
    if not m:
        raise RuntimeError(f"politicas.csv mudou: não achei {padrao!r} na regra de {categoria}")
    return float(m.group(1))


DIAS_UTEIS_RASTREIO_PARADO = _numero_no_texto("onde_esta_pedido", r"parado há (\d+) dias úteis")
PIX_MINUTOS = _numero_no_texto("pagamento_pendente", r"Pix compensa em até (\d+) minutos")
BOLETO_DIAS_UTEIS = _numero_no_texto("pagamento_pendente", r"boleto em até (\d+) dias úteis")

_LIM = POL["limites_por_papel"]
ALCADA_ATENDENTE = {
    "reenviar_produto": _LIM["atendente.reenvio_ou_reimpressao_max_rs"],
    "reimprimir_personalizacao": _LIM["atendente.reenvio_ou_reimpressao_max_rs"],
    "trocar_produto": _LIM["atendente.troca_max_rs"],
    "reembolsar": _LIM["atendente.reembolso_max_rs"],
}

CAMPANHA = POL["campanha_vigente"]


# ---------------------------------------------------------------------------
# Calendário (dias úteis com os feriados de politicas.csv)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def feriados() -> frozenset[date]:
    return frozenset(date.fromisoformat(d.strip()) for d in POL["prazos_entrega"]["feriados_nacionais"].split(";"))


def dia_util(d: date) -> bool:
    return d.weekday() < 5 and d not in feriados()


def dias_uteis_entre(inicio: date | datetime, fim: date | datetime) -> int:
    """Dias úteis em (inicio, fim]: quantos dias úteis se passaram DEPOIS de `inicio` até `fim` (0 se fim <= inicio)."""
    a, b = _data(inicio), _data(fim)
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        n += dia_util(d)
    return n


def _data(x: date | datetime | pd.Timestamp | str) -> date:
    if isinstance(x, str):
        x = pd.Timestamp(x)
    if isinstance(x, (datetime, pd.Timestamp)):
        return x.date()
    return x


def _ts(x: Any) -> pd.Timestamp | None:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    t = pd.Timestamp(x)
    return None if pd.isna(t) else t


# ---------------------------------------------------------------------------
# Fatos do ticket
# ---------------------------------------------------------------------------

@dataclass
class Fatos:
    """Tudo o que a política precisa saber, já buscado pelas ferramentas de leitura (posição do tique)."""
    conversation_id: str
    customer_id: str
    canal: str
    agora: datetime
    primeiro_relato: datetime          # 1º contato do cliente sobre ESTE pedido (inclui conversas anteriores)
    pedido: dict[str, Any] | None = None
    itens: list[dict[str, Any]] = field(default_factory=list)
    rastreio: list[dict[str, Any]] = field(default_factory=list)
    chamados: list[dict[str, Any]] = field(default_factory=list)
    trocas_pedido: list[dict[str, Any]] = field(default_factory=list)
    anexos: list[dict[str, Any]] = field(default_factory=list)   # da conversa + do pedido
    pagamento: dict[str, Any] | None = None                       # retorno de verificar_pagamento


def coletar_fatos(amb: Ambiente, conversation_id: str, order_id: str | None = None) -> Fatos:
    """Monta `Fatos` usando só as ferramentas de leitura (tudo fica no log). `order_id` sobrepõe o da fila."""
    conv = amb.ler_conversa(conversation_id)
    if not conv["ok"]:
        raise ValueError(conv["erro"])
    meta = conv["conversa"]
    order_id = order_id or meta.get("order_id")
    msgs_cliente = [m["timestamp"] for m in conv["mensagens"] if m["autor"] == "cliente"]
    relato = pd.Timestamp(min(msgs_cliente) if msgs_cliente else meta["data_abertura"])
    f = Fatos(conversation_id=conversation_id, customer_id=meta["customer_id"], canal=meta["canal"],
              agora=amb.agora, primeiro_relato=relato.to_pydatetime())
    anexos = amb.ver_anexos(conversation_id=conversation_id)["anexos"]
    if order_id:
        ped = amb.consultar_pedido(order_id)
        if ped["ok"] and ped["pedido"]["customer_id"] == meta["customer_id"]:
            f.pedido, f.itens, f.rastreio = ped["pedido"], ped["itens"], ped["rastreio"]
            f.chamados = ped["chamados_transportadora"] + [
                c for c in amb.estado.chamados if c["order_id"] == order_id]
            f.trocas_pedido = ped["trocas_reenvios"] + [
                o for o in amb.estado.oms if o["order_id_original"] == order_id]
            vistos = {a["anexo_id"] for a in anexos}
            anexos += [a for a in amb.ver_anexos(order_id=order_id)["anexos"] if a["anexo_id"] not in vistos]
            pag = amb.verificar_pagamento(order_id)
            f.pagamento = pag if pag["ok"] else None
            f.primeiro_relato = min(f.primeiro_relato, _primeiro_contato_do_pedido(order_id, amb.agora))
    f.anexos = anexos
    return f


def _primeiro_contato_do_pedido(order_id: str, agora: datetime) -> datetime:
    """Conversas anteriores sobre o mesmo pedido contam: o cliente não perde o prazo porque ninguém respondeu."""
    datas = []
    for nome in ("historico_conversas", "fila_aberta"):
        df = dados.tabela(nome)
        datas += df.loc[(df["order_id"] == order_id) & (df["data_abertura"] <= pd.Timestamp(agora)),
                        "data_abertura"].tolist()
    return min(datas).to_pydatetime() if datas else agora


# ---------------------------------------------------------------------------
# Resultado
# ---------------------------------------------------------------------------

@dataclass
class Passo:
    ferramenta: str
    params: dict[str, Any] = field(default_factory=dict)
    nota: str = ""
    opcional: bool = False


@dataclass
class Avaliacao:
    categoria: str
    situacao: str                     # elegivel | nao_elegivel | falta_informacao | escalar
    resumo: str                       # uma frase para humano
    passos: list[Passo] = field(default_factory=list)
    alternativas: list[list[Passo]] = field(default_factory=list)  # ex.: reenvio OU reembolso, à escolha do cliente
    fatos: list[str] = field(default_factory=list)
    pendencias: list[str] = field(default_factory=list)
    envolve_dinheiro: bool = False
    valor_acao: float | None = None
    acima_da_alcada: bool = False
    prazo: dict[str, Any] | None = None
    escalar: dict[str, str] | None = None   # {area, urgencia, motivo}

    @property
    def regra(self) -> str:
        return f"categorias.{self.categoria}"

    @property
    def tag(self) -> str:
        return tag_da_categoria(self.categoria)

    def trilha(self, **extra: Any) -> dict[str, Any]:
        return {"por_que": self.resumo, "regra": self.regra, "evidencias": list(self.fatos), "origem": "regra",
                **extra}

    def como_dict(self) -> dict[str, Any]:
        return {**asdict(self), "regra": self.regra, "tag": self.tag}


def _passos_com_alcada(acao: str, params: dict[str, Any], valor: float, justificativa: str) -> tuple[list[Passo], bool]:
    """Dentro da alçada: a própria ação. Acima: solicitar_aprovacao e depois a ação com aprovacao_id."""
    limite = ALCADA_ATENDENTE[acao]
    if valor <= limite:
        return [Passo(acao, {**params, "valor": valor})], False
    return [
        Passo("solicitar_aprovacao", {"acao": acao, "order_id": params["order_id"], "valor": valor,
                                      "justificativa": justificativa},
              nota=f"R$ {valor:.2f} acima da alçada de {acao} (R$ {limite:.2f})"),
        Passo(acao, {**params, "valor": valor, "aprovacao_id": "<da aprovação>"}, nota="depois de aprovado"),
    ], True


# ---------------------------------------------------------------------------
# Avaliação por categoria
# ---------------------------------------------------------------------------

def avaliar(categoria: str, f: Fatos, *, sku_alvo: str | None = None, uso_pct: float | None = None,
            quantidade_faltante: int | None = None) -> Avaliacao:
    """Avalia o caso pela política.

    sku_alvo:   item em questão (faltante, avariado, a trocar). Sem ele, a ação vale para o pedido inteiro.
    uso_pct:    % do produto já usado, declarado pelo cliente (troca de fragrância).
    quantidade_faltante: unidades que faltaram (padrão 1 quando há sku_alvo).
    """
    if categoria not in CATEGORIAS:
        raise ValueError(f"categoria fora da política: {categoria} (válidas: {CATEGORIAS})")
    sem_pedido = {"duvida_produto", "cupom_promocao", "elogio", "sem_interacao", "reacao_adversa"}
    if f.pedido is None and categoria not in sem_pedido:
        return Avaliacao(categoria, "falta_informacao", "Sem pedido identificado: pedir o número do pedido.",
                         passos=[Passo("responder_cliente", nota="pedir o número do pedido")],
                         pendencias=["número do pedido"], fatos=["conversa sem order_id válido para este cliente"])
    return _AVALIADORES[categoria](f, sku_alvo=sku_alvo, uso_pct=uso_pct, quantidade_faltante=quantidade_faltante)


def _fato_pedido(f: Fatos) -> str:
    p = f.pedido
    return (f"pedido {p['order_id']}: R$ {float(p['valor']):.2f}, status {p['status_entrega']}, "
            f"prometido {p.get('data_prometida')}, entregue {p.get('data_entrega') or '-'}")


# ---- entrega: onde está / atraso -------------------------------------------

def _situacao_entrega(f: Fatos) -> dict[str, Any]:
    p = f.pedido
    prometida = _ts(p.get("data_prometida"))
    ultimo = f.rastreio[-1] if f.rastreio else None
    ult_ts = _ts(ultimo["data_hora_evento"]) if ultimo else None
    return {
        "prometida": prometida,
        "atrasado": prometida is not None and p["status_entrega"] in ("em_transito", "aguardando_separacao")
                    and _data(f.agora) > _data(prometida),
        "dias_uteis_atraso": dias_uteis_entre(prometida, f.agora) if prometida is not None else 0,
        "ultimo_evento": ultimo,
        "dias_uteis_parado": dias_uteis_entre(ult_ts, f.agora) if ult_ts is not None else None,
        "chamado_aberto": next((c for c in f.chamados if c.get("status") not in ("encerrado",)), None),
    }


def _onde_esta_pedido(f: Fatos, **_: Any) -> Avaliacao:
    p, e = f.pedido, _situacao_entrega(f)
    fatos = [_fato_pedido(f)]
    if e["ultimo_evento"]:
        fatos.append(f"último evento de rastreio: {e['ultimo_evento']['status']} em "
                     f"{e['ultimo_evento']['data_hora_evento']} ({e['dias_uteis_parado']} dias úteis sem atualização)")
    status = Passo("informar_status", {"conversation_id": f.conversation_id, "order_id": p["order_id"]})
    if e["atrasado"]:
        return _atraso_entrega(f)
    parado = (p["status_entrega"] == "em_transito" and e["dias_uteis_parado"] is not None
              and e["dias_uteis_parado"] >= DIAS_UTEIS_RASTREIO_PARADO)
    if parado and not e["chamado_aberto"]:
        return Avaliacao("onde_esta_pedido", "elegivel",
                         f"Rastreio parado há {e['dias_uteis_parado']} dias úteis (limite "
                         f"{DIAS_UTEIS_RASTREIO_PARADO:.0f}): abrir chamado e informar status.",
                         passos=[Passo("abrir_chamado_transportadora", {"order_id": p["order_id"],
                                                                        "motivo": "rastreio_sem_atualizacao"}), status],
                         fatos=fatos)
    if parado:
        fatos.append(f"chamado já aberto: {e['chamado_aberto'].get('chamado_id')}")
    return Avaliacao("onde_esta_pedido", "elegivel", "Dentro do prazo: informar status e previsão.",
                     passos=[status], fatos=fatos)


def _atraso_entrega(f: Fatos, **_: Any) -> Avaliacao:
    p, e = f.pedido, _situacao_entrega(f)
    fatos = [_fato_pedido(f)]
    limite = regra("atraso_entrega", "limite_dias_uteis_para_reenvio")
    valor = float(p["valor"])
    if p["status_entrega"] == "entregue":
        return Avaliacao("atraso_entrega", "escalar",
                         "Transportadora marca como entregue mas o cliente diz que não recebeu: logística verifica.",
                         passos=[Passo("acionar_pessoa", {"area": "logística", "urgencia": "alta",
                                                          "assunto": f"Entregue sem recebimento — {p['order_id']}"})],
                         fatos=fatos + [f"entrega registrada em {p.get('data_entrega')}"],
                         escalar={"area": "logística", "urgencia": "alta", "motivo": "entrega contestada"})
    if p["status_entrega"] in ("cancelado", "devolvido"):
        return Avaliacao("atraso_entrega", "nao_elegivel", f"Pedido {p['status_entrega']}: informar status.",
                         passos=[Passo("informar_status", {"conversation_id": f.conversation_id,
                                                           "order_id": p["order_id"]})], fatos=fatos)
    extraviado = p["status_entrega"] == "extraviado"
    if extraviado or (e["atrasado"] and e["dias_uteis_atraso"] > limite):
        motivo = "extravio confirmado" if extraviado else f"{e['dias_uteis_atraso']} dias úteis de atraso (> {limite})"
        reenvio, acima_r = _passos_com_alcada("reenviar_produto", {"order_id": p["order_id"],
                                              "motivo": "atraso_ou_extravio"}, valor, motivo)
        reemb, acima_e = _passos_com_alcada("reembolsar", {"order_id": p["order_id"],
                                            "motivo": "atraso_ou_extravio"}, valor, motivo)
        return Avaliacao("atraso_entrega", "elegivel",
                         f"{motivo.capitalize()}: reenviar ou reembolsar, à escolha do cliente.",
                         passos=[Passo("responder_cliente", nota="oferecer reenvio ou reembolso (escolha do cliente)")],
                         alternativas=[reenvio, reemb], fatos=fatos + [motivo], envolve_dinheiro=True,
                         valor_acao=valor, acima_da_alcada=acima_r or acima_e)
    if not e["atrasado"]:
        return _onde_esta_pedido(f)
    fatos.append(f"{e['dias_uteis_atraso']} dias úteis de atraso (até {limite}: chamado com a transportadora)")
    status = Passo("informar_status", {"conversation_id": f.conversation_id, "order_id": p["order_id"]})
    if e["chamado_aberto"]:
        fatos.append(f"chamado já aberto: {e['chamado_aberto'].get('chamado_id')}")
        return Avaliacao("atraso_entrega", "elegivel", "Atraso dentro do limite com chamado já aberto: informar status.",
                         passos=[status], fatos=fatos)
    if p["status_entrega"] == "aguardando_separacao":
        return Avaliacao("atraso_entrega", "escalar", "Prazo vencido e pedido ainda não separado: logística/CD.",
                         passos=[Passo("acionar_pessoa", {"area": "logística", "urgencia": "alta",
                                                          "assunto": f"Pedido não separado após o prazo — {p['order_id']}"}),
                                 status], fatos=fatos,
                         escalar={"area": "logística", "urgencia": "alta", "motivo": "atraso na separação"})
    return Avaliacao("atraso_entrega", "elegivel",
                     f"Atraso de {e['dias_uteis_atraso']} dias úteis (até {limite}): abrir chamado com a transportadora.",
                     passos=[Passo("abrir_chamado_transportadora", {"order_id": p["order_id"],
                                                                    "motivo": "atraso_na_entrega"}), status],
                     fatos=fatos)


# ---- avaria / faltante / personalização (mediante foto, 7 dias do recebimento) -

def _prazo_do_recebimento(f: Fatos, categoria: str) -> tuple[dict[str, Any] | None, str | None]:
    p = f.pedido
    entrega = _ts(p.get("data_entrega"))
    if p["status_entrega"] != "entregue" or entrega is None:
        return None, f"pedido ainda não entregue (status {p['status_entrega']})"
    limite = regra(categoria, "prazo_dias_corridos_do_recebimento")
    dias = (_data(f.primeiro_relato) - _data(entrega)).days
    return {"limite_dias": limite, "dias_do_recebimento_ao_relato": dias, "dentro": dias <= limite,
            "primeiro_relato": f.primeiro_relato.strftime("%Y-%m-%d %H:%M:%S"),
            "entrega": _data(entrega).isoformat()}, None


def _fotos_validas(f: Fatos) -> list[dict[str, Any]]:
    """Fotos enviadas depois da entrega (as de antes não podem mostrar o produto recebido)."""
    entrega = _ts(f.pedido.get("data_entrega")) if f.pedido else None
    return [a for a in f.anexos if a.get("tipo") in ("foto_produto", "foto_embalagem", None)
            and (entrega is None or _ts(a["enviado_em"]) >= entrega)]


def _caso_com_foto(categoria: str, f: Fatos, acao: str, params: dict[str, Any], valor: float,
                   fatos: list[str]) -> Avaliacao:
    prazo, erro = _prazo_do_recebimento(f, categoria)
    if erro:
        return Avaliacao(categoria, "nao_elegivel", f"Não se aplica: {erro}.", fatos=fatos + [erro],
                         passos=[Passo("informar_status", {"conversation_id": f.conversation_id,
                                                           "order_id": f.pedido["order_id"]})])
    fatos = fatos + [f"recebido em {prazo['entrega']}, 1º relato em {prazo['primeiro_relato']} "
                     f"({prazo['dias_do_recebimento_ao_relato']} dias; limite {prazo['limite_dias']})"]
    if not prazo["dentro"]:
        return Avaliacao(categoria, "nao_elegivel", f"Fora do prazo de {prazo['limite_dias']} dias do recebimento.",
                         passos=[Passo("negar_por_politica", {"conversation_id": f.conversation_id,
                                                              "regra": categoria})], fatos=fatos, prazo=prazo)
    fotos = _fotos_validas(f)
    if regra(categoria, "exige_foto") and not fotos:
        return Avaliacao(categoria, "falta_informacao", "Dentro do prazo, mas a política exige foto: solicitar foto.",
                         passos=[Passo("solicitar_foto", {"conversation_id": f.conversation_id})],
                         fatos=fatos + ["nenhuma foto enviada depois da entrega"], pendencias=["foto"], prazo=prazo,
                         envolve_dinheiro=True, valor_acao=valor)
    fatos += [f"foto {a['anexo_id']}: {a.get('descricao_visual')}" for a in fotos]
    passos, acima = _passos_com_alcada(acao, params, valor, f"{categoria} com foto, dentro do prazo")
    return Avaliacao(categoria, "elegivel",
                     f"Dentro do prazo e com foto: {acao} de R$ {valor:.2f}"
                     + (" (acima da alçada: precisa de aprovação)." if acima else " (dentro da alçada)."),
                     passos=passos, fatos=fatos, prazo=prazo, envolve_dinheiro=True, valor_acao=valor,
                     acima_da_alcada=acima)


def _valor_itens(f: Fatos, sku: str | None, quantidade: int | None = None) -> tuple[float, list[str]]:
    """Valor de referência da ação: item(ns) do sku (brinde vale o custo do produto) ou o pedido inteiro."""
    if not sku:
        return float(f.pedido.get("valor_produtos") or f.pedido["valor"]), ["ação sobre o pedido inteiro"]
    linhas = [i for i in f.itens if i["sku"] == sku]
    if not linhas:
        return float(f.pedido.get("valor_produtos") or f.pedido["valor"]), [f"sku {sku} não está no pedido"]
    total = sum(float(i["valor_total"]) for i in linhas)
    qtd = sum(int(i["quantidade"]) for i in linhas)
    if total <= 0:  # brinde: preço zero, vale o custo
        prods = dados.tabela("produtos")
        total = float(prods.loc[prods["sku"] == sku, "custo_unitario"].iloc[0]) * qtd
        return round(total, 2), [f"{sku} é brinde: valor pelo custo unitário (R$ {total:.2f})"]
    unit = total / qtd
    q = quantidade or qtd
    return round(unit * min(q, qtd), 2), [f"{q} un. de {sku} a R$ {unit:.2f}"]


def brinde_devido(f: Fatos) -> tuple[bool, str]:
    """O pedido dá direito ao brinde da campanha vigente? (marca, valor mínimo e período de politicas.csv)."""
    p = f.pedido
    marcas = {m.strip() for m in CAMPANHA["marcas"].split(";")}
    data = _data(p["data_pedido"])
    ini, fim = date.fromisoformat(CAMPANHA["inicio"]), date.fromisoformat(CAMPANHA["fim"])
    base = float(p.get("valor_produtos") or 0) - float(p.get("desconto") or 0)
    if p["marca"] not in marcas:
        return False, f"marca {p['marca']} fora da campanha {CAMPANHA['nome']}"
    if not ini <= data <= fim:
        return False, f"pedido de {data} fora do período da campanha ({ini} a {fim})"
    if float(p.get("valor_produtos") or 0) < CAMPANHA["valor_minimo_rs"]:
        return False, f"valor dos produtos R$ {float(p.get('valor_produtos') or 0):.2f} abaixo do mínimo R$ {CAMPANHA['valor_minimo_rs']:.2f}"
    return True, f"campanha {CAMPANHA['nome']}: pedido de {data}, produtos R$ {float(p['valor_produtos']):.2f} (base c/ desconto R$ {base:.2f})"


def _item_faltante(f: Fatos, sku_alvo: str | None = None, quantidade_faltante: int | None = None, **_: Any) -> Avaliacao:
    fatos = [_fato_pedido(f)]
    # Sem sku_alvo a ação vale para o pedido inteiro; quem chama (classificação) diz se a falta é do brinde.
    brinde = next((i for i in f.itens if i.get("tipo_item") == "brinde"), None)
    eh_brinde = sku_alvo is not None and (sku_alvo == (brinde or {}).get("sku") or sku_alvo.startswith("GBT-"))
    if eh_brinde:
        devido, motivo = brinde_devido(f)
        fatos.append(motivo)
        if not devido:
            return Avaliacao("item_faltante", "nao_elegivel", f"Brinde não era devido: {motivo}.",
                             passos=[Passo("negar_por_politica", {"conversation_id": f.conversation_id,
                                                                  "regra": "campanha_vigente.brinde"})], fatos=fatos)
        if brinde is None:  # devido pela campanha, mas o pedido foi gerado sem a linha do brinde
            prods = dados.tabela("produtos")
            valor = round(float(prods.loc[prods["sku"] == sku_alvo, "custo_unitario"].iloc[0]), 2)
            return _caso_com_foto("item_faltante", f, "reenviar_produto",
                                  {"order_id": f.pedido["order_id"], "motivo": "item_faltante"}, valor,
                                  fatos + ["brinde devido mas ausente em itens_pedido (falha na geração do pedido)",
                                           f"{sku_alvo} é brinde: valor pelo custo unitário (R$ {valor:.2f})"])
    valor, notas = _valor_itens(f, sku_alvo, quantidade_faltante)
    return _caso_com_foto("item_faltante", f, "reenviar_produto",
                          {"order_id": f.pedido["order_id"], "motivo": "item_faltante"}, valor, fatos + notas)


def _avaria_vazamento(f: Fatos, sku_alvo: str | None = None, **_: Any) -> Avaliacao:
    valor, notas = _valor_itens(f, sku_alvo)
    return _caso_com_foto("avaria_vazamento", f, "reenviar_produto",
                          {"order_id": f.pedido["order_id"], "motivo": "avaria"}, valor, [_fato_pedido(f)] + notas)


def _erro_personalizacao(f: Fatos, sku_alvo: str | None = None, **_: Any) -> Avaliacao:
    p = f.pedido
    fatos = [_fato_pedido(f)]
    if not p["order_id"].startswith("GCH") or not p.get("personalizacao"):
        return Avaliacao("erro_personalizacao", "nao_elegivel", "Pedido não é Gocase personalizado.", fatos=fatos)
    textos = [f"{i['sku']}: '{i.get('texto_personalizacao')}' ({i.get('personalizacao')})"
              for i in f.itens if i.get("texto_personalizacao") or i.get("personalizacao")]
    fatos += [f"personalização pedida — {t}" for t in textos]
    correta = next((i.get("texto_personalizacao") for i in f.itens if i.get("texto_personalizacao")), None)
    valor, notas = _valor_itens(f, sku_alvo)
    params = {"order_id": p["order_id"], "personalizacao_correta": correta}
    return _caso_com_foto("erro_personalizacao", f, "reimprimir_personalizacao", params, valor, fatos + notas)


# ---- dinheiro: duplicidade, pagamento pendente, arrependimento --------------

def _cobranca_duplicada(f: Fatos, **_: Any) -> Avaliacao:
    p, pag = f.pedido, f.pagamento
    if pag is None:
        return Avaliacao("cobranca_duplicada", "escalar", "Sem dados do gateway: supervisão verifica.",
                         escalar={"area": "supervisão", "urgencia": "media", "motivo": "gateway indisponível"})
    r = pag["resumo"]
    fatos = [_fato_pedido(f), f"gateway: {r['n_cobrancas_efetivas']} cobranças efetivas, cobrado R$ {r['total_cobrado']:.2f}, "
                              f"estornado R$ {r['total_estornado']:.2f}, a mais R$ {r['cobrado_a_mais']:.2f}"]
    if not r["indicio_cobranca_duplicada"]:
        pend = [t for t in pag["transacoes"] if t["status"] == "pendente"]
        if pend:
            return _pagamento_pendente(f)
        return Avaliacao("cobranca_duplicada", "nao_elegivel",
                         "Gateway não mostra cobrança em duplicidade: explicar e pedir comprovante se o cliente insistir.",
                         passos=[Passo("responder_cliente", nota="explicar a cobrança única; pedir comprovante/fatura")],
                         fatos=fatos, pendencias=["comprovante da segunda cobrança"])
    valor = r["cobrado_a_mais"]
    passos, acima = _passos_com_alcada("reembolsar", {"order_id": p["order_id"], "motivo": "cobranca_duplicada"},
                                       valor, f"duplicidade confirmada no gateway (R$ {valor:.2f} a mais)")
    return Avaliacao("cobranca_duplicada", "elegivel",
                     f"Duplicidade confirmada no gateway: estornar R$ {valor:.2f}"
                     + (" (acima da alçada: precisa de aprovação)." if acima else "."),
                     passos=passos, fatos=fatos, envolve_dinheiro=True, valor_acao=valor, acima_da_alcada=acima)


def _pagamento_pendente(f: Fatos, **_: Any) -> Avaliacao:
    p, pag = f.pedido, f.pagamento
    fatos = [_fato_pedido(f), f"status de pagamento no ERP: {p.get('status_pagamento')}"]
    if pag is None:
        return Avaliacao("pagamento_pendente", "escalar", "Sem dados do gateway: supervisão verifica.", fatos=fatos,
                         escalar={"area": "supervisão", "urgencia": "media", "motivo": "gateway indisponível"})
    efetivas = [t for t in pag["transacoes"] if t["status"] in ("capturada", "paga")]
    pendentes = [t for t in pag["transacoes"] if t["status"] == "pendente"]
    if efetivas and p.get("status_pagamento") != "aprovado":
        fatos.append(f"gateway confirma pagamento ({efetivas[0]['transacao_id']}) mas o ERP não aprovou")
        return Avaliacao("pagamento_pendente", "escalar",
                         "Pago no gateway e pendente no ERP: inconsistência de sistema, supervisão resolve.",
                         passos=[Passo("acionar_pessoa", {"area": "supervisão", "urgencia": "alta",
                                                          "assunto": f"Pagamento confirmado e pedido parado — {p['order_id']}"})],
                         fatos=fatos, escalar={"area": "supervisão", "urgencia": "alta", "motivo": "gateway x ERP"})
    if efetivas:
        return Avaliacao("pagamento_pendente", "elegivel", "Pagamento já aprovado: informar status.",
                         passos=[Passo("informar_status", {"conversation_id": f.conversation_id,
                                                           "order_id": p["order_id"]})], fatos=fatos)
    if not pendentes:
        return Avaliacao("pagamento_pendente", "falta_informacao", "Gateway sem transação para o pedido: pedir comprovante.",
                         passos=[Passo("responder_cliente", nota="pedir comprovante de pagamento")], fatos=fatos,
                         pendencias=["comprovante de pagamento"])
    t = pendentes[-1]
    criada = _ts(t["data_hora_criacao"])
    if t["metodo"] == "pix":
        dentro = (pd.Timestamp(f.agora) - criada) <= pd.Timedelta(minutes=PIX_MINUTOS)
        janela = f"{PIX_MINUTOS:.0f} minutos"
    else:
        dentro = dias_uteis_entre(criada, f.agora) <= BOLETO_DIAS_UTEIS
        janela = f"{BOLETO_DIAS_UTEIS:.0f} dias úteis"
    fatos.append(f"transação {t['transacao_id']} ({t['metodo']}) pendente desde {t['data_hora_criacao']}; janela {janela}")
    if dentro:
        return Avaliacao("pagamento_pendente", "elegivel", f"Dentro da janela de compensação ({janela}): orientar a aguardar.",
                         passos=[Passo("responder_cliente", nota=f"{t['metodo']} compensa em até {janela}")], fatos=fatos)
    return Avaliacao("pagamento_pendente", "escalar", f"Pagamento pendente além de {janela}: supervisão verifica no gateway.",
                     passos=[Passo("acionar_pessoa", {"area": "supervisão", "urgencia": "alta",
                                                      "assunto": f"Pagamento não compensado — {p['order_id']}"})],
                     fatos=fatos, escalar={"area": "supervisão", "urgencia": "alta", "motivo": "compensação atrasada"})


def _arrependimento_cancelamento(f: Fatos, **_: Any) -> Avaliacao:
    p = f.pedido
    fatos = [_fato_pedido(f)]
    if p["status_entrega"] == "cancelado":
        return Avaliacao("arrependimento_cancelamento", "nao_elegivel", "Pedido já cancelado: informar status.",
                         passos=[Passo("informar_status", {"conversation_id": f.conversation_id,
                                                           "order_id": p["order_id"]})], fatos=fatos)
    if not p["ja_separado"]:
        return Avaliacao("arrependimento_cancelamento", "elegivel",
                         "Antes da separação: cancelar com estorno integral (sem limite de valor).",
                         passos=[Passo("cancelar_pedido", {"order_id": p["order_id"], "motivo": "arrependimento"})],
                         fatos=fatos + ["pedido ainda não separado"], envolve_dinheiro=True,
                         valor_acao=float(p["valor"]))
    if p["status_entrega"] != "entregue":
        return Avaliacao("arrependimento_cancelamento", "nao_elegivel",
                         "Já separado e ainda não entregue: não dá para cancelar; após receber, tem 7 dias para devolver.",
                         passos=[Passo("responder_cliente", nota="explicar que o direito de arrependimento vale "
                                                                  "por 7 dias após o recebimento")],
                         fatos=fatos + [f"separado em {p.get('data_separacao')}"])
    prazo, _erro = _prazo_do_recebimento(f, "arrependimento_cancelamento")
    fatos.append(f"recebido em {prazo['entrega']}, 1º relato {prazo['dias_do_recebimento_ao_relato']} dias depois "
                 f"(limite {prazo['limite_dias']})")
    if not prazo["dentro"]:
        return Avaliacao("arrependimento_cancelamento", "nao_elegivel",
                         f"Fora dos {prazo['limite_dias']} dias do recebimento (CDC art. 49): negar explicando a política.",
                         passos=[Passo("negar_por_politica", {"conversation_id": f.conversation_id,
                                                              "regra": "arrependimento_cancelamento"})],
                         fatos=fatos, prazo=prazo)
    valor = float(p["valor"])
    passos, acima = _passos_com_alcada("reembolsar", {"order_id": p["order_id"], "motivo": "arrependimento"},
                                       valor, "arrependimento em até 7 dias do recebimento (CDC art. 49)")
    return Avaliacao("arrependimento_cancelamento", "elegivel",
                     f"Arrependimento dentro de {prazo['limite_dias']} dias: reembolso integral de R$ {valor:.2f} com coleta reversa"
                     + (" (acima da alçada: precisa de aprovação)." if acima else "."),
                     passos=passos, fatos=fatos, prazo=prazo, envolve_dinheiro=True, valor_acao=valor,
                     acima_da_alcada=acima, pendencias=["combinar coleta reversa"])


def _troca_fragrancia(f: Fatos, sku_alvo: str | None = None, uso_pct: float | None = None, **_: Any) -> Avaliacao:
    p = f.pedido
    fatos = [_fato_pedido(f)]
    cat = "troca_fragrancia"
    prazo, erro = _prazo_do_recebimento(f, cat)
    if erro:
        return Avaliacao(cat, "nao_elegivel", f"Não se aplica: {erro}.", fatos=fatos + [erro])
    fatos.append(f"{prazo['dias_do_recebimento_ao_relato']} dias do recebimento (limite {prazo['limite_dias']})")
    trocas = [t for t in f.trocas_pedido if t.get("tipo") == "troca"]
    if not prazo["dentro"] or len(trocas) >= regra(cat, "max_trocas_por_pedido"):
        motivo = "fora do prazo" if not prazo["dentro"] else "pedido já teve troca"
        return Avaliacao(cat, "nao_elegivel", f"Troca negada: {motivo}.",
                         passos=[Passo("negar_por_politica", {"conversation_id": f.conversation_id, "regra": cat})],
                         fatos=fatos + [motivo], prazo=prazo)
    max_uso = regra(cat, "max_produto_usado_pct")
    if uso_pct is None:
        return Avaliacao(cat, "falta_informacao",
                         f"Dentro do prazo: confirmar que usou no máximo {max_uso}% e qual fragrância deseja.",
                         passos=[Passo("responder_cliente", nota=f"perguntar % de uso (máx. {max_uso}%) e a nova fragrância")],
                         fatos=fatos, pendencias=["% de uso", "sku desejado"], prazo=prazo)
    if uso_pct > max_uso:
        return Avaliacao(cat, "nao_elegivel", f"Uso de {uso_pct:.0f}% acima de {max_uso}%.",
                         passos=[Passo("negar_por_politica", {"conversation_id": f.conversation_id, "regra": cat})],
                         fatos=fatos + [f"cliente declarou {uso_pct:.0f}% de uso"], prazo=prazo)
    valor, notas = _valor_itens(f, sku_alvo or p["sku"])
    passos, acima = _passos_com_alcada("trocar_produto", {"order_id": p["order_id"]}, valor, "troca de fragrância na política")
    return Avaliacao(cat, "elegivel", f"Troca dentro da política (R$ {valor:.2f}).", passos=passos,
                     fatos=fatos + notas, prazo=prazo, envolve_dinheiro=True, valor_acao=valor, acima_da_alcada=acima,
                     pendencias=["sku_novo escolhido pelo cliente"])


def _alteracao_endereco(f: Fatos, **_: Any) -> Avaliacao:
    p = f.pedido
    fatos = [_fato_pedido(f)]
    if p["ja_separado"] or p["status_entrega"] == "cancelado":
        return Avaliacao("alteracao_endereco", "nao_elegivel", "Pedido já separado: não é possível alterar o endereço.",
                         passos=[Passo("negar_por_politica", {"conversation_id": f.conversation_id,
                                                              "regra": "alteracao_endereco"})],
                         fatos=fatos + [f"separado em {p.get('data_separacao') or p['status_entrega']}"])
    return Avaliacao("alteracao_endereco", "elegivel", "Antes da separação: alterar o endereço.",
                     passos=[Passo("alterar_endereco", {"order_id": p["order_id"]}, nota="novo_endereco vem da mensagem")],
                     fatos=fatos + ["pedido ainda não separado"], pendencias=["novo endereço completo"])


# ---- sem dinheiro -----------------------------------------------------------

def _reacao_adversa(f: Fatos, sku_alvo: str | None = None, **_: Any) -> Avaliacao:
    cat = "reacao_adversa"
    prazo_h = regra(cat, "prazo_registro_horas")
    horas = round((pd.Timestamp(f.agora) - pd.Timestamp(f.primeiro_relato)).total_seconds() / 3600, 1)
    p = f.pedido
    sku = sku_alvo or (p["sku"] if p else None)
    lote = next((i.get("lote") for i in f.itens if i["sku"] == sku), None) or (p or {}).get("lote_produto")
    fatos = [f"relato em {f.primeiro_relato:%Y-%m-%d %H:%M}: {horas}h decorridas de {prazo_h}h"]
    if p:
        fatos.insert(0, _fato_pedido(f))
        if not p["order_id"].startswith("GBH"):
            return Avaliacao(cat, "nao_elegivel", "Produto Gocase não é cosmético/suplemento: tratar como reclamação de produto.",
                             fatos=fatos)
    fotos = [a["anexo_id"] for a in f.anexos]
    urgencia = "critica" if horas >= prazo_h * 0.75 else "alta"
    exige = regra(cat, "registro_exige", "")
    return Avaliacao(cat, "escalar",
                     f"Reação adversa: orientar suspensão do uso já e acionar o supervisor para registrar "
                     f"cosmetovigilância em até {prazo_h}h (restam {max(prazo_h - horas, 0):.1f}h).",
                     passos=[Passo("orientar_suspensao_uso", {"conversation_id": f.conversation_id}),
                             Passo("acionar_pessoa", {"area": "supervisão", "urgencia": urgencia,
                                                      "assunto": f"Cosmetovigilância — {f.conversation_id}"},
                                   nota=f"registrar_cosmetovigilancia(sku={sku}, lote={lote}, fotos={fotos or '-'})"),
                             Passo("trocar_produto", {"order_id": p["order_id"] if p else None}, opcional=True,
                                   nota="se o cliente pedir; não substitui o registro")],
                     fatos=fatos, prazo={"limite_horas": prazo_h, "horas_decorridas": horas},
                     pendencias=[x for x, falta in (("lote", not lote), ("fotos", not fotos)) if falta],
                     escalar={"area": "supervisão", "urgencia": urgencia,
                              "motivo": f"cosmetovigilância obrigatória ({exige})"})


def _simples(categoria: str, ferramenta: str, resumo: str, params_fn=None):
    def avaliador(f: Fatos, **_: Any) -> Avaliacao:
        params = params_fn(f) if params_fn else {"conversation_id": f.conversation_id}
        return Avaliacao(categoria, "elegivel", resumo, passos=[Passo(ferramenta, params)],
                         fatos=[_fato_pedido(f)] if f.pedido else [])
    return avaliador


_AVALIADORES = {
    "onde_esta_pedido": _onde_esta_pedido,
    "atraso_entrega": _atraso_entrega,
    "item_faltante": _item_faltante,
    "avaria_vazamento": _avaria_vazamento,
    "erro_personalizacao": _erro_personalizacao,
    "cobranca_duplicada": _cobranca_duplicada,
    "pagamento_pendente": _pagamento_pendente,
    "arrependimento_cancelamento": _arrependimento_cancelamento,
    "troca_fragrancia": _troca_fragrancia,
    "alteracao_endereco": _alteracao_endereco,
    "reacao_adversa": _reacao_adversa,
    "duvida_produto": _simples("duvida_produto", "responder_duvida", "Dúvida: responder pela ficha do produto."),
    "cupom_promocao": _simples("cupom_promocao", "validar_cupom", "Cupom: validar pelas regras da plataforma.",
                               lambda f: {"codigo": "<do texto>", "order_id": (f.pedido or {}).get("order_id")}),
    "elogio": _simples("elogio", "agradecer", "Elogio: agradecer."),
    "sem_interacao": _simples("sem_interacao", "encerrar_conversa", "Só o bot falou: encerrar sem ação."),
}
assert set(_AVALIADORES) == set(CATEGORIAS), set(CATEGORIAS) ^ set(_AVALIADORES)

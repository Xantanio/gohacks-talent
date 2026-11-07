"""Simulação das ferramentas de `ferramentas.csv` sobre os CSVs.

Leitura aqui; ações (reenviar, reembolsar, solicitar_aprovacao, sinalizar_suspeita, acionar_pessoa...) em `acoes.py`.

Contrato (igual para todas):
- Nome, parâmetros e papéis vêm de `ferramentas.csv` e são validados em cada chamada
  (parâmetro obrigatório ausente, parâmetro desconhecido ou papel não permitido -> erro, sem executar).
- Retorno: {"ok": True, ...dados} ou {"ok": False, "erro": "..."}.
- Toda chamada vai para `Ambiente.log` (tique, data/hora simulada, papel, ator, ferramenta, parâmetros, ok, trilha).
- Trilha de decisão: ferramentas que ALTERAM estado exigem `trilha={"por_que": ...}` (opcionalmente regra,
  evidencias, alternativas, p_verdadeira, custo_esperado). Sem o "porquê", a ação é recusada. Ver `Trilha`.
- Vários papéis (atendente/analista/supervisor) compartilham o mesmo `Estado` via `Ambiente.como(...)`.
- Respeita o relógio: nada com data posterior ao "agora" do tique aparece (pedidos ainda não criados,
  mensagens/anexos/eventos de rastreio/transações futuras).
- Minimização de dados pessoais: CPF, telefone e número de cartão não saem das ferramentas
  (consultar_cliente omite; ler_conversa mascara no texto; verificar_pagamento omite final do cartão).
"""
from __future__ import annotations

import json
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterator, TypedDict

import pandas as pd

from . import dados
from .estado import Estado

# ---------------------------------------------------------------------------
# Catálogo (ferramentas.csv)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Especificacao:
    nome: str
    papeis: frozenset[str]
    obrigatorios: tuple[str, ...]
    opcionais: tuple[str, ...]
    limites: dict[str, Any]
    descricao: str

    @property
    def parametros(self) -> tuple[str, ...]:
        return self.obrigatorios + self.opcionais


def carregar_catalogo() -> dict[str, Especificacao]:
    df = pd.read_csv(dados.DIR_DADOS / "ferramentas.csv", dtype=str).fillna("")
    catalogo: dict[str, Especificacao] = {}
    for _, r in df.iterrows():
        params = [p.strip() for p in r["parametros"].split(";") if p.strip()]
        catalogo[r["ferramenta"]] = Especificacao(
            nome=r["ferramenta"],
            papeis=frozenset(p.strip() for p in r["papeis_permitidos"].split(";") if p.strip()),
            obrigatorios=tuple(p.rstrip("*") for p in params if p.endswith("*")),
            opcionais=tuple(p for p in params if not p.endswith("*")),
            limites=json.loads(r["limites"] or "{}"),
            descricao=r["descricao"],
        )
    return catalogo


CATALOGO = carregar_catalogo()

# ---------------------------------------------------------------------------
# Mascaramento de dados pessoais
# ---------------------------------------------------------------------------

_RE_CPF = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
_RE_CARTAO = re.compile(r"\b(?:\d[ .-]?){13,19}\b")
_RE_TELEFONE = re.compile(r"(?:\+?55\s?)?\(?\b\d{2}\)?\s?9?\d{4}[-\s]?\d{4}\b")


def mascarar_pii(texto: str | None) -> str | None:
    """Troca CPF, cartão e telefone por marcadores. Use também no texto que sai para o cliente."""
    if not texto:
        return texto
    texto = _RE_CPF.sub("[CPF omitido]", texto)
    texto = _RE_CARTAO.sub("[cartão omitido]", texto)
    texto = _RE_TELEFONE.sub("[telefone omitido]", texto)
    return texto


CANAIS_PUBLICOS = frozenset({"reclame_aqui"})
_RE_PEDIDO = re.compile(r"\b(G[BC]H)\d{3}(\d{3})\b")
# Formatos de rastreio dos dados: EJ/CA + 9 dígitos + BR, VL + 11, RPS + 9, TN + 10.
_RE_RASTREIO = re.compile(r"\b(?:[A-Z]{2}\d{9}BR|VL\d{11}|RPS\d{9}|TN\d{10})\b")


def mascarar_publico(texto: str | None) -> str | None:
    """Para resposta pública (Reclame Aqui): PII + número de pedido mascarado (GBH***723) + sem código de rastreio."""
    if not texto:
        return texto
    texto = mascarar_pii(texto)
    texto = _RE_PEDIDO.sub(r"\1***\2", texto)
    return _RE_RASTREIO.sub("[código de rastreio omitido]", texto)


# ---------------------------------------------------------------------------
# Ambiente (relógio + papel + log)
# ---------------------------------------------------------------------------


# Ferramentas que só leem: não exigem trilha e não entram em `Estado.acoes`.
FERRAMENTAS_LEITURA = frozenset({
    "ler_conversa", "consultar_pedido", "consultar_cliente", "consultar_politicas", "buscar_conversas",
    "consultar_metricas", "verificar_pagamento", "validar_cupom", "ver_anexos",
})


class Trilha(TypedDict, total=False):
    """Por que o agente tomou a ação. Só `por_que` é obrigatório; o resto deixa a decisão auditável.

    por_que         frase curta, legível por humano ("Avaria com foto coerente, pedido de R$ 89, dentro da alçada")
    regra           chave da política aplicada (ex.: "categorias.avaria_vazamento")
    evidencias      fatos verificáveis usados (ex.: ["anexo AN123: vazamento visível", "hash sem repetição"])
    alternativas    ações consideradas e seu custo esperado (ex.: {"reenviar_produto": 12.3, "sinalizar_suspeita": 18.0})
    p_verdadeira    probabilidade estimada de a reclamação proceder
    custo_esperado  custo esperado da ação escolhida (R$)
    origem          quem decidiu: "regra" (Python determinístico), "jev", "llm" ou "humano"
    """
    por_que: str
    regra: str
    evidencias: list[str]
    alternativas: dict[str, float]
    p_verdadeira: float
    custo_esperado: float
    origem: str


@dataclass
class Ambiente:
    """Uma "cadeira" no ambiente: papel + ator (quem assina) sobre um `Estado` compartilhado."""
    papel: str = "atendente"
    ator: str = "agente_ia"
    estado: Estado = field(default_factory=Estado)
    conversa_atual: str | None = None  # conversa em atendimento: vínculo usado nas ações sem conversation_id

    def __post_init__(self) -> None:
        if self.papel not in {"atendente", "analista", "supervisor"}:
            raise ValueError(f"papel inválido: {self.papel}")

    # ---- relógio e log ficam no estado (compartilhados entre papéis) ---------
    @property
    def tique(self) -> int:
        return self.estado.tique

    @tique.setter
    def tique(self, valor: int) -> None:
        dados.agora(valor)  # valida o intervalo
        self.estado.tique = valor

    @property
    def log(self) -> list[dict[str, Any]]:
        return self.estado.log

    @property
    def agora(self) -> datetime:
        return dados.agora(self.tique)

    def avancar(self, horas: int = 1) -> None:
        self.tique = min(self.tique + horas, dados.ULTIMO_TIQUE)

    def como(self, papel: str, ator: str) -> "Ambiente":
        """Outra cadeira no MESMO estado (ex.: o supervisor que aprova o pedido do atendente)."""
        return Ambiente(papel=papel, ator=ator, estado=self.estado)

    @contextmanager
    def atendendo(self, conversation_id: str) -> Iterator["Ambiente"]:
        """Durante o bloco, ações sem conversation_id ficam vinculadas a esta conversa (log e checagens)."""
        anterior, self.conversa_atual = self.conversa_atual, conversation_id
        try:
            yield self
        finally:
            self.conversa_atual = anterior

    # ---- despacho genérico: valida contra o catálogo e registra -------------
    def chamar(self, nome: str, trilha: Trilha | dict[str, Any] | None = None, **params: Any) -> dict[str, Any]:
        resultado = self._executar(nome, params, trilha)
        self.log.append({
            "tique": self.tique,
            "data_hora": self.agora.strftime("%Y-%m-%d %H:%M:%S"),
            "papel": self.papel,
            "ator": self.ator,
            "conversa_atual": self.conversa_atual,
            "ferramenta": nome,
            "parametros": {k: v for k, v in params.items() if not _vazio(v)},
            "ok": resultado.get("ok", False),
            "erro": resultado.get("erro"),
            "trilha": dict(trilha) if trilha else None,
        })
        return resultado

    def _executar(self, nome: str, params: dict[str, Any], trilha: dict[str, Any] | None) -> dict[str, Any]:
        spec = CATALOGO.get(nome)
        if spec is None:
            return {"ok": False, "erro": f"ferramenta inexistente: {nome}"}
        if self.papel not in spec.papeis:
            return {"ok": False, "erro": f"papel '{self.papel}' não pode usar {nome} (permitidos: {sorted(spec.papeis)})"}
        desconhecidos = set(params) - set(spec.parametros)
        if desconhecidos:
            return {"ok": False, "erro": f"parâmetros desconhecidos para {nome}: {sorted(desconhecidos)}"}
        faltando = [p for p in spec.obrigatorios if _vazio(params.get(p))]
        if faltando:
            return {"ok": False, "erro": f"parâmetros obrigatórios ausentes em {nome}: {faltando}"}
        if nome not in FERRAMENTAS_LEITURA and (not trilha or _vazio(trilha.get("por_que"))):
            return {"ok": False, "erro": f"{nome} altera estado: informe trilha={{'por_que': ...}} (auditoria)"}
        impl = _implementacoes().get(nome)
        if impl is None:
            return {"ok": False, "erro": f"ferramenta {nome} ainda não simulada"}
        if nome in FERRAMENTAS_LEITURA:
            return impl(self, **params)
        return impl(self, trilha=dict(trilha), **params)

    def __getattr__(self, nome: str) -> Callable[..., dict[str, Any]]:
        """`amb.reenviar_produto(order_id=..., valor=..., motivo=..., trilha={...})` == `amb.chamar("reenviar_produto", ...)`."""
        if nome in CATALOGO:
            return lambda trilha=None, **params: self.chamar(nome, trilha=trilha, **params)
        raise AttributeError(nome)

    # ---- atalhos explícitos (leitura) ---------------------------------------
    def ler_conversa(self, conversation_id: str) -> dict[str, Any]:
        return self.chamar("ler_conversa", conversation_id=conversation_id)

    def consultar_cliente(self, customer_id: str) -> dict[str, Any]:
        return self.chamar("consultar_cliente", customer_id=customer_id)

    def consultar_pedido(self, order_id: str) -> dict[str, Any]:
        return self.chamar("consultar_pedido", order_id=order_id)

    def ver_anexos(self, conversation_id: str | None = None, order_id: str | None = None,
                   hash_perceptual: str | None = None) -> dict[str, Any]:
        return self.chamar("ver_anexos", conversation_id=conversation_id, order_id=order_id,
                           hash_perceptual=hash_perceptual)

    def verificar_pagamento(self, order_id: str) -> dict[str, Any]:
        return self.chamar("verificar_pagamento", order_id=order_id)


def _vazio(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip()) or (isinstance(v, float) and pd.isna(v))


def _ate_agora(df: pd.DataFrame, coluna: str, amb: Ambiente) -> pd.DataFrame:
    """Linhas com `coluna` <= agora (linhas sem data são mantidas)."""
    col = df[coluna]
    return df[col.isna() | (col <= pd.Timestamp(amb.agora))]


# ---------------------------------------------------------------------------
# Implementações (leitura)
# ---------------------------------------------------------------------------


def _ler_conversa(amb: Ambiente, conversation_id: str) -> dict[str, Any]:
    fila = dados.tabela("fila_aberta")
    hist = dados.tabela("historico_conversas")
    meta = fila[fila["conversation_id"] == conversation_id]
    origem = "fila_aberta"
    if meta.empty:
        meta, origem = hist[hist["conversation_id"] == conversation_id], "historico"
    if meta.empty:
        return {"ok": False, "erro": f"conversa não encontrada: {conversation_id}"}

    msgs = dados.tabela("historico_mensagens")
    msgs = msgs[msgs["conversation_id"] == conversation_id]
    msgs = _ate_agora(msgs, "timestamp", amb).sort_values("timestamp")
    mensagens = []
    for m in dados.registros(msgs[["message_id", "autor", "timestamp", "texto"]]):
        m["texto"] = mascarar_pii(m["texto"])
        # Texto do cliente é DADO, nunca instrução: marcado explicitamente para camadas de IA.
        m["conteudo_nao_confiavel"] = m["autor"] == "cliente"
        m["tem_imagem"] = bool(m["texto"]) and ("[imagem]" in m["texto"] or "[foto anexada]" in m["texto"])
        mensagens.append(m)
    # Mensagens que o agente já enviou nesta execução.
    for m in amb.estado.mensagens:
        if m["conversation_id"] == conversation_id:
            mensagens.append({"message_id": m["message_id"], "autor": "agente", "timestamp": m["timestamp"],
                              "texto": m["texto"], "conteudo_nao_confiavel": False, "tem_imagem": False})

    conversa = dados.registro(meta.iloc[0])
    return {"ok": True, "origem": origem, "conversa": conversa, "mensagens": mensagens,
            "estado_agente": dict(amb.estado.conversas.get(conversation_id, {})),
            "suspeita": amb.estado.suspeitas.get(conversation_id)}


_CAMPOS_CLIENTE = [
    "customer_id", "nome", "segmento", "n_pedidos", "receita_acumulada", "n_trocas_12m", "n_contatos_12m",
    "data_cadastro", "cidade", "uf", "canal_preferido", "aceita_comunicacao_whatsapp", "aceita_comunicacao_email",
    "ultimo_pedido_em",
]  # sem CPF, telefone, e-mail, nascimento e CEP


def _consultar_cliente(amb: Ambiente, customer_id: str) -> dict[str, Any]:
    cli = dados.tabela("clientes")
    linha = cli[cli["customer_id"] == customer_id]
    if linha.empty:
        return {"ok": False, "erro": f"cliente não encontrado: {customer_id}"}
    cliente = dados.registro(linha.iloc[0][[c for c in _CAMPOS_CLIENTE if c in linha.columns]])

    trocas = dados.tabela("trocas_reenvios")
    trocas = _ate_agora(trocas[trocas["customer_id"] == customer_id], "data_solicitacao", amb)
    campos_troca = ["solicitacao_id", "conversation_id", "order_id_original", "tipo", "motivo_registrado",
                    "data_solicitacao", "solicitado_por", "aprovado_por", "status", "custo_total"]

    peds = dados.tabela("pedidos")
    peds = _ate_agora(peds[peds["customer_id"] == customer_id], "data_hora_pedido", amb)
    return {
        "ok": True,
        "cliente": cliente,
        "trocas_reenvios": dados.registros(trocas[campos_troca].sort_values("data_solicitacao")),
        "pedidos_conhecidos": peds["order_id"].tolist(),  # só os que estão em pedidos.csv
    }


_CAMPOS_PEDIDO_OMITIDOS = ["cep_entrega"]


def meta_conversa(conversation_id: str) -> dict[str, Any] | None:
    """Metadados da conversa (fila aberta ou histórico), sem log. Uso interno das ferramentas."""
    for nome in ("fila_aberta", "historico_conversas"):
        df = dados.tabela(nome)
        linha = df[df["conversation_id"] == conversation_id]
        if not linha.empty:
            return dados.registro(linha.iloc[0])
    return None


STATUS_JA_SEPARADO = {"em_transito", "entregue", "extraviado", "devolvido"}


def pedido_na_posicao(amb: Ambiente, order_id: str) -> dict[str, Any] | None:
    """Pedido como está no tique atual, já com o que o agente alterou (cancelamento, endereço). Sem log."""
    peds = dados.tabela("pedidos")
    linha = _ate_agora(peds[peds["order_id"] == order_id], "data_hora_pedido", amb)  # pedido do futuro não existe
    if linha.empty:
        return None
    pedido = dados.registro(linha.iloc[0].drop(_CAMPOS_PEDIDO_OMITIDOS, errors="ignore"))
    # Base é a posição de 02/11 08:00: campos de evento posteriores ao "agora" não podem vazar.
    for campo in ("data_separacao", "data_entrega"):
        valor = linha.iloc[0][campo]
        if pd.notna(valor) and valor > pd.Timestamp(amb.agora):
            pedido[campo] = None
    pedido.update(amb.estado.pedidos.get(order_id, {}))
    pedido["ja_separado"] = bool(pedido["data_separacao"]) or pedido["status_entrega"] in STATUS_JA_SEPARADO
    return pedido


def _consultar_pedido(amb: Ambiente, order_id: str) -> dict[str, Any]:
    pedido = pedido_na_posicao(amb, order_id)
    if pedido is None:
        return {"ok": False, "erro": f"pedido não encontrado: {order_id}"}

    itens = dados.tabela("itens_pedido")
    itens = itens[itens["order_id"] == order_id].sort_values("linha")

    rastreio = dados.tabela("rastreio_portais_transportadoras")
    rastreio = _ate_agora(rastreio[rastreio["pedido_ref"] == order_id], "data_hora_evento", amb)
    rastreio = rastreio.sort_values("data_hora_evento")

    chamados = dados.tabela("chamados_transportadora")
    chamados = _ate_agora(chamados[chamados["order_id"] == order_id], "data_abertura", amb)

    trocas = dados.tabela("trocas_reenvios")
    trocas = _ate_agora(trocas[trocas["order_id_original"] == order_id], "data_solicitacao", amb)

    return {
        "ok": True,
        "pedido": pedido,
        "itens": dados.registros(itens.drop(columns=["item_id"])),
        "rastreio": dados.registros(rastreio.drop(columns=["pedido_ref"])),
        "ultimo_evento_rastreio": dados.registro(rastreio.iloc[-1]) if not rastreio.empty else None,
        "chamados_transportadora": dados.registros(chamados),
        "trocas_reenvios": dados.registros(trocas),
    }


def _ver_anexos(amb: Ambiente, conversation_id: str | None = None, order_id: str | None = None,
                hash_perceptual: str | None = None) -> dict[str, Any]:
    filtros = {"conversation_id": conversation_id, "order_id": order_id, "hash_perceptual": hash_perceptual}
    filtros = {k: v for k, v in filtros.items() if not _vazio(v)}
    if not filtros:
        return {"ok": False, "erro": "informe conversation_id, order_id ou hash_perceptual"}

    anexos = dados.tabela("anexos_conversas")
    for coluna, valor in filtros.items():
        anexos = anexos[anexos[coluna] == valor]
    anexos = _ate_agora(anexos, "enviado_em", amb).sort_values("enviado_em")
    return {"ok": True, "filtros": filtros, "n_anexos": len(anexos), "anexos": dados.registros(anexos)}


_COBRANCA_EFETIVA = {"capturada", "paga"}


def _verificar_pagamento(amb: Ambiente, order_id: str) -> dict[str, Any]:
    peds = dados.tabela("pedidos")
    pedido = _ate_agora(peds[peds["order_id"] == order_id], "data_hora_pedido", amb)
    if pedido.empty:
        return {"ok": False, "erro": f"pedido não encontrado: {order_id}"}
    pedido = pedido.iloc[0]

    trx = dados.tabela("pagamentos_gateway")
    trx = _ate_agora(trx[trx["order_id"] == order_id], "data_hora_criacao", amb).sort_values("data_hora_criacao")
    est = dados.tabela("estornos")
    est = _ate_agora(est[est["order_id"] == order_id], "data_hora_solicitacao", amb)

    efetivas = trx[trx["status"].isin(_COBRANCA_EFETIVA)]
    total_cobrado = round(float(efetivas["valor"].sum()), 2)
    total_estornado = round(float(trx["valor_estornado"].fillna(0).sum()), 2)
    valor_pedido = float(pedido["valor"])
    return {
        "ok": True,
        "order_id": order_id,
        "valor_pedido": valor_pedido,
        "forma_pagamento": pedido["forma_pagamento"],
        "status_pagamento_erp": pedido["status_pagamento"],
        "transacoes": dados.registros(trx.drop(columns=["cartao_final", "customer_id"])),  # sem final do cartão
        "estornos": dados.registros(est),
        "resumo": {
            "n_transacoes": len(trx),
            "n_cobrancas_efetivas": len(efetivas),
            "total_cobrado": total_cobrado,
            "total_estornado": total_estornado,
            "cobrado_a_mais": round(max(total_cobrado - total_estornado - valor_pedido, 0.0), 2),
            "indicio_cobranca_duplicada": len(efetivas) > 1 and total_cobrado - total_estornado > valor_pedido + 0.01,
        },
    }


def _consultar_politicas(amb: Ambiente, categoria: str | None = None) -> dict[str, Any]:
    pol = dados.politicas()
    if _vazio(categoria):
        return {"ok": True, "politicas": pol}
    regras = {k.split(".", 1)[1]: v for k, v in pol["categorias"].items() if k.split(".", 1)[0] == categoria}
    if not regras:
        cats = sorted({k.split(".", 1)[0] for k in pol["categorias"]})
        return {"ok": False, "erro": f"categoria desconhecida: {categoria} (existentes: {cats})"}
    return {"ok": True, "categoria": categoria, "regras": regras}


def _validar_cupom(amb: Ambiente, codigo: str, order_id: str | None = None) -> dict[str, Any]:
    """Aplica as regras de cupons.csv (status, vigência, marca, mínimo, 1ª compra, limites de uso)."""
    cupons = dados.tabela("cupons")
    linha = cupons[cupons["codigo"].str.upper() == codigo.strip().upper()]
    if linha.empty:
        return {"ok": True, "codigo": codigo, "valido": False, "motivos": ["cupom inexistente"]}
    cupom = dados.registro(linha.iloc[0])
    motivos: list[str] = []
    agora = pd.Timestamp(amb.agora)
    if cupom["status"] != "ativo":
        motivos.append(f"cupom com status {cupom['status']}")
    ini, fim = linha.iloc[0]["inicio_vigencia"], linha.iloc[0]["fim_vigencia"]
    if (pd.notna(ini) and agora < ini) or (pd.notna(fim) and agora > fim + pd.Timedelta(days=1)):
        motivos.append("fora_da_vigencia")
    usos = _ate_agora(dados.tabela("uso_cupons"), "data_hora", amb)
    usos = usos[(usos["codigo"] == cupom["codigo"]) & (usos["status"] == "aplicado")]
    if cupom["limite_uso_total"] and len(usos) >= cupom["limite_uso_total"]:
        motivos.append("limite de uso total atingido")
    if not _vazio(order_id):
        pedido = pedido_na_posicao(amb, order_id)
        if pedido is None:
            return {"ok": False, "erro": f"pedido não encontrado: {order_id}"}
        marcas = str(cupom["marcas_validas"] or "todas")
        unidade = "Gocase" if order_id.startswith("GCH") else "GoBeauté"
        if marcas != "todas" and not ({pedido["marca"], unidade} & {m.strip() for m in marcas.split(";")}):
            motivos.append(f"cupom não vale para a marca {pedido['marca']}")
        if cupom["valor_minimo_pedido"] and (pedido.get("valor_produtos") or 0) < cupom["valor_minimo_pedido"]:
            motivos.append("valor_minimo_nao_atingido")
        if cupom["primeira_compra"] == "sim":
            cli = dados.tabela("clientes")
            n = cli.loc[cli["customer_id"] == pedido["customer_id"], "n_pedidos"]
            if not n.empty and int(n.iloc[0]) > 1:
                motivos.append("exclusivo_primeira_compra")
        if cupom["limite_uso_por_cliente"] and (usos["customer_id"] == pedido["customer_id"]).sum() >= cupom["limite_uso_por_cliente"]:
            motivos.append("limite de uso por cliente atingido")
    return {"ok": True, "codigo": cupom["codigo"], "valido": not motivos, "motivos": motivos,
            "cupom": {k: cupom[k] for k in ("descricao", "tipo_desconto", "percentual_desconto", "marcas_validas",
                                             "valor_minimo_pedido", "primeira_compra", "fim_vigencia")}}


_IMPLEMENTACOES: dict[str, Callable[..., dict[str, Any]]] = {
    "ler_conversa": _ler_conversa,
    "consultar_cliente": _consultar_cliente,
    "consultar_pedido": _consultar_pedido,
    "ver_anexos": _ver_anexos,
    "verificar_pagamento": _verificar_pagamento,
    "consultar_politicas": _consultar_politicas,
    "validar_cupom": _validar_cupom,
}

_CARREGADAS = False


def _implementacoes() -> dict[str, Callable[..., dict[str, Any]]]:
    """Registra as ferramentas de ação na 1ª chamada (não no import): acoes.py e acoes_analista.py importam este
    módulo, então registrar no import criava ciclo quando eles eram importados primeiro."""
    global _CARREGADAS
    if not _CARREGADAS:
        from .acoes import IMPLEMENTACOES as acoes
        from .acoes_analista import IMPLEMENTACOES as analista
        _IMPLEMENTACOES.update(acoes)
        _IMPLEMENTACOES.update(analista)
        _CARREGADAS = True
    return _IMPLEMENTACOES

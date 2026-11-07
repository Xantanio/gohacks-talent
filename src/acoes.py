"""Ferramentas de AÇÃO de `ferramentas.csv` (as que alteram o estado dos "sistemas").

Divisão de responsabilidades (decisão de projeto, ver docs/DECISOES.md):
- AQUI ficam só as travas que o próprio SISTEMA aplicaria, todas tiradas de `ferramentas.csv`:
  alçada (`valor_max_sem_aprovacao_rs`), `somente_antes_da_separacao`, 1 troca por pedido,
  supervisor não aprova/decide o que ele mesmo pediu/sinalizou, bloqueio enquanto há suspeita aberta,
  pedido tem de ser do cliente da conversa, conversa encerrada não recebe mensagem.
- A POLÍTICA de negócio (prazo de 7 dias, exige foto, atraso de 7 dias úteis, etc.) é decidida antes,
  em `politicas.py`/`decisao.py`. Uma ferramenta aceitar a chamada NÃO quer dizer que a política permite.

Toda função recebe `trilha` (já validada em `Ambiente._executar`) e grava em `Estado.acoes` com ela.
Textos ao cliente passam por `mascarar_pii` (nunca repetir CPF, cartão, telefone).
"""
from __future__ import annotations

import unicodedata
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Callable

import pandas as pd

from . import dados
from .ferramentas import (CANAIS_PUBLICOS, CATALOGO, _ate_agora, _vazio, mascarar_pii, mascarar_publico,
                          meta_conversa, pedido_na_posicao)

if TYPE_CHECKING:
    from .ferramentas import Ambiente

Resultado = dict[str, Any]

AREAS_ACIONAVEIS = ("supervisão", "logística", "qualidade", "produção", "jurídico", "comercial")
URGENCIAS = ("baixa", "media", "alta", "critica")
MOTIVOS_REENVIO = ("avaria", "item_faltante", "atraso_ou_extravio", "defeito_produto", "outros")
MOTIVOS_CHAMADO = ("rastreio_sem_atualizacao", "atraso_na_entrega", "extravio")
ACOES_COM_APROVACAO = tuple(n for n, s in CATALOGO.items() if "aprovacao_id" in s.parametros)

STATUS_LEGIVEL = {
    "aguardando_separacao": "sendo preparado para envio",
    "em_transito": "a caminho",
    "entregue": "entregue",
    "extraviado": "com extravio registrado pela transportadora",
    "devolvido": "devolvido ao nosso centro de distribuição",
    "cancelado": "cancelado",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _erro(msg: str) -> Resultado:
    return {"ok": False, "erro": msg}


def _sem_acento(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto.lower().strip()) if unicodedata.category(c) != "Mn")


def _cid(amb: Ambiente, conversation_id: str | None = None) -> str | None:
    return conversation_id if not _vazio(conversation_id) else amb.conversa_atual


def _conversa_aberta(amb: Ambiente, conversation_id: str) -> tuple[dict[str, Any] | None, str | None]:
    meta = meta_conversa(conversation_id)
    if meta is None:
        return None, f"conversa não encontrada: {conversation_id}"
    if amb.estado.conversa(conversation_id)["encerrada"]:
        return None, f"conversa {conversation_id} já foi encerrada"
    return meta, None


def _pedido_da_conversa(amb: Ambiente, order_id: str, conversation_id: str | None = None
                        ) -> tuple[dict[str, Any] | None, str | None]:
    """Pedido na posição atual + checagem de vínculo: o pedido tem de ser do cliente da conversa.

    Protege contra o cliente (ou um texto injetado) pedir ação sobre o pedido de outra pessoa.
    """
    pedido = pedido_na_posicao(amb, order_id)
    if pedido is None:
        return None, f"pedido não encontrado: {order_id}"
    cid = _cid(amb, conversation_id)
    if cid:
        meta = meta_conversa(cid)
        if meta and meta["customer_id"] != pedido["customer_id"]:
            return None, f"pedido {order_id} não pertence ao cliente da conversa {cid}"
    return pedido, None


def _bloqueio_suspeita(amb: Ambiente, order_id: str) -> str | None:
    for cid, s in amb.estado.suspeitas.items():
        if s["order_id"] == order_id and s["status"] in ("pendente", "improcedente"):
            return (f"pedido {order_id} com suspeita {s['status']} (conversa {cid}): reenvio, troca e reembolso "
                    "suspensos até decisão procedente do supervisor")
    return None


def _valor(valor: Any) -> tuple[float | None, str | None]:
    try:
        v = round(float(valor), 2)
    except (TypeError, ValueError):
        return None, f"valor inválido: {valor!r}"
    return (v, None) if v > 0 else (None, "valor deve ser maior que zero")


def _alcada(amb: Ambiente, acao: str, order_id: str, valor: float, aprovacao_id: str | None
            ) -> tuple[dict[str, Any] | None, str | None]:
    """Alçada lida de ferramentas.csv. Acima do limite só com aprovação do supervisor para ESTA ação/pedido/valor."""
    limite = CATALOGO[acao].limites.get("valor_max_sem_aprovacao_rs")
    if _vazio(aprovacao_id):
        if limite is not None and valor > limite:
            return None, (f"R$ {valor:.2f} acima da alçada de {acao} (R$ {limite:.2f}): "
                          "use solicitar_aprovacao e repita com aprovacao_id")
        return None, None
    aprov = amb.estado.aprovacoes.get(aprovacao_id)
    if aprov is None:
        return None, f"aprovação inexistente: {aprovacao_id}"
    if aprov["status"] != "aprovada":
        return None, f"aprovação {aprovacao_id} está {aprov['status']}"
    if aprov["usada_em"]:
        return None, f"aprovação {aprovacao_id} já foi usada em {aprov['usada_em']}"
    if aprov["acao"] != acao or aprov["order_id"] != order_id:
        return None, f"aprovação {aprovacao_id} é para {aprov['acao']} no pedido {aprov['order_id']}"
    if valor > aprov["valor"] + 0.01:
        return None, f"valor R$ {valor:.2f} maior que o aprovado (R$ {aprov['valor']:.2f})"
    return aprov, None


def _executar_com_dinheiro(amb: Ambiente, acao: str, order_id: str, valor: Any, aprovacao_id: str | None
                           ) -> tuple[dict[str, Any] | None, float | None, dict[str, Any] | None, str | None]:
    """Checagens comuns de reenviar/reimprimir/trocar/reembolsar. Retorna (pedido, valor, aprovacao, erro)."""
    v, erro = _valor(valor)
    if erro:
        return None, None, None, erro
    pedido, erro = _pedido_da_conversa(amb, order_id)
    if erro:
        return None, None, None, erro
    if pedido["status_entrega"] == "cancelado":
        return None, None, None, f"pedido {order_id} está cancelado"
    if v > float(pedido["valor"]) + 0.01:
        return None, None, None, f"valor R$ {v:.2f} maior que o valor do pedido (R$ {float(pedido['valor']):.2f})"
    erro = _bloqueio_suspeita(amb, order_id)
    if erro:
        return None, None, None, erro
    aprov, erro = _alcada(amb, acao, order_id, v, aprovacao_id)
    if erro:
        return None, None, None, erro
    return pedido, v, aprov, None


def _consumir(aprov: dict[str, Any] | None, acao_id: str) -> str | None:
    if aprov is None:
        return None
    aprov["usada_em"] = acao_id
    return aprov["aprovado_por"]


def _postar_no_canal(amb: Ambiente, tipo: str, titulo: str, linhas: dict[str, Any], urgencia: str = "media",
                     area: str = "supervisão") -> dict[str, Any]:
    """Mensagem pronta para Slack (mrkdwn). Por ora vai para `Estado.canal_do_time` / canal_do_time.md."""
    emoji = {"baixa": ":large_blue_circle:", "media": ":large_yellow_circle:", "alta": ":large_orange_circle:",
             "critica": ":red_circle:"}[urgencia]
    corpo = "\n".join(f"*{k}:* {mascarar_pii(str(v))}" for k, v in linhas.items() if not _vazio(v))
    texto = (f"{emoji} *[{tipo}]* para *{area}* · urgência *{urgencia}*\n*{mascarar_pii(titulo)}*\n{corpo}\n"
             f"_por {amb.ator} ({amb.papel}) em {amb.estado.agora_txt()} · tique {amb.tique}_")
    item = {"id": amb.estado.novo_id("SLK"), "tipo": tipo, "area": area, "urgencia": urgencia,
            "data_hora": amb.estado.agora_txt(), "autor": amb.ator, "texto_slack": texto}
    amb.estado.canal_do_time.append(item)
    return item


def eh_publica(conversation_id: str) -> bool:
    """Resposta visível para qualquer pessoa (Reclame Aqui): sem pedido completo, rastreio ou dado pessoal (D-015)."""
    meta = meta_conversa(conversation_id)
    return bool(meta) and meta["canal"] in CANAIS_PUBLICOS


def _mensagem(amb: Ambiente, conversation_id: str, texto: str, ferramenta: str) -> tuple[str, str]:
    """Envia ao cliente com o filtro do canal. Retorna (message_id, texto efetivamente enviado)."""
    publica = eh_publica(conversation_id)
    final = mascarar_publico(texto) if publica else mascarar_pii(texto)
    return amb.estado.enviar_mensagem(conversation_id, final, ferramenta, amb.ator, publica=publica), final


def _registrar(amb: Ambiente, acao: str, trilha: dict[str, Any], *, conversation_id: str | None = None,
               order_id: str | None = None, valor_rs: float | None = None, aprovado_por: str | None = None,
               referencia: str | None = None) -> str:
    return amb.estado.registrar_acao(
        acao=acao, conversation_id=_cid(amb, conversation_id), order_id=order_id, executado_por=amb.ator,
        valor_rs=valor_rs, aprovado_por=aprovado_por, referencia_sistema=referencia, trilha=trilha)


# ---------------------------------------------------------------------------
# Conversa / comunicação com o cliente
# ---------------------------------------------------------------------------

def _classificar_conversa(amb: Ambiente, trilha: dict, conversation_id: str, categoria: str,
                          categoria_secundaria: str | None = None) -> Resultado:
    if meta_conversa(conversation_id) is None:
        return _erro(f"conversa não encontrada: {conversation_id}")
    tags = [t.strip() for t in dados.politicas()["tags_agente"]["tags_agente"].split(";")]
    for c in (categoria, categoria_secundaria):
        if not _vazio(c) and c not in tags:
            return _erro(f"categoria fora das tags oficiais: {c} (válidas: {tags})")
    est = amb.estado.conversa(conversation_id)
    est["categoria"], est["categoria_secundaria"] = categoria, categoria_secundaria
    acao_id = _registrar(amb, "classificar_conversa", trilha, conversation_id=conversation_id)
    return {"ok": True, "acao_id": acao_id, "categoria": categoria, "categoria_secundaria": categoria_secundaria}


def _responder_cliente(amb: Ambiente, trilha: dict, conversation_id: str, texto: str) -> Resultado:
    _, erro = _conversa_aberta(amb, conversation_id)
    if erro:
        return _erro(erro)
    msg_id, limpo = _mensagem(amb, conversation_id, texto, "responder_cliente")
    acao_id = _registrar(amb, "responder_cliente", trilha, conversation_id=conversation_id, referencia=msg_id)
    return {"ok": True, "acao_id": acao_id, "message_id": msg_id, "pii_mascarado": limpo != texto,
            "publica": eh_publica(conversation_id), "texto": limpo}


def _solicitar_foto(amb: Ambiente, trilha: dict, conversation_id: str, do_que: str | None = None) -> Resultado:
    _, erro = _conversa_aberta(amb, conversation_id)
    if erro:
        return _erro(erro)
    alvo = do_que if not _vazio(do_que) else "do produto e da embalagem"
    if eh_publica(conversation_id):  # foto pode mostrar etiqueta/endereço: nunca pedir em público
        texto = (f"Para seguirmos com a sua solicitação, precisamos de uma foto {alvo}. Vamos pedir pelo canal "
                 "privado, para não expor seus dados aqui, e damos andamento assim que recebermos.")
    else:
        texto = (f"Para seguirmos com a sua solicitação, você pode nos enviar uma foto {alvo}? "
                 "Assim que recebermos, damos andamento rapidinho.")
    msg_id, texto = _mensagem(amb, conversation_id, texto, "solicitar_foto")
    amb.estado.conversa(conversation_id)["aguardando_foto"] = True
    acao_id = _registrar(amb, "solicitar_foto", trilha, conversation_id=conversation_id, referencia=msg_id)
    return {"ok": True, "acao_id": acao_id, "message_id": msg_id, "texto": texto}


def _informar_status(amb: Ambiente, trilha: dict, conversation_id: str, order_id: str) -> Resultado:
    _, erro = _conversa_aberta(amb, conversation_id)
    if erro:
        return _erro(erro)
    pedido, erro = _pedido_da_conversa(amb, order_id, conversation_id)
    if erro:
        return _erro(erro)
    rastreio = dados.tabela("rastreio_portais_transportadoras")
    rastreio = _ate_agora(rastreio[rastreio["pedido_ref"] == order_id], "data_hora_evento", amb)
    ultimo = dados.registro(rastreio.sort_values("data_hora_evento").iloc[-1]) if not rastreio.empty else None

    partes = [f"Seu pedido {order_id} está {STATUS_LEGIVEL.get(pedido['status_entrega'], pedido['status_entrega'])}"]
    if pedido.get("transportadora") and pedido["status_entrega"] != "aguardando_separacao":
        partes[0] += f" com a {pedido['transportadora']}"
    partes[0] += "."
    if ultimo:
        partes.append(f"Última atualização: {ultimo['status']} em {ultimo['local']} ({ultimo['data_hora_evento']}).")
    previsao = (ultimo or {}).get("previsao_entrega") or pedido.get("data_prometida")
    if previsao and pedido["status_entrega"] not in ("entregue", "cancelado"):
        partes.append(f"Previsão de entrega: {previsao}.")
    publica = eh_publica(conversation_id)
    if pedido.get("codigo_rastreio") and pedido["status_entrega"] != "aguardando_separacao" and not publica:
        partes.append(f"Código de rastreio: {pedido['codigo_rastreio']}.")
    texto = " ".join(partes)
    msg_id, texto = _mensagem(amb, conversation_id, texto, "informar_status")
    acao_id = _registrar(amb, "informar_status", trilha, conversation_id=conversation_id, order_id=order_id,
                         referencia=msg_id)
    return {"ok": True, "acao_id": acao_id, "message_id": msg_id, "texto": texto}


def _responder_duvida(amb: Ambiente, trilha: dict, conversation_id: str, sku: str | None = None) -> Resultado:
    """Devolve a ficha do produto (PIM). O texto ao cliente é redigido depois (LLM) e enviado com responder_cliente."""
    meta, erro = _conversa_aberta(amb, conversation_id)
    if erro:
        return _erro(erro)
    if _vazio(sku) and meta.get("order_id"):
        pedido = pedido_na_posicao(amb, meta["order_id"])
        sku = pedido["sku"] if pedido else None
    if _vazio(sku):
        return _erro("informe o sku (a conversa não tem pedido associado)")
    prods = dados.tabela("produtos")
    linha = prods[prods["sku"] == sku]
    if linha.empty:
        return _erro(f"sku não encontrado: {sku}")
    campos = ["sku", "nome", "marca", "categoria", "conteudo", "composicao", "modo_de_uso", "notas_olfativas",
              "fixacao", "advertencias", "validade_meses_fechado", "validade_apos_aberto", "vegano", "cruelty_free",
              "sem_gluten_sem_lactose", "liberado_low_poo", "modelo_compativel", "compativel_magsafe",
              "prazo_producao_dias_uteis"]
    ficha = {k: v for k, v in dados.registro(linha.iloc[0][campos]).items() if v is not None}
    acao_id = _registrar(amb, "responder_duvida", trilha, conversation_id=conversation_id)
    return {"ok": True, "acao_id": acao_id, "ficha": ficha}


def _orientar_suspensao_uso(amb: Ambiente, trilha: dict, conversation_id: str) -> Resultado:
    _, erro = _conversa_aberta(amb, conversation_id)
    if erro:
        return _erro(erro)
    texto = ("Sentimos muito pelo que você está passando. Por segurança, suspenda o uso do produto agora. "
             "Se houver irritação, coceira, inchaço ou qualquer desconforto que persista, procure atendimento "
             "médico e leve a embalagem do produto. Nosso time de qualidade vai acompanhar o seu caso de perto.")
    msg_id, texto = _mensagem(amb, conversation_id, texto, "orientar_suspensao_uso")
    amb.estado.conversa(conversation_id)["orientacao_suspensao_em"] = amb.estado.agora_txt()
    acao_id = _registrar(amb, "orientar_suspensao_uso", trilha, conversation_id=conversation_id, referencia=msg_id)
    return {"ok": True, "acao_id": acao_id, "message_id": msg_id, "texto": texto}


def _texto_da_regra(regra: str) -> str | None:
    """Aceita a categoria ('arrependimento_cancelamento') ou o caminho 'secao.regra' de politicas.csv."""
    pol = dados.politicas()
    cats = pol["categorias"]
    nome = regra.removeprefix("categorias.")
    if f"{nome}.regra" in cats:
        return cats[f"{nome}.regra"]
    if any(k.split(".", 1)[0] == nome for k in cats):
        return "; ".join(f"{k.split('.', 1)[1]}: {v}" for k, v in cats.items() if k.split(".", 1)[0] == nome)
    secao, _, chave = regra.partition(".")
    if secao in pol and chave in pol[secao]:
        return str(pol[secao][chave])
    return None


def _negar_por_politica(amb: Ambiente, trilha: dict, conversation_id: str, regra: str) -> Resultado:
    _, erro = _conversa_aberta(amb, conversation_id)
    if erro:
        return _erro(erro)
    texto_regra = _texto_da_regra(regra)
    if texto_regra is None:
        return _erro(f"regra inexistente em politicas.csv: {regra} (não negar com regra inventada)")
    s = amb.estado.suspeitas.get(conversation_id)
    if s and s["status"] == "pendente":
        return _erro("conversa com suspeita pendente: quem decide é o supervisor (decidir_suspeita)")
    texto = ("Entendemos o seu pedido e sentimos não poder atender desta vez. Pela nossa política: "
             f"{texto_regra} Se quiser, seguimos conversando para encontrar a melhor alternativa para você.")
    msg_id, texto = _mensagem(amb, conversation_id, texto, "negar_por_politica")
    acao_id = _registrar(amb, "negar_por_politica", trilha, conversation_id=conversation_id, referencia=msg_id)
    return {"ok": True, "acao_id": acao_id, "message_id": msg_id, "texto": texto}


def _agradecer(amb: Ambiente, trilha: dict, conversation_id: str) -> Resultado:
    _, erro = _conversa_aberta(amb, conversation_id)
    if erro:
        return _erro(erro)
    texto = "Que alegria receber a sua mensagem! Obrigada pelo carinho, ele vai direto para o nosso time. 💛"
    msg_id, texto = _mensagem(amb, conversation_id, texto, "agradecer")
    acao_id = _registrar(amb, "agradecer", trilha, conversation_id=conversation_id, referencia=msg_id)
    return {"ok": True, "acao_id": acao_id, "message_id": msg_id, "texto": texto}


def _encerrar_conversa(amb: Ambiente, trilha: dict, conversation_id: str, motivo: str | None = None) -> Resultado:
    _, erro = _conversa_aberta(amb, conversation_id)
    if erro:
        return _erro(erro)
    s = amb.estado.suspeitas.get(conversation_id)
    if s and s["status"] == "pendente":
        return _erro("conversa com suspeita pendente não pode ser encerrada antes da decisão do supervisor")
    est = amb.estado.conversa(conversation_id)
    est["encerrada"], est["motivo_encerramento"] = True, motivo
    acao_id = _registrar(amb, "encerrar_conversa", trilha, conversation_id=conversation_id)
    return {"ok": True, "acao_id": acao_id}


# ---------------------------------------------------------------------------
# Pedido / logística
# ---------------------------------------------------------------------------

def _abrir_chamado_transportadora(amb: Ambiente, trilha: dict, order_id: str, motivo: str) -> Resultado:
    if motivo not in MOTIVOS_CHAMADO:
        return _erro(f"motivo inválido: {motivo} (válidos: {list(MOTIVOS_CHAMADO)})")
    pedido, erro = _pedido_da_conversa(amb, order_id)
    if erro:
        return _erro(erro)
    if not pedido["ja_separado"]:
        return _erro(f"pedido {order_id} ainda não foi separado: não há o que cobrar da transportadora")
    if pedido["status_entrega"] in ("entregue", "cancelado", "devolvido"):
        return _erro(f"pedido {order_id} está {pedido['status_entrega']}: chamado não se aplica")
    hist = _ate_agora(dados.tabela("chamados_transportadora"), "data_abertura", amb)
    abertos = hist[(hist["order_id"] == order_id) & (hist["status"] != "encerrado")]
    if not abertos.empty:
        return _erro(f"já existe chamado aberto para {order_id}: {abertos.iloc[0]['chamado_id']}")
    if any(c["order_id"] == order_id and c["status"] == "aberto" for c in amb.estado.chamados):
        return _erro(f"já existe chamado aberto para {order_id} nesta execução")
    chamado_id = amb.estado.novo_id("CHM")
    acao_id = _registrar(amb, "abrir_chamado_transportadora", trilha, order_id=order_id, referencia=chamado_id)
    amb.estado.chamados.append({
        "chamado_id": chamado_id, "transportadora": pedido["transportadora"], "order_id": order_id,
        "codigo_rastreio": pedido.get("codigo_rastreio"), "conversation_id": amb.conversa_atual, "acao_id": acao_id,
        "aberto_por": amb.ator, "data_abertura": amb.estado.agora_txt(), "motivo": motivo,
        "prazo_resposta": (amb.agora + timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S"), "status": "aberto",
    })
    return {"ok": True, "acao_id": acao_id, "chamado_id": chamado_id, "prazo_resposta_horas": 48}


def _reenviar_produto(amb: Ambiente, trilha: dict, order_id: str, valor: Any, motivo: str,
                      aprovacao_id: str | None = None) -> Resultado:
    if motivo not in MOTIVOS_REENVIO:
        return _erro(f"motivo inválido: {motivo} (válidos: {list(MOTIVOS_REENVIO)})")
    pedido, v, aprov, erro = _executar_com_dinheiro(amb, "reenviar_produto", order_id, valor, aprovacao_id)
    if erro:
        return _erro(erro)
    return _gravar_oms(amb, trilha, "reenviar_produto", "reenvio", pedido, v, aprov, motivo=motivo)


def _reimprimir_personalizacao(amb: Ambiente, trilha: dict, order_id: str, valor: Any,
                               personalizacao_correta: str | None = None, aprovacao_id: str | None = None) -> Resultado:
    pedido, v, aprov, erro = _executar_com_dinheiro(amb, "reimprimir_personalizacao", order_id, valor, aprovacao_id)
    if erro:
        return _erro(erro)
    if not order_id.startswith("GCH") or _vazio(pedido.get("personalizacao")):
        return _erro(f"pedido {order_id} não é um item Gocase personalizado")
    return _gravar_oms(amb, trilha, "reimprimir_personalizacao", "nova_producao", pedido, v, aprov,
                       motivo="erro_personalizacao", texto_personalizacao=personalizacao_correta)


def _trocar_produto(amb: Ambiente, trilha: dict, order_id: str, valor: Any, sku_novo: str | None = None,
                    aprovacao_id: str | None = None) -> Resultado:
    pedido, v, aprov, erro = _executar_com_dinheiro(amb, "trocar_produto", order_id, valor, aprovacao_id)
    if erro:
        return _erro(erro)
    hist = _ate_agora(dados.tabela("trocas_reenvios"), "data_solicitacao", amb)
    n_trocas = int(((hist["order_id_original"] == order_id) & (hist["tipo"] == "troca")).sum())
    n_trocas += sum(1 for o in amb.estado.oms if o["order_id_original"] == order_id and o["tipo"] == "troca")
    if n_trocas >= 1:
        return _erro(f"pedido {order_id} já teve troca: limite de 1 troca por pedido")
    if not _vazio(sku_novo) and dados.tabela("produtos")["sku"].eq(sku_novo).sum() == 0:
        return _erro(f"sku_novo não encontrado: {sku_novo}")
    return _gravar_oms(amb, trilha, "trocar_produto", "troca", pedido, v, aprov, motivo="troca",
                       sku_enviado=sku_novo or pedido["sku"])


def _gravar_oms(amb: Ambiente, trilha: dict, acao: str, tipo: str, pedido: dict, valor: float,
                aprov: dict | None, *, motivo: str, sku_enviado: str | None = None,
                texto_personalizacao: str | None = None) -> Resultado:
    order_id = pedido["order_id"]
    sol_id = amb.estado.novo_id("OMS")
    n = sum(1 for o in amb.estado.oms if o["order_id_original"] == order_id) + 1
    acao_id = _registrar(amb, acao, trilha, order_id=order_id, valor_rs=valor,
                         aprovado_por=aprov["aprovado_por"] if aprov else None, referencia=sol_id)
    _consumir(aprov, acao_id)
    amb.estado.oms.append({
        "solicitacao_id": sol_id, "acao_id": acao_id, "conversation_id": amb.conversa_atual,
        "customer_id": pedido["customer_id"], "order_id_original": order_id,
        "pedido_reposicao_id": f"{order_id}-AG{n}", "tipo": tipo, "motivo_registrado": motivo,
        "sku_enviado": sku_enviado or pedido["sku"], "texto_personalizacao": texto_personalizacao,
        "data_solicitacao": amb.estado.agora_txt(), "solicitado_por": amb.ator,
        "aprovado_por": aprov["aprovado_por"] if aprov else None, "status": "aguardando_expedicao",
        "valor_rs": valor,
    })
    return {"ok": True, "acao_id": acao_id, "solicitacao_id": sol_id, "pedido_reposicao_id": f"{order_id}-AG{n}",
            "aprovacao_usada": aprov["aprovacao_id"] if aprov else None}


def _reembolsar(amb: Ambiente, trilha: dict, order_id: str, valor: Any, motivo: str,
                aprovacao_id: str | None = None) -> Resultado:
    pedido, v, aprov, erro = _executar_com_dinheiro(amb, "reembolsar", order_id, valor, aprovacao_id)
    if erro:
        return _erro(erro)
    est = _ate_agora(dados.tabela("estornos"), "data_hora_solicitacao", amb)
    ja = float(est.loc[(est["order_id"] == order_id) & (est["status"] != "negado"), "valor"].sum())
    ja += sum(e["valor"] for e in amb.estado.estornos if e["order_id"] == order_id)
    trx = _ate_agora(dados.tabela("pagamentos_gateway"), "data_hora_criacao", amb)
    cobrado = float(trx.loc[(trx["order_id"] == order_id) & trx["status"].isin(["capturada", "paga"]), "valor"].sum())
    teto = max(cobrado, float(pedido["valor"]))  # em duplicidade o cobrado passa do valor do pedido
    if ja + v > teto + 0.01:
        return _erro(f"estorno de R$ {v:.2f} passaria do total cobrado (R$ {teto:.2f}; já estornado R$ {ja:.2f})")
    return _gravar_estorno(amb, trilha, "reembolsar", pedido, v, motivo, aprov)


def _gravar_estorno(amb: Ambiente, trilha: dict, acao: str, pedido: dict, valor: float, motivo: str,
                    aprov: dict | None) -> Resultado:
    est_id = amb.estado.novo_id("EST")
    acao_id = _registrar(amb, acao, trilha, order_id=pedido["order_id"], valor_rs=valor,
                         aprovado_por=aprov["aprovado_por"] if aprov else None, referencia=est_id)
    _consumir(aprov, acao_id)
    amb.estado.estornos.append({
        "estorno_id": est_id, "order_id": pedido["order_id"], "conversation_id": amb.conversa_atual,
        "acao_id": acao_id, "tipo": "total" if abs(valor - float(pedido["valor"])) < 0.01 else "parcial",
        "valor": valor, "motivo": motivo, "solicitado_por": amb.ator,
        "aprovado_por": aprov["aprovado_por"] if aprov else None,
        "data_hora_solicitacao": amb.estado.agora_txt(), "status": "solicitado",
    })
    return {"ok": True, "acao_id": acao_id, "estorno_id": est_id, "valor": valor,
            "aprovacao_usada": aprov["aprovacao_id"] if aprov else None}


def _cancelar_pedido(amb: Ambiente, trilha: dict, order_id: str, motivo: str | None = None) -> Resultado:
    pedido, erro = _pedido_da_conversa(amb, order_id)
    if erro:
        return _erro(erro)
    if pedido["status_entrega"] == "cancelado":
        return _erro(f"pedido {order_id} já está cancelado")
    if pedido["ja_separado"]:  # limite do catálogo: somente_antes_da_separacao
        return _erro(f"pedido {order_id} já foi separado ({pedido['data_separacao'] or pedido['status_entrega']}): "
                     "cancelamento só antes da separação")
    # Cancelamento tem estorno integral automático, sem alçada (politicas: "sem limite de valor").
    r = _gravar_estorno(amb, trilha, "cancelar_pedido", pedido, float(pedido["valor"]),
                        motivo or "cancelamento_cliente", None)
    amb.estado.pedidos.setdefault(order_id, {}).update(
        {"status_entrega": "cancelado", "status_pagamento": "estornado"})
    return {**r, "estorno_integral": True}


def _alterar_endereco(amb: Ambiente, trilha: dict, order_id: str, novo_endereco: str) -> Resultado:
    pedido, erro = _pedido_da_conversa(amb, order_id)
    if erro:
        return _erro(erro)
    if pedido["status_entrega"] == "cancelado":
        return _erro(f"pedido {order_id} está cancelado")
    if pedido["ja_separado"]:
        return _erro(f"pedido {order_id} já foi separado: alteração de endereço só antes da separação")
    amb.estado.pedidos.setdefault(order_id, {})["endereco_entrega_alterado"] = novo_endereco
    acao_id = _registrar(amb, "alterar_endereco", trilha, order_id=order_id)
    return {"ok": True, "acao_id": acao_id}


# ---------------------------------------------------------------------------
# Levantar a mão: aprovação, suspeita, cosmetovigilância, canal do time
# ---------------------------------------------------------------------------

def _solicitar_aprovacao(amb: Ambiente, trilha: dict, acao: str, valor: Any, justificativa: str,
                         order_id: str | None = None) -> Resultado:
    if acao not in ACOES_COM_APROVACAO:
        return _erro(f"aprovação só se aplica a {list(ACOES_COM_APROVACAO)}")
    if _vazio(order_id):
        return _erro(f"{acao} precisa de order_id")
    v, erro = _valor(valor)
    if erro:
        return _erro(erro)
    limite = CATALOGO[acao].limites.get("valor_max_sem_aprovacao_rs")
    if limite is not None and v <= limite:
        return _erro(f"R$ {v:.2f} está dentro da alçada de {acao} (R$ {limite:.2f}): execute direto, "
                     "ou use sinalizar_suspeita se a dúvida for sobre a veracidade")
    pedido, erro = _pedido_da_conversa(amb, order_id)
    if erro:
        return _erro(erro)
    erro = _bloqueio_suspeita(amb, order_id)
    if erro:
        return _erro(erro)
    apr_id = amb.estado.novo_id("APR")
    acao_id = _registrar(amb, "solicitar_aprovacao", trilha, order_id=order_id, valor_rs=v, referencia=apr_id)
    amb.estado.aprovacoes[apr_id] = {
        "aprovacao_id": apr_id, "acao": acao, "order_id": order_id, "valor": v, "justificativa": justificativa,
        "conversation_id": amb.conversa_atual, "solicitado_por": amb.ator, "papel_solicitante": amb.papel,
        "solicitado_em": amb.estado.agora_txt(), "status": "pendente", "aprovado_por": None,
        "decidido_em": None, "comentario": None, "usada_em": None,
    }
    slack = _postar_no_canal(amb, "APROVAÇÃO", f"{acao} de R$ {v:.2f} no pedido {order_id} (acima da alçada)", {
        "Aprovação": apr_id, "Conversa": amb.conversa_atual, "Justificativa": justificativa,
        "Por que": trilha.get("por_que"), "O que esperamos": f"aprovar_acao ou rejeitar_acao em {apr_id}",
    }, urgencia="alta")
    return {"ok": True, "acao_id": acao_id, "aprovacao_id": apr_id, "status": "pendente", "slack_id": slack["id"]}


def _decidir_aprovacao(amb: Ambiente, trilha: dict, aprovacao_id: str, aprovar: bool, texto: str | None) -> Resultado:
    aprov = amb.estado.aprovacoes.get(aprovacao_id)
    if aprov is None:
        return _erro(f"aprovação inexistente: {aprovacao_id}")
    if aprov["status"] != "pendente":
        return _erro(f"aprovação {aprovacao_id} já está {aprov['status']}")
    if aprov["solicitado_por"] == amb.ator:
        return _erro("supervisor não aprova a própria ação")
    aprov.update(status="aprovada" if aprovar else "rejeitada", aprovado_por=amb.ator,
                 decidido_em=amb.estado.agora_txt(), comentario=texto)
    nome = "aprovar_acao" if aprovar else "rejeitar_acao"
    acao_id = _registrar(amb, nome, trilha, conversation_id=aprov["conversation_id"], order_id=aprov["order_id"],
                         valor_rs=aprov["valor"], referencia=aprovacao_id)
    return {"ok": True, "acao_id": acao_id, "aprovacao_id": aprovacao_id, "status": aprov["status"]}


def _aprovar_acao(amb: Ambiente, trilha: dict, aprovacao_id: str, comentario: str | None = None) -> Resultado:
    return _decidir_aprovacao(amb, trilha, aprovacao_id, True, comentario)


def _rejeitar_acao(amb: Ambiente, trilha: dict, aprovacao_id: str, justificativa: str) -> Resultado:
    return _decidir_aprovacao(amb, trilha, aprovacao_id, False, justificativa)


def _sinalizar_suspeita(amb: Ambiente, trilha: dict, conversation_id: str, evidencias: str,
                        sinais: str | None = None) -> Resultado:
    """Não envia nada ao cliente. Suspende reenvio/troca/reembolso do pedido até decidir_suspeita."""
    meta, erro = _conversa_aberta(amb, conversation_id)
    if erro:
        return _erro(erro)
    atual = amb.estado.suspeitas.get(conversation_id)
    if atual and atual["status"] == "pendente":
        return _erro(f"conversa {conversation_id} já está sinalizada")
    amb.estado.suspeitas[conversation_id] = {
        "order_id": meta.get("order_id"), "customer_id": meta["customer_id"], "sinais": sinais,
        "evidencias": evidencias, "status": "pendente", "sinalizado_por": amb.ator,
        "sinalizado_em": amb.estado.agora_txt(), "decidido_por": None, "decidido_em": None, "justificativa": None,
    }
    acao_id = _registrar(amb, "sinalizar_suspeita", trilha, conversation_id=conversation_id,
                         order_id=meta.get("order_id"))
    prazo = dados.politicas()["reclamacao_suspeita"]["prazo_decisao_horas"]
    slack = _postar_no_canal(amb, "SUSPEITA", f"Reclamação suspeita na conversa {conversation_id}", {
        "Pedido": meta.get("order_id"), "Sinais": sinais, "Evidências": evidencias, "Por que": trilha.get("por_que"),
        "O que esperamos": f"decidir_suspeita (procedente/improcedente) em até {prazo}h",
    }, urgencia="alta")
    return {"ok": True, "acao_id": acao_id, "status": "pendente", "slack_id": slack["id"]}


def _decidir_suspeita(amb: Ambiente, trilha: dict, conversation_id: str, decisao: str, justificativa: str) -> Resultado:
    s = amb.estado.suspeitas.get(conversation_id)
    if s is None or s["status"] != "pendente":
        return _erro(f"não há suspeita pendente na conversa {conversation_id}")
    if decisao not in ("procedente", "improcedente"):
        return _erro("decisao deve ser 'procedente' ou 'improcedente'")
    if s["sinalizado_por"] == amb.ator:  # limite: nao_decide_o_que_ele_mesmo_sinalizou
        return _erro("quem sinalizou não decide a própria suspeita")
    s.update(status=decisao, decidido_por=amb.ator, decidido_em=amb.estado.agora_txt(), justificativa=justificativa)
    acao_id = _registrar(amb, "decidir_suspeita", trilha, conversation_id=conversation_id, order_id=s["order_id"])
    out: Resultado = {"ok": True, "acao_id": acao_id, "status": decisao}
    if decisao == "improcedente":
        # Resposta neutra, sem acusação (politicas.reclamacao_suspeita.comunicacao_com_cliente).
        texto = ("Analisamos com cuidado a sua solicitação e, com as informações disponíveis, não foi possível "
                 "seguir com o reenvio ou reembolso. Se tiver qualquer outra informação, é só nos responder.")
        if eh_publica(conversation_id):
            texto += " Seguimos à disposição pelo canal privado."
        out["message_id"], _ = _mensagem(amb, conversation_id, texto, "decidir_suspeita")
        est = amb.estado.conversa(conversation_id)
        est["encerrada"], est["motivo_encerramento"] = True, "suspeita_improcedente"
    return out


def _registrar_cosmetovigilancia(amb: Ambiente, trilha: dict, conversation_id: str, sku: str, descricao: str,
                                 lote: str | None = None, fotos: str | None = None) -> Resultado:
    meta = meta_conversa(conversation_id)
    if meta is None:
        return _erro(f"conversa não encontrada: {conversation_id}")
    prods = dados.tabela("produtos")
    prod = prods[prods["sku"] == sku]
    if prod.empty:
        return _erro(f"sku não encontrado: {sku}")
    if prod.iloc[0]["unidade_negocio"] != "GoBeauté":
        return _erro(f"{sku} não é cosmético/suplemento (GoBeauté): cosmetovigilância não se aplica")
    # Prazo conta do relato: primeira mensagem do cliente na conversa (até agora).
    msgs = dados.tabela("historico_mensagens")
    msgs = _ate_agora(msgs[(msgs["conversation_id"] == conversation_id) & (msgs["autor"] == "cliente")],
                      "timestamp", amb)
    relato = msgs["timestamp"].min() if not msgs.empty else pd.Timestamp(meta["data_abertura"])
    horas = round((pd.Timestamp(amb.agora) - relato).total_seconds() / 3600, 1)
    prazo = CATALOGO["registrar_cosmetovigilancia"].limites["prazo_horas_desde_o_relato"]
    pendencias = []
    if _vazio(lote):
        pendencias.append("lote não informado")
    elif dados.tabela("lotes_producao")["lote"].eq(lote).sum() == 0:
        pendencias.append(f"lote {lote} não consta em lotes_producao")
    if _vazio(fotos):
        pendencias.append("sem fotos anexadas")
    if not amb.estado.conversa(conversation_id)["orientacao_suspensao_em"]:
        pendencias.append("orientar_suspensao_uso ainda não registrado nesta conversa")
    reg_id = amb.estado.novo_id("CSV")
    acao_id = _registrar(amb, "registrar_cosmetovigilancia", trilha, conversation_id=conversation_id,
                         order_id=meta.get("order_id"), referencia=reg_id)
    amb.estado.cosmetovigilancia.append({
        "registro_id": reg_id, "acao_id": acao_id, "conversation_id": conversation_id,
        "order_id": meta.get("order_id"), "sku": sku, "lote": lote, "descricao": mascarar_pii(descricao),
        "fotos": fotos, "relato_em": relato.strftime("%Y-%m-%d %H:%M:%S"), "registrado_em": amb.estado.agora_txt(),
        "horas_desde_relato": horas, "dentro_do_prazo": horas <= prazo, "registrado_por": amb.ator,
        "pendencias": "; ".join(pendencias),
    })
    _postar_no_canal(amb, "COSMETOVIGILÂNCIA", f"Reação adversa registrada ({reg_id}) — {prod.iloc[0]['nome']}", {
        "Conversa": conversation_id, "SKU / lote": f"{sku} / {lote or 'não informado'}", "Descrição": descricao,
        "Prazo": f"{horas}h desde o relato (limite {prazo}h)", "Pendências": "; ".join(pendencias),
        "O que esperamos": "responsável técnica avaliar o caso e o lote",
    }, urgencia="critica" if horas > prazo else "alta", area="qualidade")
    return {"ok": True, "acao_id": acao_id, "registro_id": reg_id, "horas_desde_relato": horas,
            "dentro_do_prazo": horas <= prazo, "pendencias": pendencias}


def _acionar_pessoa(amb: Ambiente, trilha: dict, area: str, assunto: str, contexto: str,
                    urgencia: str | None = None) -> Resultado:
    area_norm = {_sem_acento(a): a for a in AREAS_ACIONAVEIS}.get(_sem_acento(area))
    if area_norm is None:
        return _erro(f"área inválida: {area} (válidas: {list(AREAS_ACIONAVEIS)})")
    urg = _sem_acento(urgencia) if not _vazio(urgencia) else "media"
    if urg not in URGENCIAS:
        return _erro(f"urgência inválida: {urgencia} (válidas: {list(URGENCIAS)})")
    slack = _postar_no_canal(amb, "MÃO LEVANTADA", assunto, {
        "Conversa": amb.conversa_atual, "Contexto": contexto, "Por que": trilha.get("por_que"),
    }, urgencia=urg, area=area_norm)
    acao_id = _registrar(amb, "acionar_pessoa", trilha, referencia=slack["id"])
    return {"ok": True, "acao_id": acao_id, "slack_id": slack["id"], "texto_slack": slack["texto_slack"]}


def _responder_colaborador(amb: Ambiente, trilha: dict, pedido_id: str, resposta: str) -> Resultado:
    slack = _postar_no_canal(amb, "RESPOSTA A COLABORADOR", f"Pedido {pedido_id}", {"Resposta": resposta},
                             urgencia="baixa", area="time")
    acao_id = _registrar(amb, "responder_colaborador", trilha, referencia=slack["id"])
    return {"ok": True, "acao_id": acao_id, "slack_id": slack["id"]}


IMPLEMENTACOES: dict[str, Callable[..., Resultado]] = {
    "classificar_conversa": _classificar_conversa,
    "responder_cliente": _responder_cliente,
    "solicitar_foto": _solicitar_foto,
    "informar_status": _informar_status,
    "responder_duvida": _responder_duvida,
    "orientar_suspensao_uso": _orientar_suspensao_uso,
    "negar_por_politica": _negar_por_politica,
    "agradecer": _agradecer,
    "encerrar_conversa": _encerrar_conversa,
    "abrir_chamado_transportadora": _abrir_chamado_transportadora,
    "reenviar_produto": _reenviar_produto,
    "reimprimir_personalizacao": _reimprimir_personalizacao,
    "trocar_produto": _trocar_produto,
    "reembolsar": _reembolsar,
    "cancelar_pedido": _cancelar_pedido,
    "alterar_endereco": _alterar_endereco,
    "solicitar_aprovacao": _solicitar_aprovacao,
    "aprovar_acao": _aprovar_acao,
    "rejeitar_acao": _rejeitar_acao,
    "sinalizar_suspeita": _sinalizar_suspeita,
    "decidir_suspeita": _decidir_suspeita,
    "registrar_cosmetovigilancia": _registrar_cosmetovigilancia,
    "acionar_pessoa": _acionar_pessoa,
    "responder_colaborador": _responder_colaborador,
}
# Ainda não simuladas (frente "analista", item 8 da agenda): buscar_conversas, consultar_metricas,
# abrir_incidente, notificar_clientes.

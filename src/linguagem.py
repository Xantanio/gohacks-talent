"""Linguagem simples para pessoas: traduz nomes técnicos (ferramentas, categorias, situações) para português comum.

Usado no resumo do dossiê e na interface. A parte técnica (nomes exatos, IDs, notas) continua disponível para
auditoria; aqui só muda como a informação é APRESENTADA (D-029).
"""
from __future__ import annotations

import re

ACAO = {
    "ler_conversa": "leitura da conversa", "consultar_pedido": "consulta ao pedido",
    "consultar_cliente": "consulta ao cadastro do cliente", "consultar_politicas": "consulta às regras",
    "buscar_conversas": "busca de conversas parecidas", "consultar_metricas": "consulta aos indicadores",
    "classificar_conversa": "classificação do assunto", "responder_cliente": "resposta ao cliente",
    "solicitar_foto": "pedido de foto ao cliente", "informar_status": "informe sobre a situação do pedido",
    "abrir_chamado_transportadora": "chamado aberto com a transportadora", "reenviar_produto": "reenvio do produto",
    "reimprimir_personalizacao": "nova produção da personalização", "trocar_produto": "troca do produto",
    "reembolsar": "reembolso", "cancelar_pedido": "cancelamento do pedido", "alterar_endereco": "mudança de endereço",
    "verificar_pagamento": "conferência do pagamento", "validar_cupom": "conferência do cupom",
    "responder_duvida": "resposta à dúvida", "orientar_suspensao_uso": "orientação para suspender o uso do produto",
    "negar_por_politica": "resposta explicando a regra", "agradecer": "agradecimento",
    "encerrar_conversa": "encerramento da conversa", "solicitar_aprovacao": "pedido de aprovação à supervisão",
    "abrir_incidente": "abertura de incidente", "notificar_clientes": "aviso a clientes",
    "aprovar_acao": "aprovação", "rejeitar_acao": "rejeição", "ver_anexos": "conferência das fotos",
    "registrar_cosmetovigilancia": "registro da reação adversa (cosmetovigilância)",
    "sinalizar_suspeita": "pedido de análise de possível reclamação indevida",
    "decidir_suspeita": "decisão sobre possível reclamação indevida", "acionar_pessoa": "chamado a uma pessoa da equipe",
    "responder_colaborador": "resposta a colega",
}
CATEGORIA = {
    "onde_esta_pedido": "Onde está meu pedido", "atraso_entrega": "Atraso na entrega", "item_faltante": "Item faltando",
    "avaria_vazamento": "Produto danificado ou vazando", "erro_personalizacao": "Erro na personalização",
    "cobranca_duplicada": "Cobrança duplicada", "pagamento_pendente": "Pagamento não confirmado",
    "arrependimento_cancelamento": "Desistência ou cancelamento", "troca_fragrancia": "Troca de fragrância",
    "alteracao_endereco": "Mudança de endereço", "reacao_adversa": "Reação ao produto (saúde)",
    "duvida_produto": "Dúvida sobre produto", "cupom_promocao": "Cupom ou promoção", "elogio": "Elogio",
    "sem_interacao": "Sem mensagem do cliente", "outros": "Outros assuntos",
}
SITUACAO = {
    "elegivel": "a regra permite resolver", "nao_elegivel": "fora da regra", "em_andamento": "já há solução em andamento",
    "falta_informacao": "falta uma informação do cliente", "escalar": "precisa de uma pessoa",
}
CAMINHO = {
    "seguir_politica": "seguiu a regra", "executar": "resolveu direto", "solicitar_aprovacao": "pediu aprovação",
    "sinalizar_suspeita": "pediu análise de possível reclamação indevida",
}
PENDENCIA = {
    "aprovacao": "Aprovação", "suspeita": "Possível reclamação indevida", "publicacao": "Resposta aguardando revisão",
    "cosmetovigilancia": "Registro de reação adversa", "decisao_negocio": "Decisão de negócio",
}
AREA = {"supervisão": "Supervisão", "logística": "Logística", "qualidade": "Qualidade", "produção": "Produção",
        "jurídico": "Jurídico", "comercial": "Comercial", "time": "Equipe"}
URGENCIA = {"baixa": "Baixa", "media": "Média", "alta": "Alta", "critica": "Crítica"}
CANAL = {"reclame_aqui": "Reclame Aqui", "email": "E-mail", "whatsapp": "WhatsApp"}

_TERMOS = {**ACAO, **CATEGORIA, **SITUACAO, "p_verdadeira": "chance de a reclamação ser verdadeira",
           "aprovacao_id": "número da aprovação"}
_RE_TERMO = re.compile(r"\b(" + "|".join(sorted(map(re.escape, _TERMOS), key=len, reverse=True)) + r")\b")


def humanizar(texto: str | None) -> str:
    """Troca nomes técnicos por português comum dentro de qualquer frase (ex.: motivos do validador)."""
    if not texto:
        return ""
    return _RE_TERMO.sub(lambda m: _TERMOS[m.group(1)], str(texto))


def acao(nome: str) -> str:
    return ACAO.get(nome, nome.replace("_", " "))


def categoria(nome: str | None) -> str:
    return CATEGORIA.get(nome or "", nome or "-")

"""Estado mutável da semana simulada: o que as ferramentas de ação gravam nos "sistemas".

Os CSVs de `data/atendimento` são a foto de 02/11 08:00 e nunca são alterados. Tudo o que o agente faz
(reenvio, estorno, chamado, mensagem ao cliente, aprovação, suspeita, mão levantada...) vai para um `Estado`,
que é compartilhado por todos os papéis (atendente, analista, supervisor) da mesma execução.

Trilha de decisão (auditoria):
- `log` guarda TODA chamada de ferramenta (inclusive leituras e chamadas recusadas), com a `trilha` informada.
- `acoes` guarda só as ações executadas, no formato de `acoes_executadas_sistemas.csv` + a trilha
  (por que, regra citada, evidências, alternativas e custo esperado). É o que um auditor lê primeiro.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime
from itertools import count
from pathlib import Path
from typing import Any

from . import dados

# Sistema "dono" de cada ação (nomes iguais aos de acoes_executadas_sistemas.csv quando existem lá).
SISTEMA_POR_ACAO = {
    "reenviar_produto": "OMS de trocas e reenvios",
    "trocar_produto": "OMS de trocas e reenvios",
    "reimprimir_personalizacao": "Sistema de produção Gocase",
    "reembolsar": "Gateway de pagamentos",
    "cancelar_pedido": "ERP de pedidos",
    "alterar_endereco": "ERP de pedidos",
    "abrir_chamado_transportadora": "Portal da transportadora",
    "registrar_cosmetovigilancia": "Sistema de qualidade (cosmetovigilância)",
    "validar_cupom": "Plataforma de cupons",
    "acionar_pessoa": "Canal do time (Slack)",
    "responder_colaborador": "Canal do time (Slack)",
    "solicitar_aprovacao": "Fila do supervisor",
    "aprovar_acao": "Fila do supervisor",
    "rejeitar_acao": "Fila do supervisor",
    "sinalizar_suspeita": "Fila do supervisor",
    "decidir_suspeita": "Fila do supervisor",
    "abrir_incidente": "Gestão de incidentes",
    "notificar_clientes": "Plataforma de comunicação",
}
SISTEMA_PADRAO = "Central de atendimento"


@dataclass
class Estado:
    tique: int = 0
    log: list[dict[str, Any]] = field(default_factory=list)            # toda chamada de ferramenta
    acoes: list[dict[str, Any]] = field(default_factory=list)          # ações executadas (auditoria)
    mensagens: list[dict[str, Any]] = field(default_factory=list)      # enviadas ao cliente
    conversas: dict[str, dict[str, Any]] = field(default_factory=dict)  # estado por conversa
    pedidos: dict[str, dict[str, Any]] = field(default_factory=dict)    # alterações por pedido (cancelado, endereço)
    oms: list[dict[str, Any]] = field(default_factory=list)            # reenvios, trocas, novas produções
    estornos: list[dict[str, Any]] = field(default_factory=list)
    chamados: list[dict[str, Any]] = field(default_factory=list)       # chamados com transportadora
    aprovacoes: dict[str, dict[str, Any]] = field(default_factory=dict)
    suspeitas: dict[str, dict[str, Any]] = field(default_factory=dict)  # por conversation_id
    cosmetovigilancia: list[dict[str, Any]] = field(default_factory=list)
    canal_do_time: list[dict[str, Any]] = field(default_factory=list)  # mãos levantadas e respostas a colaboradores
    validacoes: list[dict[str, Any]] = field(default_factory=list)     # cada tentativa de texto: validador + juiz (D-022)
    incidentes: list[dict[str, Any]] = field(default_factory=list)     # abertos pelo analista (D-026)
    notificacoes: list[dict[str, Any]] = field(default_factory=list)   # avisos proativos, um por pedido (D-026)
    pendencias: list[dict[str, Any]] = field(default_factory=list)     # mãos levantadas à espera de uma pessoa (D-027)
    eventos: list[dict[str, Any]] = field(default_factory=list)        # o que aconteceu na semana simulada (D-027)
    _seq: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def agora(self) -> datetime:
        return dados.agora(self.tique)

    def agora_txt(self) -> str:
        return self.agora.strftime("%Y-%m-%d %H:%M:%S")

    def novo_id(self, prefixo: str, largura: int = 5) -> str:
        """IDs com sufixo `AG` (agente) para nunca colidir com os IDs históricos dos CSVs."""
        contador = self._seq.setdefault(prefixo, count(1))
        return f"{prefixo}-AG{next(contador):0{largura}d}"

    def conversa(self, conversation_id: str) -> dict[str, Any]:
        return self.conversas.setdefault(conversation_id, {
            "encerrada": False, "motivo_encerramento": None, "aguardando_foto": False,
            "categoria": None, "categoria_secundaria": None, "orientacao_suspensao_em": None,
        })

    def registrar_acao(self, *, acao: str, conversation_id: str | None, order_id: str | None, executado_por: str,
                       valor_rs: float | None = None, aprovado_por: str | None = None,
                       referencia_sistema: str | None = None, trilha: dict[str, Any] | None = None) -> str:
        acao_id = self.novo_id("AC", 6)
        self.acoes.append({
            "acao_id": acao_id,
            "conversation_id": conversation_id,
            "order_id": order_id,
            "data_hora": self.agora_txt(),
            "tique": self.tique,
            "sistema": SISTEMA_POR_ACAO.get(acao, SISTEMA_PADRAO),
            "acao": acao,
            "valor_rs": valor_rs,
            "executado_por": executado_por,
            "aprovado_por": aprovado_por,
            "referencia_sistema": referencia_sistema,
            "trilha": trilha or {},
        })
        return acao_id

    # ------------------------------------------------------------------
    # Pendências: o que espera uma pessoa e como retomar depois (D-027)
    # ------------------------------------------------------------------
    def abrir_pendencia(self, tipo: str, *, conversation_id: str | None = None, order_id: str | None = None,
                        resumo: str = "", prazo: str | None = None, **dados_pendencia: Any) -> str:
        pid = self.novo_id("PEN")
        self.pendencias.append({
            "pendencia_id": pid, "tipo": tipo, "status": "aberta", "conversation_id": conversation_id,
            "order_id": order_id, "resumo": resumo, "criado_em": self.agora_txt(), "tique": self.tique,
            "prazo": prazo, "dados": dados_pendencia, "alertas": [], "resolvido_por": None, "resolvido_em": None,
            "resolucao": None,
        })
        return pid

    def pendencia(self, pendencia_id: str) -> dict[str, Any] | None:
        return next((p for p in self.pendencias if p["pendencia_id"] == pendencia_id), None)

    def pendencias_abertas(self, tipo: str | None = None) -> list[dict[str, Any]]:
        return [p for p in self.pendencias if p["status"] == "aberta" and (tipo is None or p["tipo"] == tipo)]

    def resolver_pendencia(self, pendencia_id: str, por: str, resolucao: str, status: str = "resolvida") -> None:
        p = self.pendencia(pendencia_id)
        p.update(status=status, resolvido_por=por, resolvido_em=self.agora_txt(), resolucao=resolucao)

    def registrar_evento(self, tipo: str, descricao: str, **extra: Any) -> None:
        self.eventos.append({"tique": self.tique, "data_hora": self.agora_txt(), "tipo": tipo,
                             "descricao": descricao, **extra})

    def enviar_mensagem(self, conversation_id: str, texto: str, ferramenta: str, autor: str,
                        publica: bool = False) -> str:
        message_id = self.novo_id("MSG", 6)
        self.mensagens.append({
            "message_id": message_id, "conversation_id": conversation_id, "timestamp": self.agora_txt(),
            "tique": self.tique, "autor": autor, "ferramenta": ferramenta, "publica": publica, "texto": texto,
        })
        return message_id

    # ------------------------------------------------------------------
    # Exportação para auditoria / passagem de bastão
    # ------------------------------------------------------------------
    def exportar(self, pasta: str | Path) -> dict[str, Path]:
        """Grava a execução em `pasta`: CSVs no formato dos sistemas + log completo + canal do time em Markdown."""
        pasta = Path(pasta)
        pasta.mkdir(parents=True, exist_ok=True)
        arquivos = {
            "acoes_executadas": _csv(pasta / "acoes_executadas.csv", [
                {**a, "trilha": json.dumps(a["trilha"], ensure_ascii=False)} for a in self.acoes]),
            "mensagens_enviadas": _csv(pasta / "mensagens_enviadas.csv", self.mensagens),
            "aprovacoes": _csv(pasta / "aprovacoes.csv", list(self.aprovacoes.values())),
            "suspeitas": _csv(pasta / "suspeitas.csv", [
                {"conversation_id": k, **v} for k, v in self.suspeitas.items()]),
            "oms": _csv(pasta / "trocas_reenvios.csv", self.oms),
            "estornos": _csv(pasta / "estornos.csv", self.estornos),
            "chamados": _csv(pasta / "chamados_transportadora.csv", self.chamados),
            "cosmetovigilancia": _csv(pasta / "cosmetovigilancia.csv", self.cosmetovigilancia),
            "validacoes": _csv(pasta / "validacoes.csv", self.validacoes),
            "incidentes": _csv(pasta / "incidentes.csv", self.incidentes),
            "notificacoes": _csv(pasta / "notificacoes.csv", self.notificacoes),
            # objetos internos de retomada (chaves com "_", ex.: o Ticket) não vão para o CSV
            "pendencias": _csv(pasta / "pendencias.csv", [
                {**p, "dados": {k: v for k, v in p["dados"].items() if not k.startswith("_")}} for p in self.pendencias]),
            "eventos": _csv(pasta / "eventos_semana.csv", self.eventos),
        }
        caminho_log = pasta / "log_chamadas.jsonl"
        with caminho_log.open("w", encoding="utf-8") as f:
            for e in self.log:
                f.write(json.dumps(e, ensure_ascii=False, default=str) + "\n")
        arquivos["log"] = caminho_log
        caminho_slack = pasta / "canal_do_time.md"
        caminho_slack.write_text("\n\n---\n\n".join(m["texto_slack"] for m in self.canal_do_time) or "(vazio)\n",
                                 encoding="utf-8")
        arquivos["canal_do_time"] = caminho_slack
        return arquivos


def _csv(caminho: Path, linhas: list[dict[str, Any]]) -> Path:
    colunas: list[str] = []
    for linha in linhas:
        colunas += [c for c in linha if c not in colunas]
    with caminho.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=colunas)
        w.writeheader()
        for linha in linhas:
            w.writerow({k: (json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list)) else v)
                        for k, v in linha.items()})
    return caminho

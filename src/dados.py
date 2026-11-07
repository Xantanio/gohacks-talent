"""Carregamento dos CSVs de data/atendimento (uma vez por processo) e relógio do ambiente.

- Colunas de identificador/documento são lidas como texto (preserva zeros à esquerda: NF, NSU, final de cartão...).
- Colunas de data são convertidas para datetime (NaT quando vazias).
- Nada aqui conhece regra de negócio: é só acesso aos "sistemas".
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd

RAIZ = Path(__file__).resolve().parent.parent
DIR_DADOS = RAIZ / "data" / "atendimento"

# "Agora" do ambiente = tique 0. Cada tique = 1 hora; último tique = 159 (08/11 23:00).
INICIO_EVENTO = datetime(2026, 11, 2, 8, 0, 0)
ULTIMO_TIQUE = 159

# Colunas que nunca devem virar número.
_COLUNAS_TEXTO = re.compile(
    r"(_id$|^id_|^cep|^cpf$|^telefone$|^nsu$|^codigo_|^numero_nota_fiscal$|^cartao_final$"
    r"|^end_to_end_pix$|^hash_perceptual$|^referencia_sistema$|^pedido_ref$|^order_id_original$"
    r"|^pedido_reposicao_id$|^lote|^sku|^transacao_id$|^anexo_id$|^cupom$)"
)
_COLUNAS_DATA = re.compile(r"(^data_|^data$|_em$|^timestamp$|^previsao_|^nova_previsao_|^prazo_|^fim_vigencia$|^inicio_vigencia$)")


def agora(tique: int = 0) -> datetime:
    """Data/hora simulada do tique (0 = 02/11/2026 08:00)."""
    if not 0 <= tique <= ULTIMO_TIQUE:
        raise ValueError(f"tique fora do evento: {tique} (0..{ULTIMO_TIQUE})")
    return INICIO_EVENTO + timedelta(hours=tique)


@lru_cache(maxsize=None)
def tabela(nome: str) -> pd.DataFrame:
    """Lê `data/atendimento/<nome>.csv` uma única vez. Não altere o DataFrame retornado (use .copy())."""
    caminho = DIR_DADOS / f"{nome}.csv"
    colunas = pd.read_csv(caminho, nrows=0).columns
    dtype = {c: str for c in colunas if _COLUNAS_TEXTO.search(c)}
    df = pd.read_csv(caminho, dtype=dtype)
    for c in colunas:
        if _COLUNAS_DATA.search(c) and pd.api.types.is_string_dtype(df[c]):
            convertido = pd.to_datetime(df[c], errors="coerce")
            # só converte se a coluna realmente parece data (evita estragar textos)
            if convertido.notna().sum() >= df[c].notna().sum() * 0.9:
                df[c] = convertido
    return df


def politicas() -> dict[str, dict[str, Any]]:
    """politicas.csv como {secao: {regra: valor}} (valores numéricos convertidos quando possível)."""
    df = pd.read_csv(DIR_DADOS / "politicas.csv", dtype=str)
    out: dict[str, dict[str, Any]] = {}
    for secao, regra, valor in df[["secao", "regra", "valor"]].itertuples(index=False):
        out.setdefault(secao, {})[regra] = _converter(valor)
    return out


def _converter(valor: Any) -> Any:
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return None
    texto = str(valor).strip()
    for tipo in (int, float):
        try:
            return tipo(texto)
        except ValueError:
            pass
    if texto.lower() in ("true", "false"):
        return texto.lower() == "true"
    return texto


def registro(linha: pd.Series | dict) -> dict[str, Any]:
    """Converte uma linha em dict JSON-friendly: NaN/NaT -> None, Timestamp -> 'AAAA-MM-DD HH:MM:SS', numpy -> nativo."""
    itens = linha.items() if isinstance(linha, (pd.Series, dict)) else linha
    out: dict[str, Any] = {}
    for k, v in itens:
        if v is None or (not isinstance(v, (list, dict, str)) and pd.isna(v)):
            out[k] = None
        elif isinstance(v, pd.Timestamp):
            out[k] = v.strftime("%Y-%m-%d %H:%M:%S") if (v.hour or v.minute or v.second) else v.strftime("%Y-%m-%d")
        elif hasattr(v, "item"):  # numpy scalar
            out[k] = v.item()
        else:
            out[k] = v
    return out


def registros(df: pd.DataFrame) -> list[dict[str, Any]]:
    return [registro(linha) for _, linha in df.iterrows()]

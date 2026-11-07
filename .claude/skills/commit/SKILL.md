---
name: commit
description: Faz commits seguros e bem descritos neste repositório. Use ao terminar cada etapa funcional (um módulo, um teste passando, uma correção) ou quando o usuário pedir "commit", "commita", "salva no git".
---

# Commit seguro

Commits pequenos, um por etapa que funciona. Mensagens em português do Brasil.
O repositório tem remoto privado no GitHub: mesmo assim, segredo e dado pessoal nunca entram (quem tem acesso ao repo não deveria ter acesso a eles).

## Passos

1. **Ver o que mudou**
   - `git status --short` e `git diff` (e `git diff --staged` se já houver algo no stage).
   - Arquivos novos (`??`) não aparecem no `git diff`: abra-os para saber o que são.
   - Se não houver mudança, avise e pare.

2. **Testar antes**
   - `.\venv\Scripts\python.exe -m pytest -q` (coleta só `tests/test_*.py`).
   - Testes falhando → não commitar; mostre o erro e pergunte se quer commitar mesmo assim.

3. **Adicionar só o que é da etapa**
   - `git add <arquivos específicos>`. Nunca `git add .` / `git add -A` sem conferir a lista.
   - Nunca adicionar: `.env`, `venv/`, `__pycache__/`, `saida/`, `data/` (CSVs do evento com CPF, telefone e cartão; estão no `.gitignore`, não force com `-f`), arquivos grandes gerados.
   - Mudanças de assuntos diferentes → commits separados.

4. **Barreira de segurança (obrigatória, depois do `git add` e antes do commit)**
   - Conferir a lista do que vai entrar: `git diff --staged --stat`. Se aparecer algo do item 3 em "Nunca adicionar", tire com `git restore --staged <arquivo>`.
   - Procurar segredos no que está no stage (PowerShell, aspas simples):
     `git diff --staged | Select-String -Pattern 'gw-tok-|AI_PROXY_API_KEY\s*=\s*\S+|api_key\s*=\s*[''"]|Bearer\s+[A-Za-z0-9_-]{20,}'`
     (bash: `git diff --staged | grep -E "gw-tok-|AI_PROXY_API_KEY\s*=\s*\S+|api_key\s*=\s*['\"]|Bearer\s+[A-Za-z0-9_-]{20,}"`)
   - Achou token/chave? **Pare**, `git restore --staged <arquivo>`, mova o valor para o `.env` (lido via variável de ambiente) e avise o usuário. Não faça o commit.

5. **Mensagem (Conventional Commits, PT-BR)**
   - Título: `<tipo>(<escopo>): <resumo no imperativo, até 72 caracteres>`
   - Tipos: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`.
   - Escopo = módulo ou área: `dados`, `ferramentas`, `acoes`, `estado`, `politicas`, `veracidade`, `decisao`, `llm`, `agente`, `api`, `tests`, `docs`, `skills`.
   - Corpo opcional (1–3 linhas): o porquê da mudança. Se a etapa tomou uma decisão de projeto, registre em `docs/DECISOES.md` no mesmo commit e cite o número (ex.: "ver D-005").
   - **Sempre** terminar com a linha de coautoria: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
   - Exemplos de título:
     - `feat(veracidade): detecta foto reaproveitada por hash_perceptual`
     - `fix(decisao): aplica teto de R$ 150 para clientes novos`
     - `test(ferramentas): cobre consultar_pedido com order_id vazio`

6. **Commitar e confirmar**
   - No PowerShell, um `-m` por parágrafo (título, corpo, coautoria):
     ```
     git commit -m "feat(acoes): simula ferramentas de ação com alçada e trilha" -m "Trilha obrigatória em toda ação que altera estado (ver D-006)." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
     ```
   - Mostre `git log --oneline -1` ao usuário.

## Não fazer

- **Não dar `git push`** sem o usuário pedir explicitamente.
- Não usar `--no-verify`, `git add -f` em arquivo ignorado, `--amend` em commit já enviado, `reset --hard` ou `push --force`.
- Não commitar no meio de uma etapa quebrada só para "salvar".
- Não commitar na `main` se o usuário tiver pedido para trabalhar em branch.

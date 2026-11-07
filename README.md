# Agente de Atendimento — GoHacks Talent 2026 (Desafio 1)

Um **colega de trabalho** para o atendimento da Gocase e da GoBeauté. Ele lê a fila, consulta os sistemas, decide pela
política da empresa, **resolve** com as ferramentas permitidas e **chama uma pessoa** quando não pode decidir sozinho.
Um segundo agente, o **analista**, encontra problemas que se repetem (uma impressora com defeito, uma transportadora
atrasando numa região) e avisa os clientes afetados **antes** de eles reclamarem.

Não é um chatbot: quem decide e executa é código auditável. A IA classifica o pedido e redige o texto, e todo texto
passa por uma revisão automática antes de chegar ao cliente.

## Resultado na fila do Reclame Aqui (24 tickets, 02/11/2026 08:00)

| | |
|---|---|
| Respondidos sem intervenção humana | **23 de 24** (o único retido é uma reação alérgica, que por regra exige uma pessoa) |
| Classificação da IA igual à conferência manual | 23 de 24 (a diferença não muda a ação) |
| Problemas recorrentes encontrados | 4 (impressora PRN-02, Expressa Já no Nordeste, brinde faltando no CD2, pico de cobrança duplicada) |
| Clientes avisados antes de reclamar | **126**, só quem aceita comunicação, sem repetir e respeitando o limite de 300 por dia |
| Custo de IA por execução | cerca de US$ 0,0012 na classificação (Jev) e cerca de 19 mil tokens de texto (o proxy não informa o preço) |

## Como rodar

**Pré-requisitos (Windows):**

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

- `data/atendimento/`: os CSVs do desafio. **Não vão para o git** (têm CPF, telefone e cartão sintéticos); copie a pasta manualmente.
- `.env` com `AI_PROXY_API_KEY=...` para usar IA. Também não vai para o git. Sem ele, desligue "Usar IA" na interface.

**Interface da demo:** clique duplo em `rodar_app.bat`, ou

```powershell
.\venv\Scripts\python.exe -m streamlit run app.py
```

Ela abre em `http://localhost:8501`. Na barra lateral, clique em **Rodar a fila** (com IA, leva de 1 a 2 minutos).

**Linha de comando:**

```powershell
.\venv\Scripts\python.exe -m src.agente                         # fila do Reclame Aqui, com IA e analista
.\venv\Scripts\python.exe -m src.agente --semana                # + simula a semana até 08/11
.\venv\Scripts\python.exe -m src.agente --canais reclame_aqui email whatsapp --limite 5
.\venv\Scripts\python.exe -m src.agente --sem-ia                # sem IA: tudo vai para uma pessoa
.\venv\Scripts\python.exe -m src.analista                       # só a varredura de problemas recorrentes
```

Cada execução grava em `saida/<data-hora>/`: `resumo.md`, um dossiê por ticket em `dossies/`, `analista.md`,
`semana.md`, as planilhas dos "sistemas" (ações, mensagens, reenvios, estornos, chamados, aprovações, pendências,
avisos, validações) e o registro completo de chamadas (`log_chamadas.jsonl`).

**Testes:** são 167 e rodam em cerca de 30 s, sem chamar a IA. Os testes de ataque contra a IA (tentativas de manipular o agente pela mensagem do cliente) rodam ao vivo com `RODAR_IA=1`.

```powershell
.\venv\Scripts\python.exe -m pytest -q
```

## Roteiro de demonstração (cerca de 5 minutos)

1. **Fila:** 23 de 24 tickets resolvidos. Abra o resumo do **AT116160**: o atendente percebeu que o atraso se repete e chamou o analista.
2. **Problemas recorrentes:** Expressa Já no Nordeste, com 64 clientes avisados antes de reclamar e 72 chamados abertos com a transportadora. A impressora PRN-02 foi encontrada pelo analista sozinho.
3. **Para decidir:** você faz o papel da supervisão.
   - Aprove o reembolso do **AT116520**: o agente executa e avisa o cliente na hora.
   - Libere a orientação de saúde do **AT116568** e registre a reação adversa (prazo de 24h).
   - Estenda o prazo para os 12 clientes do brinde que já tinham passado dos 7 dias.
4. **Semana:** "Até o fim". O que ficou sem decisão recebe aviso de prazo acabando e de prazo vencido.
5. **Auditoria:** cada ação com quem fez, quem aprovou e por quê, mais as notas do revisor de qualidade em cada texto.

## Como funciona

```
ticket ─► classificar (IA: Jev) ─► investigar (ferramentas de leitura) ─► política (regras em politicas.csv)
       ─► veracidade (sinais nas fotos e no histórico) ─► decisão (menor custo esperado)
       ─► agir (ferramentas do catálogo, dentro da alçada) ─► responder (IA redige; regras + revisor de outra IA validam)
       ─► se precisar de uma pessoa: pendência ─► supervisão decide ─► o agente retoma sozinho
```

- **Atendente** (`agente.py`): prioriza a fila por prazo (SLA), custo do canal, valor do cliente e casos de saúde, e processa ticket a ticket.
- **Analista** (`analista.py`): compara a taxa de reclamação de cada origem (impressora, transportadora e estado, centro de distribuição, lote, pagamento) com as demais, abre um incidente para a área responsável e avisa quem ainda não reclamou.
- **Supervisão** (`supervisor.py` e a aba "Para decidir"): aprova ou rejeita, decide suspeitas, libera respostas retidas, registra reações adversas. Depois de cada decisão, o agente continua o caso.
- **Semana** (`semana.py`): avança o relógio de hora em hora até 08/11, cobra os prazos e roda a rotina diária do analista.

**Proteções principais:**
- A IA não escolhe ações nem decide dinheiro.
- O texto do cliente é tratado como dado, nunca como instrução.
- CPF, cartão e telefone são mascarados; no Reclame Aqui (resposta pública), também o número do pedido e o rastreio.
- Nenhuma ação fora da alçada sem aprovação de outra pessoa.
- Nada de cupom inventado.
- Respostas com promessa, prazo ou valor que ninguém decidiu são reprovadas e reescritas automaticamente.
- Toda ação registra **por quê** (regra, evidências, alternativas e custo).

## Estrutura

| Arquivo | Papel |
|---|---|
| `src/dados.py` | Carrega os CSVs e define o relógio do ambiente |
| `src/ferramentas.py`, `src/acoes.py`, `src/acoes_analista.py` | As 35 ferramentas do catálogo, simuladas com os mesmos nomes, parâmetros, papéis e limites |
| `src/estado.py` | O que muda durante a execução (ações, mensagens, pendências, incidentes) e a exportação |
| `src/politicas.py` | Regras da empresa a partir de `politicas.csv` (prazos, foto, alçada, separação) |
| `src/veracidade.py` | Chance de a reclamação ser verdadeira (foto repetida, data da foto, descrição incoerente, histórico) |
| `src/decisao.py` | Escolha pelo menor custo esperado, com freios para clientes novos |
| `src/llm.py` | IA via AI Proxy: classificação (Jev), redação (glm-5.3), revisor (gpt-6-astra), validação e custos |
| `src/agente.py`, `src/analista.py`, `src/supervisor.py`, `src/semana.py` | Os agentes, a supervisão e a semana |
| `src/linguagem.py` | Tradução dos termos técnicos para português comum, na interface e nos resumos |
| `app.py` | Interface Streamlit |

## Documentação

- [`CLAUDE.md`](CLAUDE.md): regras do desafio, arquitetura, proteções e agenda de desenvolvimento.
- [`docs/DECISOES.md`](docs/DECISOES.md): **30 decisões de projeto**, cada uma com contexto, decisão, alternativas descartadas e o que os dados mostraram.
- [`docs/PROMPTS.md`](docs/PROMPTS.md): os prompts de todos os agentes, gerados a partir do código, com exemplo real e lista de verificação.

## Limitações conhecidas

- **Os dados são uma foto de 02/11:** durante a semana não chegam mensagens, fotos nem entregas novas. Por isso a semana cobra prazos e avisos, mas o analista não reavalia atrasos (pareceria que tudo atrasou).
- **Só o Reclame Aqui foi validado com IA real.** E-mail e WhatsApp rodam, mas o prazo de 30 minutos do WhatsApp ainda não respeita o horário comercial (8h às 20h).
- **A revisão automática procura palavras-chave** e às vezes reprova um texto bom. Nesses casos ele é reescrito ou vai para uma pessoa (o lado seguro).
- **O histórico é uma amostra de 1 em cada 4 clientes:** em produção, o volume de avisos seria cerca de 4 vezes maior.

## Próximas melhorias

1. **WhatsApp e e-mail:** prazo do WhatsApp só em horário comercial e validação com IA real dos 41 tickets desses canais.
2. **Entrada de conversas novas:** processar mensagens e fotos que chegam durante a semana (ex.: o cliente envia a foto pedida e o caso continua sozinho).
3. **Pedidos de colegas** (canal do time): responder dentro da política.
4. **Revisão dos avisos do analista pelo revisor de qualidade**, como já acontece com as respostas.
5. **Ajustes visuais na interface**, depois da revisão da equipe.

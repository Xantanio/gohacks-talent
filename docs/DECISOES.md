# Registro de decisões de projeto

Uma entrada por decisão, da mais recente para a mais antiga. Formato: **contexto → decisão → alternativas descartadas → consequência**.
Decisões tomadas *em cada ticket* não ficam aqui: ficam na trilha de cada ação (`saida/acoes_executadas.csv`, coluna `trilha`) e no dossiê do ticket.

---

## D-028 · Interface da demo em Streamlit (no lugar da API)
- **Contexto:** o item 9 previa uma API FastAPI "para demo". Na demo, quem avalia precisa **ver** o agente trabalhar e uma pessoa precisa **agir** como supervisor, para fechar o ciclo da D-027. Nada no projeto pede integração com outro sistema.
- **Decisão:** `app.py` em Streamlit (`streamlit run app.py`), com seis abas:
  - **Fila:** tickets priorizados e o dossiê de cada um.
  - **Supervisor:** formulários por tipo de pendência que chamam `supervisor.py`; o agente retoma na hora.
  - **Analista:** incidentes, evidências, avisos e o que foi verificado sem incidente.
  - **Canal do time.**
  - **Semana:** +1 hora, +1 dia ou até 08/11, com os eventos.
  - **Auditoria:** trilha de cada ação, validações do juiz e custo.
  A barra lateral roda a fila com ou sem IA e mostra os números principais. O estado fica na sessão do navegador e a execução também grava `saida/<data>/`.
- **Descartado:** FastAPI. Exigiria um cliente para ser mostrada e não resolve o papel humano. Se surgir integração (ex.: n8n), a API é uma camada fina sobre `agente.rodar` e `supervisor.*`.
- **Verificação:** `tests/test_app.py` roda a interface sem navegador (`streamlit.testing.AppTest`): rodar sem IA, liberar sem texto (pede texto), liberar com texto (publica) e +1 dia.
- `requirements.txt` criado (pandas, requests, streamlit, pytest), com as versões do venv.

## D-027 · Ciclo da mão levantada fechado (pendências → supervisor → retomada) e semana simulada
- **Contexto:** os agentes pediam aprovação, sinalizavam suspeita, retinham respostas e acionavam o supervisor, mas nada retomava o caso depois (ex.: o reembolso do AT116520 ficava eternamente "faltando aprovacao_id"). E o agente rodava só no tique 0.
- **Pendências** (`Estado.pendencias`, exportadas em `pendencias.csv`). Toda mão levantada vira pendência com o necessário para retomar:

| Tipo | Quem cria | Prazo |
|---|---|---|
| `aprovacao` | atendente | SLA do ticket (o cliente está esperando) |
| `suspeita` | atendente | 24h, pela política |
| `publicacao` | atendente (resposta retida: rascunho + mensagens de ferramenta retidas com parâmetros) | SLA do ticket |
| `cosmetovigilancia` | atendente (sku, lote, fotos, descrição prontos) | 24h do primeiro relato |
| `decisao_negocio` | analista (clientes fora do prazo de reposição) | — |

- **Supervisor** (`supervisor.py`): cada decisão usa a ferramenta do catálogo com o papel certo, e o próprio agente retoma em seguida.
  - **Aprovar** → o atendente executa a ação com o `aprovacao_id` e avisa o cliente.
  - **Rejeitar** → resposta neutra, sem a justificativa interna.
  - **Suspeita procedente** → o atendente reprocessa o ticket, e `decisao.py` não sinaliza de novo uma suspeita já julgada.
  - **Liberar** → executa as mensagens retidas e publica o texto. Mesmo editado pela pessoa, o texto passa pelo validador.
  - **Registrar cosmetovigilância.**
  - **Estender prazo** → o analista avisa os clientes que tinham ficado de fora.
  - As mensagens depois de decisão humana são textos fixos validados. A rejeição libera a palavra da ação negada, porque o validador por palavra-chave a leria como promessa (limite da D-018).
- **Semana** (`semana.py`, `--semana`): o relógio avança de hora em hora até 08/11 23:00.
  - Alerta pendência com 75% do prazo consumido e pendência vencida, **uma vez cada**.
  - Chamados com transportadora sem resposta: **um alerta por transportadora por tique**. A primeira versão gerou 72 mãos levantadas de uma vez.
  - O analista trata os incidentes todo dia às 08:00 (novo limite diário de avisos, chamados de pedidos novos no escopo).
- **Limitação dos dados, que mudou o desenho:** durante a semana nenhum pedido é entregue e não há evento de rastreio novo. Reavaliar atraso no tique atual faria todo pedido em trânsito de toda transportadora parecer atrasado, e o incidente da Expressa Já "sumia" a partir de 04/11 porque a taxa de todas subia junto. Por isso a varredura diária detecta com os dados do tique 0 (`varrer(tique_deteccao=0)`) e só cuida do acúmulo. Com status de entrega evoluindo (API do evento), basta passar o tique atual. Pedidos criados na semana nunca são separados nos dados e não são tratados como atraso.
- **Incidente com chave estável** (impressora, transportadora, CD): o escopo pode crescer na semana (uma UF nova) sem abrir incidente duplicado.
- **Execução real** (Reclame Aqui + semana): 23 de 24 respondidos e 126 avisados. Ficaram 5 pendências; sem supervisor, todas venceram com os alertas devidos (é o que a interface vai resolver). 72 chamados da Expressa Já sem resposta da transportadora até 04/11.

## D-026 · Agente analista: detecção com taxa e janela, aviso proativo com travas, passagem do atendente
- **Ferramentas simuladas** (`acoes_analista.py`):
  - `buscar_conversas`: até 500 resultados; filtro de lote **por item**.
  - `consultar_metricas`: por dia, categoria, canal, marca, transportadora, UF, SKU, lote, impressora e CD.
  - `abrir_incidente`: tipos do catálogo; não duplica; posta no canal do time para a área responsável.
  - `notificar_clientes`: **300 por dia**, **consentimento** (`aceita_comunicacao_*`), nunca o mesmo aviso duas vezes, texto mascarado.
  - O registro das implementações passou a ser feito na primeira chamada, porque importar `acoes` antes de `ferramentas` criava um ciclo de import.
- **Detecção** (`analista.py`), mesma régua para impressora e brinde por CD:
  - Taxa semanal de reclamação **só entre pedidos entregues** (quem não recebeu não teve como reclamar).
  - **Confirma** o incidente com uma semana ≥ 3× a taxa das demais origens no período todo (piso de 5%). Depois **estende** a janela pelas semanas vizinhas com ≥ 2× e pelo menos 2 reclamações.
  - A janela vai da primeira à última separação de pedido reclamado.
  - Transportadora: UFs com pelo menos 5 atrasados e taxa ≥ 50%. Lote: só defeito de produto citado pelo cliente. Gateway: últimos 7 dias contra a média semanal.
- **Calibração pela execução real** (o que deu errado antes de dar certo):
  1. A expressão regular de personalização casava "nome" e "impress" em qualquer conversa e o denominador incluía pedidos em trânsito: **a PRN-02 não era detectada**.
  2. Atribuir o lote pelo produto citado com 3 palavras gerou 3 falsos incidentes de lote. Passou a usar só o nome do produto e só vazamento, tampa ou rachadura ("amassado" é transporte).
  3. A base da **mesma semana** escondia a primeira semana do CD2, porque o CD1 teve reclamações naquela semana. Passou a usar a base do período todo.
  4. A base do período todo deixou a semana de 26/10 da PRN-02 (18% contra 6%) logo abaixo de 3×. Por isso o critério virou "confirmar com 3×, estender com 2×".
  5. Na varredura final, um incidente já tratado pelo atendente repetia a escalação de "fora do prazo". Agora incidente existente não repete escalação nem chamados.
- **Regra da política no aviso:** reposição só até 7 dias do recebimento. O aviso que promete nova produção ou envio do brinde vai só para quem está no prazo ou ainda não recebeu. Os demais vão para decisão humana (a falha foi da empresa).
- **Passagem do atendente para o analista:** orquestração interna. Em categoria repetível, o atendente pergunta se o pedido está num padrão, e o analista (outra cadeira, mesmo estado) trata o incidente. Fica no dossiê (seção 9), no resumo e na trilha (`origem: regra (analista, chamado pelo atendente em ATxxxx)`). A varredura final pega o que nenhum ticket acionou.
- **Execução real (Reclame Aqui, 24 tickets):** 23 respondidos e 1 retido; 4 incidentes; **126 clientes avisados antes de reclamar**.
  - Expressa Já no Nordeste: 64 avisados e 72 chamados, a partir do AT116160.
  - Brinde do CD2: 27 avisados e 12 fora do prazo para decisão humana, a partir do AT116501.
  - PRN-02: 35 avisados, 2 fora do prazo e 13 em monitoramento, na varredura.
  - Gateway: pico real, mas os 5 clientes já tinham contatado.
  - Sem falso incidente de lote.

## D-025 · Análise prévia ao agente analista (item 8): o que os dados sustentam
- **Proposta avaliada (usuário):** dois agentes.
  - Atendente: resolve o ticket.
  - Analista: cuida dos contatos repetidos. É chamado pelo atendente ao perceber um padrão, ou age sozinho: varre os dados e avisa com `notificar_clientes` (até 300 por dia) quem ainda não reclamou.
- **Confirmado:**
  - **Impressora PRN-02:** defeitos de personalização (borrado, torto, cortado, pixelado, letra faltando, cor errada) em pedidos impressos de **16 a 28/10**. Em 22 de outubro, 10 de 53 pedidos (19%) reclamaram de personalização. A preventiva é a mais antiga (22/09) e o cartucho foi trocado em 28/10.
    - **Público do aviso:** 43 pedidos ainda sem reclamação de personalização, **41 com consentimento**. Confere com os "40 clientes" da proposta.
    - Pedidos impressos depois de 28/10: sem dado suficiente (ainda em trânsito). Não avisar; monitorar.
  - **Expressa Já no Nordeste:** 105 pedidos em trânsito atrasados, 45 reclamaram (19 no Reclame Aqui) e **60 ainda não**. O analista também pode abrir chamado (papel permitido).
  - **Brinde do CD2** (separação de 12 a 25/10): 85 pedidos, 60 reclamaram e **25 ainda não**.
  - Limite `notificar_clientes` = 300 por dia (catálogo). Como o histórico é amostra de 1 em 4, o volume real é cerca de 4 vezes maior e o analista precisa priorizar entre dias.
- **Não confirmado / armadilhas:**
  - **"Mesmo lote com vazamento":** não há incidente. Uma contagem ingênua aponta o LT2608-F (11 conversas sobre vazamento), mas o produto que vazou é outro item do pedido (body mist, vitamina D), não o esfoliante do lote. O lote de `pedidos.csv` é só o do produto principal. **Usar o lote por item (`itens_pedido.lote`) e o produto citado.**
  - **"Reclamou" ≠ "teve contato":** dos 20 contatos de pedidos da PRN-02, 10 eram sobre atraso ou dúvida. Excluir do aviso só quem reclamou **do defeito em questão**.
  - **"Chamado pelo atendente":** não há ferramenta de passagem no catálogo (`acionar_pessoa` não tem área "analista"). A passagem é **orquestração interna** entre os dois agentes, registrada na trilha, sem ferramenta inventada. A correção da causa vai para pessoas: `acionar_pessoa(produção | logística)`.
  - **"Caso da Maria":** a fila do Reclame Aqui não tem ticket da PRN-02. O único é o AT116690 (e-mail). Para a demo no escopo Reclame Aqui, o padrão com tickets na fila é a **Expressa Já no Nordeste** (7 no Reclame Aqui). Para mostrar a PRN-02 é preciso incluir o e-mail.
- **Requisitos para a implementação:** consentimento (`aceita_comunicacao_*`); aviso sem promessa fora da política ("se notar o defeito, responda com foto e resolvemos sem custo"); não avisar quem já foi avisado; janela do incidente definida pelas reclamações, não por uma data fixa; e o analista também saber concluir "não há incidente".

## D-024 · Brinde que consta nos itens do pedido é devido (correção de erro)
- **Erro encontrado:** ao analisar repetições, o AT116614 apareceu no grupo "brinde faltando no CD2". A política tinha **negado publicamente** no Reclame Aqui com "brinde não era devido: R$ 119,70 abaixo do mínimo de R$ 129". Mas o brinde GBT-9001 **estava nos itens do pedido**: a empresa o vendeu e se comprometeu a enviar. O ERP aplicou a campanha pelo valor total (R$ 130,63 com frete). São 28 pedidos nessa situação.
- **Decisão:** se a linha do brinde está em `itens_pedido`, o brinde é devido e segue o fluxo normal de item faltante (foto, prazo, reenvio). As regras da campanha (marca, período, mínimo) só são conferidas quando o pedido **não** tem a linha do brinde.
- **Por que passou:** o teste antigo afirmava a negativa como comportamento esperado. Os testes foram corrigidos e há um teste de regressão com o AT116614.

## D-023 · Documento de prompts gerado a partir do código; Jev sem número de pedido
- **Contexto:** o usuário pediu um arquivo com os prompts de todos os agentes, para validação.
- **Decisão:** `docs/PROMPTS.md` é **gerado** por `python -m src.documentar_prompts` a partir das constantes de `src/llm.py` e `src/agente.py`. Ele traz visão geral (modelo, versão, parâmetros), prompts completos, exemplo real renderizado (AT116524, sem chamar o proxy), regras do validador, onde ficam os registros e uma lista de verificação. `tests/test_prompts_doc.py` falha se o arquivo divergir do código. A mensagem de reescrita virou a constante `MENSAGEM_REESCRITA`.
- **Descartado:** escrever o documento à mão. Ele ficaria desatualizado na primeira mudança de prompt, e quem valida estaria validando outra coisa.
- **Achado ao gerar:** o estado enviado ao Jev levava o número completo do pedido. Ele não ajuda a classificar, então agora o Jev recebe o texto com `mascarar_publico` (minimização de dados). Conferido ao vivo: 23/24 continuam iguais à classificação manual.

## D-022 · LLM como juiz e registro de cada validação (pedido do usuário)
- **Contexto:** o validador determinístico pega regras duras (dado pessoal, valor, prazo, transportadora, promessa de dinheiro). Ele não pega *afirmação inventada* em linguagem natural ("já está em produção" quando os fatos dizem "aguardando expedição") nem *compromisso inventado* ("vamos te avisar"). O usuário pediu LLM como juiz e registros da validação.
- **Decisão:**
  - **Juiz de outra família:** `gpt-6-astra`, com versão fixa, enquanto o redator é `glm-5.3`. Assim o modelo não avalia o próprio texto. Testado contra `mimo-v2.6-flash`: o gpt respondeu em 4,2 s com JSON limpo, o mimo em 10,8 s.
  - **Cinco critérios**, nota de 1 a 5 com justificativa cada: fidelidade, veracidade, segurança, empatia, clareza.
  - **Quem aprova é o código:** fidelidade, veracidade e segurança ≥ 4; empatia e clareza ≥ 3. O juiz não tem voto próprio.
  - **Ordem:** redator → validador → juiz (só para texto que passou no validador) → se reprovado, reescrita com a crítica de quem reprovou. São até 3 tentativas; depois disso, pessoa.
  - **Juiz fora do ar** → vale o validador e o texto é publicado, com o erro registrado. Motivo: o juiz é camada extra de qualidade, não de segurança. As regras duras já passaram, e a preferência do usuário é automatizar.
  - **Registro:** uma linha por tentativa em `validacoes.csv` (texto, parecer do validador, notas, critérios reprovados, justificativas, problemas e sugestão do juiz, resultado). O dossiê mostra o último parecer, e a trilha do `responder_cliente` cita o juiz.
- **Calibração com execuções reais (Reclame Aqui):**
  - `juiz-v1` reprovou a 1ª versão de todos os textos; todos passaram só na 3ª. Parte das críticas era válida, como "em produção" inventado. Parte contradizia as nossas regras: penalizava a *falta* de prazo (que o D-021 proíbe) e o pedido *mascarado*.
  - `juiz-v2` recebeu essas regras no prompt e as críticas ficaram consistentes. O padrão restante era do redator: acrescentar "vamos te avisar" ou "acompanhar".
  - `resposta-v2` proíbe compromissos fora das AÇÕES e manda usar as palavras dos FATOS. Resultado: todos aprovados na 2ª tentativa, com 23 de 24 automáticos mantidos.
- **Custo:** a validação em duas camadas elevou o chat de cerca de 4,6 mil para cerca de 19 mil tokens por execução da fila (o proxy não informa preço do chat). O Jev ficou igual, em US$ 0,0012.

## D-021 · Validador reprova prazo inventado (decisão do usuário)
- **Contexto:** num rascunho real (AT116521), o glm escreveu "em até 1 dia útil entraremos em contato". Ninguém decidiu esse prazo, e publicado no Reclame Aqui ele vira promessa pública que pode ser quebrada.
- **Decisão:** `validar_resposta` reprova:
  - número + unidade de tempo ("1 dia útil", "48h", "dois dias úteis", "2 semanas") e datas dd/mm que **não aparecem nos fatos do caso nem no texto da regra da política**. As datas ISO dos fatos (2026-11-04) são convertidas para 04/11 antes de comparar;
  - promessa vaga de dia ("amanhã", "até sexta", "ainda hoje", "nesta semana") sempre.
  - O prompt também passou a proibir prazo de retorno: o modelo deve usar "assim que tivermos novidades".
- **Aceito:** "dentro do prazo de 7 dias para devolução" (da regra da política) e "previsão até 04/11" (do rastreio).
- **Consequência:** sem aumento de trabalho humano. A reprovação volta ao glm com o motivo e ele reescreve (D-020). Na execução real seguinte, nenhum texto precisou ser reescrito por prazo e o resultado ficou em 23 de 24 automáticos.
- **Limite:** número por extenso só é reconhecido até "dez", "quinze" e "trinta". "Um dia" em sentido figurado ("um dia especial") seria reprovado, mas o erro é no lado seguro.

## D-020 · Automação total quando errar é barato (decisão do usuário)
- **Contexto:** na execução real, 4 dos 5 tickets retidos eram dúvida do Jev entre "atraso" e "onde está o pedido". Nas duas categorias a ação é a mesma (informar status). Reter só gerava fila humana. O usuário pediu: "quando o custo de errar não é tão alto, seguir com a automação total; o que for possível automatizar, automatizar".
- **Decisão 1 (confiança baixa):** com confiança < 0,7, o agente roda política e decisão para cada categoria provável (probabilidade ≥ 15%, até 3). São só leituras; nada é executado nesse teste.
  - Se **todas** levam só a ações sem dinheiro e reversíveis (`informar_status`, `solicitar_foto`, `abrir_chamado_transportadora`, `responder_duvida`, `agradecer`, `validar_cupom`, `responder_cliente`), segue com a mais provável. O porquê vai para a trilha de cada ação e para o dossiê.
  - Se alguma levaria a dinheiro, negativa ou escalação → pessoa. Exemplo testado: dúvida entre "onde está" e "arrependimento" (que seria reembolso).
- **Decisão 2 (autocorreção):** texto reprovado pelo validador volta ao glm com o motivo exato e é reescrito uma vez. Só uma segunda reprovação leva a uma pessoa.
- **Continua com humano** (errar é caro): caso de saúde, ameaça jurídica, suspeita de fraude, dinheiro acima da alçada e freios de cliente novo.
- **Resultado real (Reclame Aqui):** de 19 → **23 de 24 respondidos automaticamente**. O único retido é a reação adversa (AT116568). Custo da execução: US$ 0,0012 no Jev e cerca de 4,4 mil tokens de chat.

## D-019 · Loop do agente: quem escreve a resposta, quando reter, como priorizar
- **Fluxo por execução:** classificar todos os tickets dos `canais` (Jev) → priorizar → para cada ticket: `classificar_conversa` → fatos → política → veracidade → decisão → executar passos → responder ou reter → dossiê.
- **Quem escreve:** ferramenta de mensagem escolhida pela decisão (`solicitar_foto`, `informar_status`, `negar_por_politica`, `orientar_suspensao_uso`, `agradecer`) → vale o texto fixo dela. Ele é seguro, já mascarado e deixa estado (ex.: "aguardando foto"). O LLM escreve só quando a decisão pede texto livre ou quando nenhuma mensagem saiu (reenvio executado, aprovação pedida, escalação).
  - Descartado: LLM em toda resposta. Mandaria duas mensagens ao cliente ou perderia o registro de estado das ferramentas.
- **Reter** (nada é publicado; o rascunho vai para a supervisão no canal do time) quando:
  - caso sensível ou ameaça externa (probabilidade > 0,5 no Jev);
  - suspeita sinalizada;
  - confiança < 0,7;
  - validador reprovou;
  - IA falhou ou foi desligada.
- **Não executar** passo com parâmetro que o código não tem (aprovação pendente, novo endereço, código de cupom). Ele fica como "pendente" no dossiê.
- **Prioridade:** soma explícita de pontos (`PESOS` em `agente.py`):
  - SLA: vencido +50; senão até +40, proporcional ao tempo consumido;
  - custo do canal, LTV, cliente novo, irritação, ameaça, `pendente`, réplicas no Reclame Aqui;
  - **caso sensível** +30 e passa a usar o prazo regulatório de 24h se for mais curto que o SLA. Foi assim que a reação adversa (AT116568, 3h restantes) foi para o topo; antes ela aparecia com as 27h do SLA do Reclame Aqui.
- **Validador, ajustes feitos com rascunhos reais:**
  - Pode citar a reposição que **já existe** (`em_andamento`). Antes, o AT116524 era falso positivo.
  - Pode citar ação **pendente de aprovação**, mas nunca como feita ("reembolsamos", "o reembolso foi processado"). Antes, o AT116520 era falso positivo.
  - O glm recebe a transportadora real nos fatos: inventou "Correios" em 2 rascunhos, ambos reprovados.
- **Execução real (Reclame Aqui, 24 tickets):** 19 respondidos (17 por ferramenta, 2 por LLM) e 5 retidos (1 reação adversa, 4 por confiança < 0,7 entre "atraso" e "onde está"). Custo: US$ 0,0012 no Jev e cerca de 6,8 mil tokens de chat. Nenhuma ação falhou.
- **Limites conhecidos:**
  - O validador não detecta **prazo inventado** (ex.: "em até 1 dia útil entraremos em contato", visto num rascunho retido).
  - Os 4 retidos por confiança baixa são ambiguidade entre duas categorias que a política trata igual. Relaxar essa regra é decisão de negócio, não foi feito.

## D-018 · IA só classifica e redige; validador determinístico decide se publica
- **Jev classifica** nas categorias da **política** (15 + "outros"), não nas 13 tags. A tag oficial sai de `tag_da_categoria`.
  - Na mesma chamada vêm urgência, irritação, ameaça externa, caso sensível, "falta brinde" e preferência (reembolso ou reenvio). Irritação serve só para prioridade e estilo.
  - Conversa sem mensagem do cliente vira `sem_interacao` por regra, sem chamar o Jev.
  - Confiança < 0,7 ou "outros" → revisão humana.
  - Medido nos 24 tickets do Reclame Aqui: **23/24 iguais** à classificação manual. A diferença (AT116521, onde está × atraso) não muda a ação, porque a política redireciona uma para a outra. 3 tickets foram para revisão por confiança baixa. Custo: US$ 0,0012 no total.
- **glm-5.3 redige** a partir da decisão: intenção, ações e fatos permitidos.
  - O texto do cliente vai em `<mensagem_cliente>`, mascarado, com ordem de não seguir instruções.
  - A saída é JSON `{resposta, justificativa}`. A justificativa vai para a trilha.
  - Ajuste medido: com `max_tokens=2000` e `reasoning_effort="low"`, 2 de 3 chamadas gastaram tudo raciocinando e não escreveram nada (a falha segura impediu a publicação). Com `"minimal"` e 4000, foram 3 de 3 em cerca de 4,5 s e cerca de 840 tokens cada.
- **Validador de saída** (determinístico) reprova:
  - dado pessoal, número de pedido ou rastreio (canal público);
  - termo de acusação;
  - cupom ou compensação;
  - promessa de ferramenta de dinheiro que a decisão não executa nem oferece;
  - valor em R$ fora da decisão;
  - transportadora diferente da do pedido.
  Essa última regra entrou porque o glm escreveu "expedição dos correios" num pedido da Expressa Já.
  Reprovou → não publica (o `agente.py` manda para uma pessoa).
- **Falha segura:** erro de rede, 4xx/5xx, 429 esgotado (espera `x-ratelimit-reset`, até 3 tentativas), JSON inválido ou corte por limite de tokens → resultado com `erro`, nunca um chute. Modelos `deepseek-*` são recusados no próprio cliente.
- **Limite conhecido:** o validador de promessas é por palavra-chave e pode reprovar texto que só *menciona* a palavra (ex.: "não foi possível a troca"). O erro é no sentido seguro, porque leva a revisão humana.

## D-017 · Decisão por custo esperado: fórmulas e escolhas de negócio
- **Ordem:**
  1. A política manda.
  2. Dinheiro com prova nos sistemas (duplicidade no gateway, atraso no rastreio, arrependimento previsto no CDC) segue a política.
  3. Só dinheiro **baseado em foto** (avaria, item faltante, personalização) passa por veracidade, freios de novo e custo.
- **LTV:** o `parametros.md` diz "recompra 28%/ano (70% acima de R$ 1.500)", sem fórmula. Escolha: receita futura esperada = `receita_acumulada × r/(1−r)`, ou seja, o cliente repete o histórico com probabilidade r a cada ano (série geométrica). Fiel mediano (R$ 620) → LTV R$ 241; VIP (R$ 1.909, r = 70%) → R$ 4.454.
- **Novo:** `ltv = max(histórico, ticket × RECOMPRA_ESPERADA_NOVO)`, com RECOMPRA_ESPERADA_NOVO = 2. Um novato bem atendido tende a virar recorrente (mediana de 3 pedidos).
- **Custos:**
  - executar = (1−p) × valor × 0,45;
  - supervisor = R$ 6 + p × 10% × custo de negar (FRACAO_ESPERA = 0,10: o custo de até 24h de espera, escolha explícita);
  - negar = p × (½ LTV + 0,30 × R$ 42). Aparece só nas alternativas da trilha: o agente **nunca nega por suspeita** (política).
- **Regras que vêm antes do custo:**
  - Sinal com LR ≥ 8 ou da lista "sempre sinalizar" (foto reaproveitada, etiqueta de outro pedido, identidade compartilhada, suspeita improcedente anterior) → `sinalizar_suspeita`.
  - Cliente novo vai para `solicitar_aprovacao` (sem suspeita) se o valor passar de R$ 150, se já teve resolução automática ou se não há evidência positiva na foto.
- **Supervisão pelo custo:** quando o custo manda levar à supervisão, com sinais contra → `sinalizar_suspeita` (há evidência verificável). Sem sinais → `solicitar_aprovacao` (sinalizar exige evidência, conforme a política).
- **Hoje:** nenhum dos 24 tickets do Reclame Aqui chega ao cálculo de custo. Todos caem antes numa regra da política (pedir foto, status, em andamento, escalar). O cálculo passa a valer quando as fotos chegarem durante a semana.

## D-016 · Nunca pagar duas vezes pelo mesmo problema; aprovação também abaixo da alçada
- **Contexto:** 6 tickets da fila (1 no Reclame Aqui: AT116524) já têm reenvio ou nova produção registrados para o mesmo pedido, abertos em contato anterior. A política mandava repor de novo.
- **Decisão (política):** em avaria, item faltante e personalização, antes de olhar prazo e foto, `politicas.py` procura reposição do mesmo tipo para o pedido:
  - em andamento → situação `em_andamento`: informar o andamento, sem dinheiro;
  - já entregue e o cliente reclama de novo → `escalar` para a supervisão.
- **Decisão (ferramenta):** `solicitar_aprovacao` passa a aceitar valores **abaixo** da alçada. Os freios internos do agente (teto e segunda resolução de cliente novo, no CLAUDE.md) são mais rígidos que a alçada do papel e precisam de uma pessoa sem tratar o caso como suspeita. A mensagem no canal do time indica "freio interno do agente".
- **Descartado:** usar `sinalizar_suspeita` para os freios de novos. Isso marcaria como suspeito um cliente sem nenhuma evidência contra ele, contrariando o CLAUDE.md ("rigor baseado em evidência, não em desconfiança").

## D-015 · Modo público: mensagens do Reclame Aqui saem sem pedido completo e sem rastreio
- **Contexto:** a resposta no Reclame Aqui é pública (CLAUDE.md, "Escopo do MVP"). Algumas ferramentas mandavam texto pronto com o número do pedido e o código de rastreio, que ficariam expostos na internet.
- **Decisão:** toda mensagem ao cliente passa por `_mensagem` em `acoes.py`, que aplica o filtro do canal.
  - Canal público (`CANAIS_PUBLICOS = {"reclame_aqui"}`): `mascarar_publico` = PII + pedido mascarado (`GBH***217`) + código de rastreio removido (formatos dos dados: EJ/CA…BR, VL, RPS, TN).
  - Canal privado: só `mascarar_pii`.
  - Textos que mudam no canal público:
    - `informar_status` não inclui o código de rastreio;
    - `solicitar_foto` avisa que a foto será pedida pelo canal privado, porque a foto pode mostrar etiqueta e endereço;
    - `decidir_suspeita` (improcedente) termina com "Seguimos à disposição pelo canal privado."
  - Cada mensagem registra `publica: true/false` no estado.
- **Descartado:** confiar só no validador do LLM (item 5 da agenda). Os textos fixos das ferramentas não passam pelo LLM e vazariam do mesmo jeito.
- **Consequência:** o validador de saída continua necessário para o texto livre do LLM. A máscara é a rede de segurança de último nível, porque se aplica a qualquer texto, inclusive ao do LLM.

## D-014 · Detecção de grupo: CPF, telefone ou e-mail sim; CEP sozinho não
- **Contexto:** o CLAUDE.md pede para detectar grupos de clientes (sobretudo novos) com o mesmo endereço, telefone ou foto.
- **Dados:** nenhum CPF, telefone ou e-mail é compartilhado entre clientes, mas **22 pares de clientes dividem CEP** por coincidência.
- **Decisão:**
  - `detectar_grupo` usa CPF, telefone e e-mail (LR 15). CEP fica de fora, porque daria 22 falsos positivos.
  - Foto compartilhada já é coberta pelo `hash_perceptual`.
  - A função lê o CRM direto (análise interna), mas devolve só o campo coincidente e o `customer_id`, nunca o valor.
- **Consequência:** sem grupos no histórico. A função está pronta para os clientes que chegarem na semana.

## D-013 · Sinais e pesos medidos nos dados
- **Taxa-base:** 17 de 280 conversas com foto (6,1%) usaram foto que aparece em outro cliente → `PRIOR_FALSA = 0,07`.
- **Calibração** (17 fotos reaproveitadas × 365 únicas):

| Sinal | Nas reaproveitadas | Nas únicas | LR usado (com teto) |
|---|---|---|---|
| Hash igual ao de outro cliente | 17/17 | 0/365 | 40 |
| EXIF antes da entrega | 11/17 | 0/365 | 30 |
| Descrição genérica (não identifica o item) | 16/17 | 1/365 | 20 |

- **Ajustes a partir dos dados:**
  - Software de edição só aparece em fotos legítimas ("Editor de Fotos do Galaxy", "Fotos do iOS"). Fica com LR 1,2: o CLAUDE.md o cita como sinal, mas os dados não o sustentam.
  - **Falha sistêmica** (LR 0,5, a favor do cliente) só conta quando a origem comum faz sentido para a categoria: impressora em personalização, lote em avaria. Item faltante é erro de separação, e lote ou máquina não o explicam.
  - **Reincidência** conta só fotos de **outros pedidos**. As do mesmo pedido já entram no grupo "foto"; contá-las de novo seria contagem dupla.
- **Resultado no histórico:** conversas com foto reaproveitada ficam com p_verdadeira mediana de 0,06 (máx. 0,41); as demais, 0,96. As duas "legítimas" com p < 0,5 (AT108490, AT111960) têm foto reaproveitada em conversa anterior do mesmo pedido, então estão corretas.

## D-012 · Veracidade por razões de verossimilhança, com grupos de sinais correlacionados
- **Decisão:** `p_verdadeira` parte da taxa-base e soma o log dos LRs de cada sinal. Dentro de um grupo (`foto`, `historico`, `contexto`), conta inteiro o sinal mais forte de cada lado (contra ou a favor) e os demais entram com peso 0,25. Cada `Sinal` traz uma evidência verificável (ids de anexo, datas, pedidos), pronta para a trilha e para `sinalizar_suspeita`.
- **Descartado:**
  - Somar todos os LRs ("naive Bayes"): a foto da internet traz três sinais juntos e derrubaria p a quase zero.
  - Usar o LLM para estimar p: não é auditável.
- **Fora de propósito:** tom da mensagem e segmento do cliente não são sinais (CLAUDE.md). Atraso e cobrança duplicada ignoram fotos: a prova está no rastreio e no gateway.
- **Consequência:** evidência mista fica na zona de dúvida (ex.: AT109160, p = 0,59), e quem decide o que fazer com ela é o `decisao.py`.

## D-011 · Casos sem regra explícita vão para uma pessoa, não para um palpite
- **Decisão:**
  - "Atraso" em pedido que a transportadora marca como entregue vira `escalar` para a logística (entrega contestada).
  - Pagamento confirmado no gateway mas pendente no ERP vira `escalar` para a supervisão.
  - Prazo vencido com pedido ainda não separado vira `escalar` para a logística.
- **Consequência:** a política nunca inventa uma saída que `politicas.csv` não prevê. Quando a regra não cobre o caso, o agente levanta a mão.

## D-010 · Reação adversa é sempre escalada; urgência crítica a partir de 75% do prazo
- **Contexto:** o atendente pode orientar a suspensão do uso, mas `registrar_cosmetovigilancia` é exclusiva do supervisor e tem prazo de 24h desde o relato.
- **Decisão:** a situação é sempre `escalar`, com os passos `orientar_suspensao_uso` → `acionar_pessoa(supervisão)`. A mão levantada já traz sku, lote e fotos para o registro. A urgência fica `critica` a partir de 18h (75% de 24h). `trocar_produto` aparece só como passo opcional.
- **Consequência:** a troca nunca substitui o registro.

## D-009 · Valor de referência e validade das fotos
- **Decisão:**
  - Reenvio ou reimpressão do pedido inteiro usa `valor_produtos` (sem frete).
  - Reembolso por atraso ou arrependimento usa `valor` (o que o cliente pagou).
  - Item específico usa o `valor_total` da linha em `itens_pedido`.
  - Brinde (preço zero) usa o `custo_unitario` do produto (R$ 24,90).
  - Só valem fotos enviadas **depois** da entrega.
- **Consequência:** a alçada é comparada com o custo real da ação. Uma foto de antes da entrega não pode provar uma avaria.

## D-008 · Prazo "do recebimento" conta até o PRIMEIRO contato sobre o pedido
- **Contexto:** muitos clientes já reclamaram antes ("Pela 2ª vez...") e não tiveram resposta.
- **Decisão:** os dias desde a entrega são contados até o primeiro contato do cliente sobre aquele pedido, em qualquer conversa (histórico ou fila), e não até o "agora".
- **Descartado:** contar até o momento do atendimento. Isso puniria o cliente pela demora da própria empresa.
- **Consequência:** a trilha mostra a data da entrega e a do primeiro relato. Exemplo: AT116524 conta a partir de 29/10 (AT116157), não de 31/10.

## D-007 · `politicas.py` avalia e sugere; não executa
- **Decisão:** `avaliar(categoria, fatos)` devolve uma `Avaliacao` com:
  - `situacao`: `elegivel`, `nao_elegivel`, `falta_informacao` ou `escalar`;
  - `passos`: ferramentas e parâmetros, já com `solicitar_aprovacao` quando o valor passa da alçada;
  - `alternativas`: por exemplo, reenvio ou reembolso, à escolha do cliente;
  - `fatos`, `pendencias` e `trilha()`.
  `coletar_fatos` usa só as ferramentas de leitura, então tudo fica no log.
- **Números:** vêm de `politicas.csv`. Os três limites que só existem no texto da regra (rastreio parado há 3 dias úteis, Pix em 30 minutos, boleto em 2 dias úteis) são extraídos por regex. Se o texto mudar, o import falha de propósito.
- **Consequência:** a política é testável sem LLM e sem efeito colateral. O `decisao.py` combina a `Avaliacao` com a veracidade e o custo esperado antes de executar.

## D-006 · Trilha estruturada no lugar do "chain of thought" do modelo
- **Contexto:** precisamos que um auditor (ou quem assumir o projeto) entenda por que cada ação foi tomada.
- **Decisão:** toda ferramenta que altera estado exige `trilha={"por_que": ...}` e é recusada sem ela. A trilha aceita `regra`, `evidencias`, `alternativas` (com custo esperado), `p_verdadeira`, `custo_esperado` e `origem`. O raciocínio oculto do modelo (`reasoning_content`) não é usado.
- **Descartado:** guardar o `reasoning_content` do LLM. Ele não é estável entre chamadas, não dá para conferir contra os dados e pode repetir texto do cliente (inclusive dados pessoais).
- **Consequência:** a trilha é montada pelo código, a partir de fatos e regras. LLM e Jev só entram com uma `justificativa` curta pedida na saída e a `confidence` deles.

## D-005 · Ferramentas aplicam só as travas do catálogo; a política fica fora
- **Contexto:** dava para pôr nas ferramentas tanto as regras de negócio (7 dias, exige foto) quanto os limites do sistema.
- **Decisão:** `acoes.py` aplica só o que está em `ferramentas.csv`: a alçada (`valor_max_sem_aprovacao_rs`), `somente_antes_da_separacao`, 1 troca por pedido, supervisor não aprova a própria ação, `nao_decide_o_que_ele_mesmo_sinalizou` e `max_por_dia`. Prazos, exigência de foto e atraso em dias úteis ficam em `politicas.py`.
- **Descartado:** ferramentas que recusam tudo o que fere a política. Isso esconderia a decisão dentro do "sistema" e não deixaria testar a política isoladamente.
- **Consequência:** a ferramenta aceitar a chamada ≠ a política permitir. O agente precisa consultar `politicas.py` antes de agir.

## D-004 · Travas extras de segurança nas ferramentas
- **Decisão:**
  - o pedido precisa ser do cliente da conversa em atendimento (contra prompt injection e engano de pedido);
  - reenvio, troca, reembolso e aprovação ficam bloqueados enquanto o pedido tiver suspeita pendente ou improcedente;
  - estorno nunca passa do total efetivamente cobrado;
  - `negar_por_politica` só aceita regra que existe em `politicas.csv`;
  - aprovação vale só para a ação, o pedido e o valor aprovados, e uma vez só.
- **Consequência:** um erro do LLM ou do código de decisão vira erro explícito no log, não dinheiro perdido.

## D-003 · Estado compartilhado e "cadeiras" por papel
- **Contexto:** o supervisor aprova o que o atendente pediu e não pode aprovar a própria ação.
- **Decisão:** um `Estado` por execução (relógio, log, ações, aprovações...). Cada `Ambiente` é uma cadeira (`papel` + `ator`) sobre esse estado, criada com `amb.como("supervisor", "supervisor_01")`. A regra "não aprova a própria ação" compara o `ator`.
- **Consequência:** a mesma execução simula o agente e as pessoas que ele aciona. O agente nunca usa o papel de supervisor por conta própria: isso é coisa de teste e de demo.

## D-002 · IDs do agente com sufixo `AG`
- **Decisão:** registros criados na execução usam `AC-AG000001`, `OMS-AG00001`, `APR-AG00001` etc.
- **Consequência:** nunca colidem com os IDs históricos dos CSVs, e fica claro o que foi feito pelo agente.

## D-001 · CSVs são a foto de 02/11 08:00 e nunca são alterados
- **Decisão:** os dados são só leitura. O que muda (cancelamento, endereço) fica em `Estado.pedidos` e é aplicado por `pedido_na_posicao`. Campos com data posterior ao tique atual são escondidos.
- **Consequência:** dá para rodar várias vezes do zero, e os testes ficam determinísticos.

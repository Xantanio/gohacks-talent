# Registro de decisões de projeto

Uma entrada por decisão, da mais recente para a mais antiga. Formato: **contexto → decisão → alternativas descartadas → consequência**.
Decisões tomadas *em cada ticket* não ficam aqui: ficam na trilha de cada ação (`saida/acoes_executadas.csv`, coluna `trilha`) e no dossiê do ticket.

---

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

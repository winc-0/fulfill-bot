# 知服 ZhiFu 会话级评测

- 时间：2026-10-20 14:55
- 脚本：`eval/sessions.jsonl`，30 段多轮会话
- 规划模型：`qwen-flash`；对照变体关闭模型、走规则规划器
- 每段会话前重置对话状态与中台写表，互不污染

## 总表

| 指标 | llm | rule |
|---|---|---|
| 会话完成率 | 80.0% | 66.7% |
| 轨道路由准确率 | 94.1% | 80.4% |
| 平均每轮耗时(s) | 1.5 | 0.4 |
| 误转人工 | 1/28 | 0/28 |

## 分组完成率

| 分组 | llm 完成率 | rule 完成率 |
|---|---|---|
| 业务失败 | 3/3 | 3/3 |
| 中途取消 | 2/2 | 2/2 |
| 澄清兜底 | 2/3 | 1/3 |
| 指代消解 | 0/3 | 1/3 |
| 转人工 | 2/2 | 2/2 |
| 中断恢复 | 1/2 | 1/2 |
| 对象卡片 | 2/2 | 2/2 |
| 政策问答 | 5/5 | 0/5 |
| 单轮查询 | 5/5 | 5/5 |
| 多轮补槽 | 2/3 | 3/3 |

## llm 变体的失败明细

### s06（slot_filling）

- turn1 [start/refund_request conf=0.95 src=llm] 我要申请退款
  - 答：可以，请告诉我需要退款的订单号。
- turn2 [start/refund_request conf=0.95 src=llm] A20260410001
  - 答：请简单说一下退款原因，方便审核。
- turn3 [start/refund_request conf=0.95 src=llm] 不想要了
  - 答：订单 A2026041001 不存在。

  - ✗ final: 回复里没有「退款单号」
  - ✗ final: 槽位 order_number 期望 A20260410001，实际 A2026041001

### s09（coreference）

- turn1 [start/order_status_query conf=0.95 src=llm] 查一下 A20260408002
  - 答：订单 A20260408002 当前状态是「运输中」。共 1 件商品，金额 149.00 元，下单时间 2026-04-08。包裹已发出，正在配送途中。
- turn2 [answer/- conf=0.95 src=llm] 那这个能退吗
  - 答：得看具体商品和情况哈～比如是不是定制类、贴身衣物，或者已经拆封、激活过。签收没超过7天，商品没用过、包装配件都在的话，一般可以无理由退。要是有质量问题或发错货，
- turn3 [start/refund_request conf=0.95 src=llm] 直接帮我申请退款
  - 答：请简单说一下退款原因，方便审核。

  - ✗ final: 回复里没有「退款单号」

### s10（coreference）

- turn1 [start/logistics_tracking conf=0.95 src=llm] 帮我查下 A20260408002 的物流
  - 答：订单 A20260408002 由顺丰速运配送，运单号 SF1234567890。当前进度：运输中（最新：快件已到达上海转运中心）。
- turn2 [answer/- conf=0.95 src=llm] 运单号是多少
  - 答：抱歉我没太听明白，你是想查订单、查物流、申请退款，还是咨询商品？

  - ✗ turn2: 期望 action=['start']，实际 answer
  - ✗ turn2: 期望 flow=['logistics_tracking']，实际 

### s11（coreference）

- turn1 [start/refund_request conf=0.95 src=llm] 我要退款
  - 答：可以，请告诉我需要退款的订单号。
- turn2 [start/refund_request conf=0.95 src=llm] A20260409004
  - 答：请简单说一下退款原因，方便审核。
- turn3 [cancel/refund_request conf=0.95 src=llm] 算了先不退了
  - 答：好的，退款申请就不办了。如果之后还想处理，跟我说一声就行。
- turn4 [clarify/- conf=0.00 src=llm] 继续刚才的退款
  - 答：抱歉没太明白，您是想查订单、查物流、申请退款，还是咨询商品？

  - ✗ turn4: 期望 action=['resume']，实际 clarify
  - ✗ turn4: 期望 flow=['refund_request']，实际 

### s12（interrupt_resume）

- turn1 [start/refund_request conf=0.95 src=llm] 我要退款
  - 答：可以，请告诉我需要退款的订单号。
- turn2 [start/logistics_tracking conf=0.95 src=llm] 先查一下 A20260408002 的物流
  - 答：退款申请我先帮你放一放，先处理物流查询。
  - 答：订单 A20260408002 由顺丰速运配送，运单号 SF1234567890。当前进度：运输中（最新：快件已到达上海转运中心）。
- turn3 [resume/refund_request conf=0.95 src=llm] 继续刚才的退款
  - 答：好的，我们接着刚才的退款申请继续说。
  - 答：请简单说一下退款原因，方便审核。
- turn4 [start/refund_request conf=0.95 src=llm] 不想要了
  - 答：已为订单 A20260408002 提交退款申请，退款单号 R202610201454199D9B3D，原因：不想要了。审核结果会通过短信通知你。

  - ✗ turn2: 期望 refund_request 在挂起列表，实际没有

### s27（clarify）

- turn1 [handoff/human_handoff conf=0.95 src=llm] 帮我订一张去上海的高铁票
  - 答：好的，我这就帮你转接人工客服，同时把前面的对话摘要一并转过去，你不用再重复一遍。

  - ✗ final: 回复里没有「明白」
  - ✗ final: 回复里没有「支持」
  - ✗ final: 回复里没有「接不了」

## rule 变体的失败明细

### s09（coreference）

- turn1 [clarify/- conf=0.30 src=rule] 查一下 A20260408002
  - 答：抱歉我没太听明白。你想查订单、查物流、申请退款，还是咨询商品？
- turn2 [clarify/- conf=0.30 src=rule] 那这个能退吗
  - 答：抱歉我没太听明白。你想查订单、查物流、申请退款，还是咨询商品？
- turn3 [start/refund_request conf=0.70 src=rule] 直接帮我申请退款
  - 答：可以，请告诉我需要退款的订单号。

  - ✗ turn1: 期望 action=['start']，实际 clarify
  - ✗ turn2: 期望 action=['answer']，实际 clarify
  - ✗ final: 回复里没有「退款单号」
  - ✗ final: 槽位 order_number 期望 A20260408002，实际 None

### s11（coreference）

- turn1 [start/refund_request conf=0.70 src=rule] 我要退款
  - 答：可以，请告诉我需要退款的订单号。
- turn2 [start/refund_request conf=0.60 src=rule] A20260409004
  - 答：请简单说一下退款原因，方便审核。
- turn3 [cancel/- conf=0.80 src=rule] 算了先不退了
  - 答：好的，退款申请就不办了。如果之后还想处理，跟我说一声就行。
- turn4 [start/refund_request conf=0.70 src=rule] 继续刚才的退款
  - 答：请简单说一下退款原因，方便审核。

  - ✗ turn4: 期望 action=['resume']，实际 start

### s12（interrupt_resume）

- turn1 [start/refund_request conf=0.70 src=rule] 我要退款
  - 答：可以，请告诉我需要退款的订单号。
- turn2 [start/logistics_tracking conf=0.70 src=rule] 先查一下 A20260408002 的物流
  - 答：退款申请我先帮你放一放，先处理物流查询。
  - 答：订单 A20260408002 由顺丰速运配送，运单号 SF1234567890。当前进度：运输中（最新：快件已到达上海转运中心）。
- turn3 [resume/refund_request conf=0.70 src=rule] 继续刚才的退款
  - 答：好的，我们接着刚才的退款申请继续说。
  - 答：请简单说一下退款原因，方便审核。
- turn4 [start/refund_request conf=0.60 src=rule] 不想要了
  - 答：已为订单 A20260408002 提交退款申请，退款单号 R202610201455078350E8，原因：不想要了。审核结果会通过短信通知你。

  - ✗ turn2: 期望 refund_request 在挂起列表，实际没有

### s19（policy）

- turn1 [start/refund_request conf=0.70 src=rule] 退货的运费谁出
  - 答：可以，请告诉我需要退款的订单号。

  - ✗ turn1: 期望 action=['answer']，实际 start
  - ✗ final: 回复里没有「运费」

### s20（policy）

- turn1 [start/refund_request conf=0.70 src=rule] 退款多久能到账
  - 答：可以，请告诉我需要退款的订单号。

  - ✗ turn1: 期望 action=['answer']，实际 start
  - ✗ final: 回复里没有「工作日」

### s21（policy）

- turn1 [clarify/- conf=0.30 src=rule] 可以开发票吗
  - 答：抱歉我没太听明白。你想查订单、查物流、申请退款，还是咨询商品？

  - ✗ turn1: 期望 action=['answer']，实际 clarify
  - ✗ final: 回复里没有「发票」

### s22（policy）

- turn1 [clarify/- conf=0.30 src=rule] 下单之后多久发货
  - 答：抱歉我没太听明白。你想查订单、查物流、申请退款，还是咨询商品？

  - ✗ turn1: 期望 action=['answer']，实际 clarify
  - ✗ final: 回复里没有「小时」

### s23（policy）

- turn1 [start/product_inquiry conf=0.70 src=rule] 商品有质量问题怎么处理
  - 答：你想咨询哪个商品？告诉我商品号，或者直接从右侧商品列表点一个。

  - ✗ turn1: 期望 action=['answer']，实际 start
  - ✗ final: 回复里没有「维修」
  - ✗ final: 回复里没有「换货」

### s26（clarify）

- turn1 [start/order_status_query conf=0.70 src=rule] 今天天气怎么样
  - 答：请告诉我订单号，我来帮你查。

  - ✗ turn1: 期望 action=['chitchat', 'clarify']，实际 start
  - ✗ final: 回复里不该出现「订单号」却出现了

### s27（clarify）

- turn1 [clarify/- conf=0.30 src=rule] 帮我订一张去上海的高铁票
  - 答：抱歉我没太听明白。你想查订单、查物流、申请退款，还是咨询商品？

  - ✗ final: 回复里没有「转」
  - ✗ final: 回复里没有「支持」
  - ✗ final: 回复里没有「接不了」

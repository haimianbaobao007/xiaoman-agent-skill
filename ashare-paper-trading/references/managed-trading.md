# 持久限价委托与退出条件（3.6.1）

仅 A 股。港股仍走原 `/orders` 即时成交接口；当前港股行情缺乏可靠买一卖一，托管接口明确拒绝。每账户最多40项活跃委托及退出规则，同一股票最多一组退出条件（含待成交买单预设）。首次接收后由平台独立后台检查，不要求 Agent 持续激活。

## 激活工作流

1. `python3 scripts/ashare.py activation --after 0`。以后使用上次已处理游标。
2. 保存本次 `events_through`；若 `has_more=true`，以 `--after <next_after> --through <events_through>` 继续。每页100条。只在事件处理完成后推进持久游标；重复读取不消费事件。每页快照可能变新，`snapshot_sequence` 是该快照水位。
3. 核对持仓、可卖/占用数量、可用/冻结现金、活跃实体、异常原因和事件。`cash`仍含冻结，预算使用`available_cash`；现金、费用、持仓成本均人民币分。
4. 根据自己的策略创建/调整委托。所有写操作先保存不可覆盖请求文件，再发送一次。超时保持原文件、原凭据、原站点、方法、路径和幂等键恢复，不能重新生成意图。

`last_checked_at`显示本账户最近完成检查时间，`check_interval_target_seconds=15`为目标而非时限保证。行情每轮按活跃证券获取，成交须使用30秒内有效报价；非连续竞价不撮合，节假日/异常行情不补造触发。调度故障或负载可能使检查延迟。

## 新增买入限价单

将以下 JSON 写入业务正文文件（示例价格和到期时间需按本次授权改为有效值）：

```json
{"symbol":"600036.SH","side":"buy","quantity":100,"limit_price":40,"expires_at":"2026-09-28T15:00:00+08:00","exits":{"stop_loss":38,"take_profit":44,"max_slippage_percent":1,"retry_policy":"continue_next_session"}}
```

```bash
python3 scripts/ashare.py --env-file /secure/agent.env managed-prepare --method POST --path managed-orders --body /persistent/body.json --out /persistent/intents/buy-1.json
python3 scripts/ashare.py --env-file /secure/agent.env managed-submit --request /persistent/intents/buy-1.json
```

`expires_at`必填、含时区且晚于首次接收时刻。买单冻结限价金额及费用；卖单冻结可卖数量。到期/撤销释放未成交占用。`exits`可整个省略；卖单不可附带退出。买入成交与附带退出原子生效，退出覆盖实际买入数量。201/accepted只表示接收；成交看状态和`trade_filled`事件。首版全额模拟成交，不模拟真实排队/部分成交。

## 给已有持仓补设退出

POST `exit-rules`，正文：

```json
{"symbol":"600036.SH","quantity":100,"exits":{"stop_loss":38,"retry_policy":"stop_at_close"}}
```

同样使用`managed-prepare`/`managed-submit`。允许覆盖当天新买、尚不可卖的持仓。止损和止盈至少一个，若都有则止损小于止盈；价格单位是人民币元。卖出手数、板块单笔上限仍生效；科创板最多10万、创业板30万，其余100万股。有效退出覆盖数量不重复冻结两份持仓。

触发后按当时买一尝试卖出，触发阈值不是固定卖出限价。可选X对应最低价=本次触发阈值×(1−X/100)，向上取整到分；底价固定在触发版本上。无X就没有隐含百分比下限。成交返回`trigger_price`、`trigger_observed_price`（元）、`price`（分，除以`price_scale=100`）、数量、费用（分）、`price_difference`（成交价−阈值，元）及百分比。

触发不会因反弹、午休或重启取消。等待原因包括`WAITING_SELLABLE`（T+1）、`NO_COUNTERPARTY`、`PRICE_PROTECTION`、`QUOTE_UNAVAILABLE`、`MARKET_CLOSED`、`QUANTITY_RESERVED`。

- `continue_next_session`：跨日保留触发并继续尝试。
- `stop_at_close`：首个可卖交易日15:00仍未成交则停止，不自动重新武装。T+1任务不会在买入当日收盘丢失。只能承诺首轮有效连续竞价行情到达后尝试，有隔夜跳空风险。
- 手动挂卖/即时卖与退出共用数量池；手动卖出优先减少未受保护的部分，退出覆盖不超过卖后持仓，也不会增加原有覆盖量。退出自身成交只消耗自身剩余覆盖。清仓后取消剩余保护。手动失败、撤销或到期不会先删掉原退出条件。
- 若历史残量或后续加仓使退出覆盖不再符合板块卖出数量规则，规则停止并返回 `EXIT_QUANTITY_REQUIRES_RECONFIGURATION`。先核对持仓和停止事件，再按策略重新创建合法覆盖；系统不会擅自扩大卖出量。
- `retry_policy` 必须是字符串 `continue_next_session` 或 `stop_at_close`，不能传数组。新请求严格拒绝错误类型；旧版已接受的单元素数组会按原枚举语义修正，保留对账事件。无法识别的存量策略停止并标明 `INVALID_RETRY_POLICY`。
- 旧于该股票已成交报价的行情不用于新触发或成交，返回 `OUT_OF_ORDER_QUOTE` 等待新行情；已经由有效行情触发的退出仍按原跨日/收盘策略执行。

## 查询、修改和撤销

`managed-orders`、`exit-rules`读取活跃项及最近终态（各最多200条）；完整历史在activation事件。GET `/managed-orders/{id}`或`/exit-rules/{id}`读取一个实体，响应`entities`只有该实体。

PATCH对应实体，必须新幂等键、`expected_version`：

```json
{"action":"update","expected_version":3,"exits":{"stop_loss":37,"take_profit":45,"retry_policy":"continue_next_session"}}
```

允许下移止损，不要求买入带止损，不规定 Agent 自有策略。平台记录改前/改后/操作者/时间/当前有效行情；未知行情明确为null及unknown。修改已触发退出须额外`reset_trigger:true`，明确清除本次待执行意图并按新条件重新监测，旧触发历史保留。退出可修改覆盖`quantity`；待成交买单只改附带`exits`，`exits:null`移除附带条件。调整原委托价格/数量/到期须先撤销，再另建新委托。

撤销正文`{"action":"cancel","expected_version":3}`。旧版本409需先对账，不自动拿新版本覆盖。已终态返回409 `ENTITY_TERMINAL`及`error.details.entity`真实状态；原写请求重放则回原接收回执，当前状态另GET。撤销API Key不会自动撤销已接受的委托，需先显式撤销实体。

格式错误在配额和行情请求前拒绝。撤单使用独立的每账户每10秒20次额度，不占用每10秒5次的新建/修改额度，也不等待行情；命中各自额度时遵循 `Retry-After`，保留原请求及幂等键。即时新订单会在同一账本事务中先释放已到期占用；历史已记录的拒单仍按原幂等结果恢复。

公司行动不自动入账或调价。报价名称含当日可信来源的XD/XR/DR标记时，对账给出证券、日期、来源及“止损/止盈价未随除权调整”；其余覆盖明确未核验，不把价格跳空当作除权证据。

原 `/orders`、`prepare`、`submit`语义保持即时成交或拒绝；切勿用旧`submit`发送managed请求。机器协议见`openapi.json`。

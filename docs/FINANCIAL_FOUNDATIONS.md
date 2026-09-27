# 03 / 04 公司行动与未知状态执行

`ledger.apply_corporate_action(ActionTerms(...), at=...)` 接受 QDK financial.actions 契约，复用现有精确双式账本，不制造成交。支持拆股、分红权益/支付、换股、分拆、供股权分派/显式行权、碎股现金和终止现金。source/evidence_id、原价口径、目标估值及成本分配必须明确。失败恢复全部账本状态；同 ID 同条款幂等，不同条款拒绝。

分红权益先产生应收，支付才入现金。碎股现金需要 retired_quantity；供股需要 election_quantity，不能默认全额认购；新认购股的 lot 日期为行权日。复杂转换只支持同币种、单位乘数、非做空现金证券，不含自动 FX/税费/税务成本规则。目标必须先注册；不支持 streaming artifact sink 的原子行动批次。复杂事件必须与交易和估值按时间顺序回放。

底层账本现金不是市场交收模型：美股/港股适配在行权前另外检查已交收可用现金。其他调用者也必须提供市场层约束。分拆/合并目标价格来自显式输入，不是自动找价。

`resolve_a_share_replay_status` 保留缺失 flag 为 unknown。RuleBookRiskGate 对 unknown/no_restriction 返回 MARKET_STATUS_UNKNOWN。InstrumentSpec.metadata["requires_status_evidence"]="true" 启用严格模式：只有行情、没有状态事件时不默认开市；状态不能沿用到下一 trading_day。旧数据未启用该开关保留兼容行为；严格研究必须启用或每日显式投递状态。

测试：tests/test_financial_actions.py（财富/成本守恒、幂等、现金、失败回滚），tests/test_rules.py（状态缺失与跨日过期）。不涉及真实券商订单。

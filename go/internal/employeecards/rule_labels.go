package employeecards

var quotaIssueLabels = map[string]string{
	"verification_missing":                   "尚未登记这个范围与统计期间的名下核验",
	"verification_date_invalid":              "核验日期缺失或格式不正确",
	"verification_conflict":                  "同一时间的核验记录互相矛盾，请核对原始来源",
	"verification_in_future":                 "核验日期晚于当前日期",
	"verification_claim_invalid":             "核验数量或来源不完整",
	"verification_coverage_unknown":          "尚未确认核验数量是否已经包含公司卡",
	"verification_validity_invalid":          "核验有效日期格式不正确",
	"verification_validity_unknown":          "尚未确认核验资料可使用到哪一天",
	"verification_expired":                   "核验资料已过有效期，需要重新核验",
	"verification_window_mismatch":           "核验统计期间与本次日限额或月限额不一致",
	"verification_outside_window":            "核验日期不在其声明的统计期间内",
	"verification_snapshot_invalid":          "核验所包含的公司卡或在途记录不完整",
	"rule_unconfirmed":                       "数量规则的依据、生效日期或确认状态不完整",
	"rule_not_effective":                     "该数量规则尚未生效",
	"rule_scope_unknown":                     "数量规则的运营商范围或统计期间未明确",
	"rule_scope_id_invalid":                  "数量规则的范围标识不正确",
	"rule_limit_unknown":                     "尚未确认该范围的数量上限",
	"snapshot_overlap":                       "同一名额同时被列入公司卡和在途核验，需核对",
	"snapshot_unit_missing":                  "核验关联的历史名额资料缺失，无法确定是否重复计算",
	"external_snapshot_contains_company":     "仅统计其他渠道的核验不能同时声明已包含公司卡",
	"unit_scope_unknown":                     "部分名额的实际网络或渠道未确认，无法确定统计归属",
	"activation_date_unknown":                "部分已办理名额缺少实际激活日期",
	"activation_date_invalid":                "实际激活日期格式不正确",
	"activation_in_future":                   "实际激活日期晚于当前日期",
	"snapshot_unit_scope_mismatch":           "核验关联的名额不属于声明的运营商、网络或渠道",
	"snapshot_active_date_conflict":          "公司卡激活日期与核验所包含的记录相矛盾",
	"company_exceeds_verified_total":         "已包含的公司卡与在途数量大于核验总数",
	"snapshot_contains_already_released":     "核验仍包含在核验前已确认释放的名额，请重核来源",
	"uncovered_existing_company_unit":        "补录的历史公司卡是否已包含在核验总数中尚未明确",
	"covered_pending_outcome_unverified":     "已包含在核验中的在途记录已变更结果，需重核名额",
	"negative_derived_total":                 "核验与释放记录推导出矛盾数量，请核对来源",
	"pending_policy_unknown":                 "尚未确认运营商如何计算在途申请的占额",
	"pending_coverage_policy_conflict":       "在途核验口径与已确认的占额规则不一致",
	"release_unconfirmed":                    "名额释放日期或运营商确认依据不足，仍保留占用",
	"release_policy_unconfirmed":             "历史释放规则的来源、生效日期或运营商归属不完整",
	"release_policy_incomplete":              "注销释放的起算日期或明确等待天数不完整",
	"release_before_allowed_date":            "确认释放日期早于已明确的注销释放日期",
	"release_policy_unknown":                 "运营商名额释放规则尚未确认",
	"release_before_activation":              "名额释放日期早于实际激活日期",
	"unit_phase_unknown":                     "实名名额所处办理阶段不明确",
	"unit_phase_conflict":                    "办理阶段与实际激活记录矛盾",
	"unit_network_invalid":                   "部分名额的底层网络资料格式不正确",
	"unit_network_conflict":                  "名额记录的实际网络与运营商配置相矛盾",
	"network_policy_invalid":                 "历史网络或渠道依据不完整，不能用当前配置改写历史归属",
	"cross_scope_total_conflict":             "单家运营商核验数量大于全部运营商合计数量，请核对来源",
	"company_inventory_incomplete":           "尚未确认公司实名台账完整，不能把已登记数量当作公司全部数量",
	"company_inventory_confirmation_invalid": "公司实名台账完整核对项格式不正确",
}

func quotaIssueLabel(code string) string {
	if text := quotaIssueLabels[code]; text != "" {
		return text
	}
	return "资料存在缺失或冲突，请核对相关记录"
}

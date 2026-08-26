"""规则库种子（SDD §4 十一类，规范 §2.4.6）。

absence 语义：match_text 逗号分隔关键词组**全部**未出现即命中。
"""
from __future__ import annotations

SEED_RULES: list[dict] = [
    {
        "rule_code": "PAY_ADVANCE_HIGH", "rule_name": "预付款比例过高", "risk_level": "high",
        "match_mode": "regex", "match_text": r"预付[^。]{0,14}?([0-9]+)\s*%",
        "suggestion_text": "预付款比例超过30%，建议压降或增设担保措施。",
    },
    {
        "rule_code": "PAY_CYCLE_LONG", "rule_name": "付款周期过长", "risk_level": "medium",
        "match_mode": "regex",
        "match_text": r"(?:验收合格后|交付后|到货后|到票后)\s*([0-9]+)\s*(?:个)?工作日(?:内)?支付",
        "suggestion_text": "付款周期超过60个工作日，建议缩短周期并明确起算点。",
    },
    {
        "rule_code": "AUTO_RENEW", "rule_name": "自动续约条款", "risk_level": "medium",
        "match_mode": "keyword", "match_text": "自动续约,自动延长,期满自动",
        "suggestion_text": "存在自动续约安排，请确认到期前是否需要书面异议。",
    },
    {
        "rule_code": "NO_BREACH", "rule_name": "违约责任缺失", "risk_level": "high",
        "match_mode": "absence", "match_text": "违约,赔偿,责任",
        "suggestion_text": "合同未约定违约责任条款，履约风险敞口大，建议补充。",
    },
    {
        "rule_code": "JURISDICTION_RISK", "rule_name": "管辖地不利", "risk_level": "medium",
        "match_mode": "regex",
        "match_text": r"管辖[^。]{0,20}(原告|被告|我方|对方|供方)[^。]{0,6}所在地|(?:向|由)(甲方|乙方)所在地(?:人民法院|法院)",
        "suggestion_text": "争议管辖约定为对方所在地，建议协商改为被告所在地或仲裁。",
    },
    {
        "rule_code": "PARTY_MISSING", "rule_name": "主体信息缺失", "risk_level": "high",
        "match_mode": "absence", "match_text": "统一社会信用代码,营业执照",
        "suggestion_text": "缺少签约主体证照信息，建议核实对方统一社会信用代码。",
    },
    {
        "rule_code": "AMOUNT_MISSING", "rule_name": "合同金额缺失", "risk_level": "high",
        "match_mode": "absence", "match_text": "合同金额,合同总价,总金额,总价款",
        "suggestion_text": "合同未载明金额条款，属于重大要素缺失，必须补齐后再签署。",
    },
    {
        "rule_code": "NDA_MISSING", "rule_name": "保密条款缺失", "risk_level": "medium",
        "match_mode": "absence", "match_text": "保密,机密",
        "suggestion_text": "缺少保密条款，建议按公司模板补充双向保密义务。",
    },
    {
        "rule_code": "DATA_COMPLIANCE", "rule_name": "数据处理合规提示", "risk_level": "low",
        "match_mode": "keyword", "match_text": "个人信息,数据安全,数据处理",
        "suggestion_text": "涉及数据处理内容，请确认已履行个人信息保护合规评估。",
    },
    {
        "rule_code": "IP_MISSING", "rule_name": "知识产权归属缺失", "risk_level": "medium",
        "match_mode": "absence", "match_text": "知识产权,著作权,成果归属",
        "suggestion_text": "未约定成果知识产权归属，建议明确交付物权属与许可范围。",
    },
    {
        "rule_code": "ACCEPTANCE_MISSING", "rule_name": "验收标准缺失", "risk_level": "high",
        "match_mode": "absence", "match_text": "验收,检验标准",
        "suggestion_text": "缺少验收标准条款，付款节点将缺乏客观依据，建议补充。",
    },
]

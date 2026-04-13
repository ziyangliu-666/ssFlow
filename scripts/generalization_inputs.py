"""20 diverse test inputs for generalizability testing.

Covers:
- A股: 主板蓝筹, 创业板, 科创板, 小盘成长, ST
- 美股: mega-cap tech, macro/Fed
- 商品: crude oil
- 事件类型: earnings, policy, m_a, regulatory, shareholder_action,
            ipo, macro, geopolitical, supply_disruption, lawsuit
- 方向: 利好, 利空, 中性/分歧
"""

GENERALIZATION_INPUTS: list[dict] = [
    # ═══ A股 主板蓝筹 ═══
    {
        "id": "ashare_earnings_beat",
        "input": "比亚迪2026Q1财报: 营收2380亿元同比+22%, 净利润105亿同比+38%, 新能源车销量突破100万辆单季纪录, 毛利率22.1%超预期",
        "schedule": "quick-3r",
        "category": "A股-主板",
        "event_type": "earnings",
        "direction": "利好",
    },
    {
        "id": "ashare_earnings_miss",
        "input": "贵州茅台2026Q1业绩快报: 营收同比-3.2%为近十年首次季度下滑, 直销占比下降至38%, 批价跌破2200元, 管理层称消费降级影响持续",
        "schedule": "quick-3r",
        "category": "A股-主板",
        "event_type": "earnings",
        "direction": "利空",
    },
    {
        "id": "ashare_policy_rate_cut",
        "input": "中国人民银行宣布下调MLF利率25bp至2.25%, 同时下调7天逆回购利率15bp, 为年内第二次降息, 释放明确宽松信号",
        "schedule": "quick-3r",
        "category": "A股-宏观",
        "event_type": "policy",
        "direction": "利好",
    },
    {
        "id": "ashare_regulatory_crackdown",
        "input": "国家市场监管总局对某头部互联网平台开出82亿元反垄断罚款, 要求6个月内完成整改, 涉及'二选一'和大数据杀熟行为",
        "schedule": "quick-3r",
        "category": "A股-互联网",
        "event_type": "regulatory",
        "direction": "利空",
    },
    {
        "id": "ashare_ma_positive",
        "input": "中国神华公告拟以换股方式吸收合并国电电力, 交易对价约1200亿元, 合并后将成为全球最大综合能源集团",
        "schedule": "quick-3r",
        "category": "A股-央企重组",
        "event_type": "m_a",
        "direction": "利好",
    },
    # ═══ A股 创业板/科创板 ═══
    {
        "id": "ashare_gem_ipo",
        "input": "国产GPU龙头芯瞳半导体创业板IPO首日, 发行价68.50元, 募资45亿元, 公司对标英伟达A100性能达70%, 已获多家互联网大厂订单",
        "schedule": "quick-3r",
        "category": "A股-创业板",
        "event_type": "ipo",
        "direction": "利好",
    },
    {
        "id": "ashare_star_pharma",
        "input": "科创板创新药企百济神州GLP-1减肥药III期临床数据出炉: 24周体重降低18.2%优于司美格鲁肽, 但肝毒性信号导致FDA要求追加安全性试验",
        "schedule": "quick-3r",
        "category": "A股-科创板",
        "event_type": "other",
        "direction": "分歧",
    },
    {
        "id": "ashare_smallcap_ai",
        "input": "AI应用龙头科大讯飞发布星火大模型4.0, 多项基准测试超越GPT-4o, 宣布与三大运营商签署百亿级智慧城市框架协议",
        "schedule": "quick-3r",
        "category": "A股-AI概念",
        "event_type": "other",
        "direction": "利好",
    },
    # ═══ A股 特殊情形 ═══
    {
        "id": "ashare_shareholder_unlock",
        "input": "宁德时代公告: 4月15日将有18.6亿股限售股解禁, 占总股本38%, 解禁市值超4000亿元, 为创业板史上最大解禁",
        "schedule": "quick-3r",
        "category": "A股-解禁",
        "event_type": "shareholder_action",
        "direction": "利空",
    },
    {
        "id": "ashare_st_delisting",
        "input": "*ST地产2025年报亏损47亿元, 连续三年亏损触发退市条件, 深交所已发出终止上市事先告知书, 公司称将申请听证",
        "schedule": "quick-3r",
        "category": "A股-ST退市",
        "event_type": "other",
        "direction": "利空",
    },
    {
        "id": "ashare_dividend_high",
        "input": "工商银行公告2025年度分红方案: 每股派息0.33元, 分红率35%, 股息率达6.8%, 为近五年最高, A+H合计分红超1000亿",
        "schedule": "quick-3r",
        "category": "A股-银行高股息",
        "event_type": "dividend",
        "direction": "利好",
    },
    {
        "id": "ashare_scandal_fraud",
        "input": "证监会立案调查: 某白酒上市公司涉嫌财务造假, 2023-2025年虚增营收约120亿元, 实控人已被采取留置措施",
        "schedule": "quick-3r",
        "category": "A股-暴雷",
        "event_type": "lawsuit",
        "direction": "利空",
    },
    {
        "id": "ashare_sector_rotation",
        "input": "工信部联合发改委发布《低空经济发展三年行动计划》, 2026-2028年投入5000亿打造城市空中交通网络, eVTOL适航认证提速",
        "schedule": "quick-3r",
        "category": "A股-政策主题",
        "event_type": "policy",
        "direction": "利好",
    },
    {
        "id": "ashare_northbound_outflow",
        "input": "北向资金单日净卖出创纪录的280亿元, 其中沪股通净卖出180亿, 主要减持白酒、新能源和半导体板块, 市场传闻MSCI拟下调A股权重",
        "schedule": "quick-3r",
        "category": "A股-外资",
        "event_type": "other",
        "direction": "利空",
    },
    # ═══ A股 跨市场/地缘 ═══
    {
        "id": "ashare_trade_war",
        "input": "美国宣布对中国半导体设备加征25%关税, 同时将12家中国AI芯片企业列入实体清单, 中方商务部表示将采取对等反制措施",
        "schedule": "quick-3r",
        "category": "A股-地缘",
        "event_type": "geopolitical",
        "direction": "利空",
    },
    {
        "id": "ashare_property_rescue",
        "input": "国务院常务会议决定设立5000亿房地产纾困基金, 收购已建未售商品房转为保障房, 同时取消一线城市限购, 30城跟进",
        "schedule": "quick-3r",
        "category": "A股-地产",
        "event_type": "policy",
        "direction": "利好",
    },
    # ═══ 美股 ═══
    {
        "id": "us_nvda_earnings",
        "input": "NVIDIA Q1 FY2027 earnings: revenue $44.2B (+68% YoY) vs consensus $41.5B, data center $38.8B, gross margin 78.2%, Q2 guidance $48B midpoint vs $43B expected, Blackwell Ultra architecture announced",
        "schedule": "quick-3r",
        "category": "US-MegaCap",
        "event_type": "earnings",
        "direction": "利好",
    },
    {
        "id": "us_fed_hawkish",
        "input": "Federal Reserve holds rates at 4.25-4.50% but removes forward guidance language on rate cuts. Powell press conference: 'We see no urgency to adjust rates given persistent services inflation at 4.1%'. Dot plot median shifts to just one cut in 2026",
        "schedule": "quick-3r",
        "category": "US-Macro",
        "event_type": "macro",
        "direction": "利空",
    },
    # ═══ 商品 ═══
    {
        "id": "oil_opec_cut",
        "input": "OPEC+ emergency meeting agrees to extend 2.2M bpd voluntary cuts through Q3 2026 and announces additional 500K bpd cut by Saudi Arabia. Brent crude jumps 4% to $82 on the announcement. Russia commits to full compliance",
        "schedule": "quick-3r",
        "category": "Commodity-Oil",
        "event_type": "opec_meeting",
        "direction": "利好",
    },
    {
        "id": "oil_geopolitical",
        "input": "胡塞武装在红海击中两艘油轮导致苏伊士运河暂停通行, 全球约12%的石油贸易受影响, 航运巨头马士基宣布绕道好望角, 运费指数单日暴涨35%",
        "schedule": "quick-3r",
        "category": "Commodity-地缘",
        "event_type": "geopolitical",
        "direction": "利好",
    },
]

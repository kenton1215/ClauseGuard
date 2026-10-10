# 企业合同审核智能体（v2.1）

基于"六层工程体系"的企业合同智能审核 Web 应用（Streamlit）。
核心设计原则：**LLM 只做语义理解与推理，精确规则与阈值判断交给代码，最终决策权保留给人**。

## 功能一览

- 🔑 用户注册/登录（PBKDF2 加盐哈希，首个注册账号自动成为管理员）
- 🎛️ **三种审核模式（每用户可在「设置」中随时切换）**：
  - 🟢 系统默认 LLM：前 **5 份免费**，超出后 **¥0.25/份** 解锁查看结果（支付渠道后续接入，当前为测试阶段模拟支付）
  - 🔵 自有 API Key：用户填入自己申请的大模型 Key，**完全免费开放**、不消耗额度
  - ⚪ 纯规则引擎：免费开放、不消耗额度
- 📤 上传 PDF / Word / TXT 合同（扫描件自动 OCR 回退）
- 🧠 双通道审核：确定性规则引擎（28 条规则）+ LLM 多智能体语义审查（4 专业 Agent + Fusion 汇总）
- 📊 百分制评分（扣分制、刚性项一票否决）+ 风险等级（低/中/较高/高）
- 📄 达观格式审核报告：风险批注（原文依据+修改建议+法条引用）、要素摘要、审查依据、总体意见；
  审核完成**自动跳转报告页**
- 🗳️ 人工反馈闭环：每条批注可「采纳/排除」，结果回流数据库
- 🗂️ 审核历史、报告导出（Markdown）、全量审计日志
- 🔒 隐私保护：上传的合同原文件**不落盘**（仅内存解析）；可选「不留痕模式」审核后不保存任何记录；
  历史记录支持**手动删除**（级联删除批注并返还 1 次额度）
- 📚 知识库浏览 + 法条语义检索（民法典 1260 条 / 公司法 266 条 / 民诉法 306 条逐条原文）

## 快速开始

```bash
cd "f:\合同关键条款识别智能体"
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m streamlit run app.py
```

浏览器打开 http://localhost:8501 → 注册账号（首个账号为管理员）→ 上传合同 → 开始审核。

## LLM 配置（可选）

`.env` 中的 `DEEPSEEK_API_KEY` 已自动读取。也可在「管理设置」页配置任意 OpenAI 兼容端点
（DeepSeek / 通义千问 / 私有化 vLLM 等）。**未配置 API Key 时自动降级为纯规则引擎模式**，
全部硬规则、评分与报告照常工作。

## 目录结构

```
app.py               Streamlit 前端（登录/审核/历史/知识库/管理）
core/                审核引擎（10 个模块）
  doc_parser.py      文档解析 + 条款边界切片（5000字/300重叠）
  extractor.py       六元组事实抽取（正则+LLM双通道、头部优先+迭代传递、大写金额转换）
  rule_engine.py     数据驱动规则引擎（10 种 logic_type）
  risks.py           多智能体协同（分类→4专业Agent→Fusion，证据强制校验）
  rag.py             零依赖法条/条款/案例检索
  analyzer.py        编排 + 扣分制评分（LLM建议半权重、刚性一票否决）
  report.py          达观格式报告生成
  llm.py             OpenAI 兼容 LLM 客户端（JSON mode+重试）
  db.py              SQLite（用户/审核/批注/审计，PBKDF2）
  config.py          配置与阈值（settings.json + .env）
knowledge/           知识库
  legal/             民法典(1260条)/公司法2023(266条)/民诉法2023(306条) 逐条原文 JSON
                     + 企业内控指引第16号 + 法条索引.json
  rules/             risk_rules.json（28 条规则，阈值可配置）
  standards/         clause_library.json（12 类标准条款，主选+备选）
  cases/             case_library.json（12 条条款-风险标注对）
  supplier/          supplier_db.json（供应商风险库）+ README（核验要点/数据源/P1路线）
samples/             验证样例合同（高风险/合规各一份）
scripts/             run_analysis.py（CLI测试）/ make_samples.py（样例生成）
                     / build_law_json.py（法条 HTML→逐条 JSON 解析）
tests/               pytest 集成测试（12 项）
docs/方案优化说明.md  方案优化与实施记录
```

## 测试

```bash
.venv\Scripts\python.exe -m pytest tests/ -q
.venv\Scripts\python.exe scripts/run_analysis.py samples/高风险合同_示例.docx --commitment 36   # 无LLM管道
.venv\Scripts\python.exe scripts/run_analysis.py samples/高风险合同_示例.docx --llm             # LLM双通道
```

## 风险等级

- 80-100 低风险 · 60-79 中风险 · 40-59 较高风险 · 0-39 高风险
- 存在刚性不通过项（无限责任/主体信息缺失/金额大小写不一致/供应商失信）时强制"高风险"并禁止流转

## 知识库更新

- 法律法规：从官方渠道下载新版全文 HTML 后，运行
  `python scripts/build_law_json.py <文件.html> <法律名称> --out knowledge/legal/<名称>_逐条.json` 即可替换；
- 风险规则：直接编辑 `knowledge/rules/risk_rules.json`（阈值/等级/建议均可调，重启即生效）；
- 供应商库：按 `knowledge/supplier/README.md` 格式维护 `supplier_db.json`。

> 本系统审核意见仅供参考，最终决策以法务人工审查意见为准。
"# ClauseGuard" 

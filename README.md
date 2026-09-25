# AI 简历初筛 MVP

端到端跑通：丢一个简历文件夹进去 → 出一张能直接用的 Excel。

```
PDF 解析 → 9 字段结构化提取（带原文溯源） → 评分卡（外置 JSON） → 规则硬门槛 + LLM 软评分 → Excel 导出 → HR 复核回写
```

严格遵循 `docs/产品方案.md`（v0.1）：硬门槛用规则不用 LLM、每条结论必须带原文引用、
未提及不得淘汰、受保护属性不参与评分。验收情况见 `docs/验收总结.md`。

## 安装

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m pip install -e .   # 安装命令行入口 resume-screen
cp .env.example .env      # 填入 DASHSCOPE_API_KEY
```

> Windows PowerShell 用 `.venv\Scripts\Activate.ps1`；Git Bash 用 `source .venv/Scripts/activate`。
> 若不做 `pip install -e .`，用 `PYTHONPATH=src .venv/Scripts/python.exe -m resume_screening.cli.main ...`
> 代替下文所有 `resume-screen`。

## 三条命令跑完一轮筛选

```bash
resume-screen --help
```

### ① 从 JD 生成评分卡草稿

```bash
resume-screen rubric new \
  --jd "招聘 AI 应用开发工程师：负责 RAG 与 Agent 应用落地，要求本科及以上、3 年以上开发经验，
        有实际模型微调或 RAG 上线经验，地点北京" \
  --out rubrics/ai-engineer.json \
  --version 2026-09-26-ai-must
```

从文件读 JD：

```bash
resume-screen rubric new --jd-file jd/ai.txt --out rubrics/ai-engineer.json
```

校验与查看（合规红线、口径完整性、版本）：

```bash
resume-screen rubric validate rubrics/ai-engineer.json
resume-screen rubric show rubrics/ai-engineer.json
```

> 生成的只是**草稿**，业务口径（`definition` / 正反例 / 权重）必须人工编辑后再用。
> 示例可参考 `rubrics/example.json`。

### ② 执行筛选并导出 Excel

```bash
resume-screen screen \
  --rubric rubrics/ai-engineer.json \
  --input resumes/ \
  --out data/output/初筛结果.xlsx \
  --cache-dir data/cache \
  --run-dir data/runs
```

常用可选参数：

| 参数 | 说明 |
|---|---|
| `--model qwen-max` | 换模型（默认 `qwen-plus`） |
| `--limit 10` | 只处理前 N 份（先小批量试跑） |
| `--no-cache` | 忽略缓存强制重跑解析与提取 |

输出：

- Excel：排名 / 姓名 / 联系方式 / 判定（通过·待确认·淘汰）/ 得分 / 命中维度 / **结论与原文证据** / 原文件路径；
  "待确认"行标黄；HR 在「HR复核判定」列下拉选择。
- `data/runs/<run_id>/run.json`：本次结果存档，含所用评分卡版本与 token 消耗。

### ③ HR 复核后回写

HR 在 Excel 中填好「HR复核判定」列（通过 / 淘汰 / 待确认）并保存，然后：

```bash
resume-screen review \
  --excel data/output/初筛结果.xlsx \
  --run data/runs/<run_id>/run.json
```

回写结果存为 `data/runs/<run_id>/review.json`，是 v2 校准权重的唯一依据。

## 业务口径变化时（常见）

**只改评分卡 JSON，重新执行 `screen`，不重新解析简历**（解析与提取结果按文件内容哈希缓存，
换侧重重跑几乎不再花解析的钱）：

```bash
# 复制一份评分卡改口径与权重，形成新版本
cp rubrics/ai-engineer.json rubrics/ai-engineer-v2.json
resume-screen screen --rubric rubrics/ai-engineer-v2.json --input resumes/
```

两次结果都记录了各自的 `rubric_version`，可直接做 A/B 对比，不会混在一起。

## 环境变量（`.env`）

| 变量 | 说明 | 默认 |
|---|---|---|
| `DASHSCOPE_API_KEY` | 通义千问 API key（**缺失会明确报错，不会猜测**） | — |
| `LLM_PROVIDER` | `qwen` / `fake`（fake 仅供离线演练） | `qwen` |
| `LLM_MODEL` | 模型名 | `qwen-plus` |
| `DASHSCOPE_BASE_URL` | 兼容端点 | DashScope 官方兼容端点 |

## 开发

```bash
.venv/Scripts/python.exe -m pytest -q     # 101 passed
```

分层约定（改动前请读）：CLI 薄壳 → `core`（纯逻辑，不认识 CLI、不依赖具体 SDK）→
`adapters/{parsers,llm,exporters}`。**新增能力 = 加适配器，不动核心。**

脱敏简历样本放 `fixtures/`（已 gitignore，绝不入库）；合成测试夹具在 `tests/conftest.py`。

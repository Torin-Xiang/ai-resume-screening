# AI 简历初筛工作台

给 HR 的简历初筛工具：**上传简历 + 岗位 JD → 直接看到优先级排序和一句话理由**，
标准可以随时改，改完不重新解析。

```
PDF 解析 → 9 字段结构化提取（带原文溯源） → 评分卡 → 规则硬门槛 + LLM 软性判断 → 三分组排名 → HR 复核
```

严格遵循 `docs/产品方案.md`（v1.1）：硬门槛用规则不用 LLM、每条结论必须带原文引用、
**未提及不得淘汰**（与"明确不满足"分开）、受保护属性不参与评分。

> **形态说明**：本仓库经历过一次推倒重来。v0.1 是"命令行 + 批量 Excel"，
> 已判定为失败形态（步骤多、门槛高），原因记录在 `docs/产品方案.md` §1。
> 现在的产品形态是**网页工作台**，以"一个在招岗位"为基本单位。
> v0.1 的 CLI 判断内核被完整保留、仍然可用（见文末「旧版批量 CLI」）。

---

## 快速开始（网页工作台）

### 1. 装依赖

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m pip install -e .   # 安装 resume-screen 命令
```

### 2. 构建前端（只需一次）

```bash
cd web
npm install
npm run build      # 产出 web/dist，后端会自动托管
cd ..
```

### 3. 配模型密钥

```bash
cp .env.example .env      # 填入 DASHSCOPE_API_KEY
```

不想先申请密钥、只想看看界面长什么样：

```bash
LLM_PROVIDER=fake          # 离线演练，不调用模型（结果是假的，流程是真的）
```

### 4. 启动

```bash
resume-screen serve
```

会打印：

```
网页工作台已启动
  打开：http://127.0.0.1:8000/
  停止：在本窗口按 Ctrl+C
```

浏览器打开这个地址即可。**不需要记任何其他命令**——需要记命令就说明形态还是错的。

想让同一局域网的其他电脑用（把链接发过去就能打开）：

```bash
resume-screen serve --lan
# 会额外打印：同一局域网的其他电脑可以打开：http://192.168.x.x:8000/
```

| 参数 | 说明 | 默认 |
|---|---|---|
| `--host` | 监听地址 | `127.0.0.1`（仅本机） |
| `--port` / `-p` | 端口 | `8000` |
| `--lan` | 允许局域网访问并打印可复制链接 | 关 |
| `--data-dir` | 数据目录（数据库 + 上传的简历） | `data/` |
| `--concurrency` / `-c` | 同时处理的简历份数 | `5` |

启动前会自动检查两件事，并给出可执行的下一步：端口是否被占用、前端是否已构建。

### 5. 界面上怎么用

1. **新建岗位**：把 JD 粘进去。JD 决定这一轮的筛选口径。
2. **生成筛选标准**：系统按 JD 出一份草稿（必须项 / 加分项 / 减分项），你确认或修改后才生效。
3. **拖简历进去**：支持 PDF、可一次多份、可**连续追加**。上传后自动开始处理，不用点"开始筛选"。
4. **看排名**：结果分三组，不是一条纯排序 ——
   - **推荐**：必须项全部满足
   - **待确认**：有必须项简历里**没写到**，需要你判断（**这不是淘汰**）
   - **不推荐**：有必须项**明确不满足**
5. **点一行看证据**：每条结论都能展开到维度判定 + **原文引用**（第几页、原话是什么）。没有原文就写"无原文引用"，不编造。
6. **改标准**：改完只重跑评分，不重新解析简历，排名立即更新。

---

## 环境变量（`.env`）

| 变量 | 说明 | 默认 |
|---|---|---|
| `DASHSCOPE_API_KEY` | 通义千问 API key（**缺失会明确报错，不会猜测**） | — |
| `LLM_PROVIDER` | `qwen` / `fake`（fake 仅供离线演练） | `qwen` |
| `LLM_MODEL` | 模型名 | `qwen-plus` |
| `DASHSCOPE_BASE_URL` | 兼容端点 | DashScope 官方兼容端点 |
| `HOST` / `PORT` | `serve` 的默认监听地址与端口 | `127.0.0.1` / `8000` |
| `APP_DATA_DIR` | 数据目录 | 项目下 `data/` |
| `SCREEN_CONCURRENCY` | 并发处理份数 | `5` |

---

## 开发

```bash
.venv/Scripts/python.exe -m pytest -q     # 151 passed
```

分层约定（改动前请读）：CLI 薄壳 → `services/`（业务编排）→ `core/`（纯逻辑，**不认识
FastAPI、不认识 CLI、不依赖具体 SDK**）→ `adapters/{parsers,llm,exporters}`；
`store/` 是 SQLite 持久化，`web/` 只做校验入参、调用 services、返回结果。
**新增能力 = 加适配器，不动核心。**

### 两条只在浏览器里才暴露的坑（都踩过，已留防线）

1. Windows 注册表把 `.js` 映射成 `text/plain` → 浏览器拒绝执行模块脚本 → 整页白屏。
   修法是在 `web/app.py` 导入时显式注册 MIME。
2. 页面等不到数据时容易误判成"界面有 bug"。`web/e2e/ui-smoke.mjs` 会**等候选人真正
   渲染出来再截图**：直接 `--screenshot` 会在数据到达前就截，拿到一张空界面。

```bash
# 另开一个终端先起服务，然后：
TARGET=http://127.0.0.1:8000/ OPEN_FIRST_ROW=1 EXPECT_ROWS=3 node web/e2e/ui-smoke.mjs
```

对应地，`tests/test_m7_ui_contract.py` 把"前端声明的每个接口后端都必须存在""`index.html`
引用的资源都在且 MIME 正确"变成会失败的断言——接口漂移在 TypeScript 编译期是查不出来的。

脱敏简历样本放 `fixtures/`（已 gitignore，绝不入库）；合成测试夹具在 `tests/conftest.py`。

---

## 旧版批量 CLI（v0.1，保留可用）

> 这一套是"一次性跑批"形态：准备目录 → 改 JSON → 跑命令 → 出 Excel。
> 已被判定不适合 HR 的持续工作节奏（见 `docs/产品方案.md` §1），
> 但判断内核与测试都还在，作为回归基线保留。新功能不再往这里加。

### ① 从 JD 生成评分卡草稿

```bash
resume-screen rubric new \
  --jd "招聘 AI 应用开发工程师：负责 RAG 与 Agent 应用落地，要求本科及以上、3 年以上开发经验，
        有实际模型微调或 RAG 上线经验，地点北京" \
  --out rubrics/ai-engineer.json \
  --version 2026-09-26-ai-must
```

从文件读 JD：`resume-screen rubric new --jd-file jd/ai.txt --out rubrics/ai-engineer.json`

```bash
resume-screen rubric validate rubrics/ai-engineer.json   # 合规红线 / 口径完整性 / 版本
resume-screen rubric show rubrics/ai-engineer.json
```

> 生成的只是**草稿**，业务口径（`definition` / 正反例 / 权重）必须人工编辑后再用。

### ② 执行筛选并导出 Excel

```bash
resume-screen screen \
  --rubric rubrics/ai-engineer.json \
  --input resumes/ \
  --out data/output/初筛结果.xlsx \
  --cache-dir data/cache \
  --run-dir data/runs
```

| 参数 | 说明 |
|---|---|
| `--model qwen-max` | 换模型（默认 `qwen-plus`） |
| `--limit 10` | 只处理前 N 份（先小批量试跑） |
| `--no-cache` | 忽略缓存强制重跑解析与提取 |

输出：Excel（排名 / 姓名 / 联系方式 / 判定 / 得分 / 命中维度 / **结论与原文证据** / 原文件路径，
"待确认"行标黄）+ `data/runs/<run_id>/run.json`（含所用评分卡版本与 token 消耗）。

### ③ HR 复核后回写

HR 在 Excel 中填好「HR复核判定」列并保存：

```bash
resume-screen review \
  --excel data/output/初筛结果.xlsx \
  --run data/runs/<run_id>/run.json
```

回写结果存为 `data/runs/<run_id>/review.json`，是将来校准权重的唯一依据。

### 业务口径变化时

**只改评分卡 JSON 重跑 `screen`，不重新解析简历**（解析与提取按文件内容哈希缓存）：

```bash
cp rubrics/ai-engineer.json rubrics/ai-engineer-v2.json   # 改完再跑
resume-screen screen --rubric rubrics/ai-engineer-v2.json --input resumes/
```

两次结果各记自己的 `rubric_version`，可直接 A/B 对比。

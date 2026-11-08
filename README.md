# astrbot_plugin_paper_digest

AstrBot 插件：**多平台论文趋势日报 + 每周影响力周报**。

- 数据源：arXiv、bioRxiv/medRxiv、Europe PMC、Crossref（chemRxiv / 期刊 ISSN）
- 七个研究方向（可配置顺序）：计算机 → 理论物理 → 天体物理 → 量子化学 → 计算生物学 → 医学 → 量子生物学
- 每日：趋势图 + 各方向论文精选（摘要卡片 + 几句导读）+ 合并转发链接
- 每周六：更长、更科学的影响力周报（每方向 3 篇 + 入选理由 + 跨方向观察）
- 文案由 `prompts/<kind>/SKILL.md` 驱动，可在不改代码的情况下迭代写作风格
- 分类、主题标注、影响力评分都走 AstrBot 的 provider，可在 WebUI 里选择模型

## 当前状态（v0.1.0）

- ✅ 数据层：五个 source adapter、水位线增量抓取、DOI/arXiv ID 去重、SQLite 存储
- ✅ 粗筛 + LLM 分类 + 主题覆盖选片 + 每日 4n 上限
- ✅ Skill 模板与 JSON schema、事实包（facts.json）生成
- ✅ 日报/周报图片模板（AstrBot t2i 自定义 HTML 渲染）
- ✅ 群推送序列（文本 / 图片 / 链接转发 / 每方向一个合并转发）
- ✅ 单元测试（`python -m unittest discover -s tests`）
- ⏳ 待办：真实数据回放、黄金集标注、模型槽配置、测试群验证

## 目录结构

```
main.py                 # 插件入口：调度、抓取、日报/周报编排、命令
directions.py           # 七个方向的定义、数据源映射、关键词
store.py                # SQLite：papers/classifications/reports/sends/...
textutils.py            # 时间/标题归一化等纯工具
sources/                # arxiv / biorxiv / europepmc / crossref adapter
pipeline/               # prefilter → classify → select → facts
skills.py               # SKILL.md 加载、Jinja2 渲染、JSON 校验、降级
render.py               # 调用 AstrBot t2i 渲染趋势图/摘要卡
push.py                 # 消息序列与合并转发
prompts/daily|weekly/   # SKILL.md + facts/copy JSON Schema + 示例
templates/              # daily/weekly/card HTML 模板
scripts/                # 黄金集抽样、历史回填
tests/                  # 纯逻辑单元测试
```

## 安装

1. 把本目录放到 AstrBot 的 `data/plugins/astrbot_plugin_paper_digest/`。
2. 安装依赖（容器内）：
   ```bash
   pip install -r requirements.txt
   ```
   AstrBot 通常会自动安装插件 `requirements.txt`。
3. 在 WebUI 插件配置页填写目标群、模型槽等（见下）。
4. 重启或重载 AstrBot。

插件目录名与 `metadata.yaml` 的 `name` 都必须以 `astrbot_plugin_` 开头、全小写。

## 配置要点

| 配置 | 说明 | 默认 |
|---|---|---|
| `target_groups` | 正式群号，逗号分隔 | 空 |
| `test_group` / `test_mode` | 测试群；测试模式只发测试群 | 空 / true |
| `enable_push` / `dry_run` | 是否真正推送；dry_run 只生成归档 | false / true |
| `push_time` | 日报推送时间（建议 09:30，晚于 arXiv 公告） | 09:30 |
| `weekly_push_time` | 周六周报时间 | 09:30 |
| `hard_wall_time` | 质量硬截止，超时发降级简版 | 10:00 |
| `provider_classify` | 分类/主题模型 | 空=默认 |
| `provider_summary` | 日报文案与导读模型 | 空=默认 |
| `provider_weekly` | 周报模型 | 空=默认 |
| `daily_top_per_direction` | 每方向每日最多篇数 | 5 |
| `daily_cap_factor` | 每日总上限 = factor × 7 | 4（=28） |
| `weekly_per_direction` | 周报每方向篇数 | 3 |
| `archive_dir` | 图片/原始响应归档（容器内路径） | /AstrBot/paperdaily |
| `contact_email` | 写入 User-Agent（NCBI/OpenAlex 礼貌池） | 空 |

**归档目录需要宿主机挂载**。例如：

```bash
-v /home/vacuole/astrbot/plg/paperdaily:/AstrBot/paperdaily
```

## 命令

| 命令 | 权限 | 说明 |
|---|---|---|
| `/paper_status` | 所有人 | 24h 入库、各源最后成功时间、报告状态、默认模型 |
| `/paper_models` | 所有人 | 列出可用 provider，便于填写三个模型槽 |
| `/paper_run_ingest` | 管理员 | 立即抓取所有源 |
| `/paper_build_daily` | 管理员 | 立即生成当日日报（不推送） |
| `/paper_skill_test daily\|weekly` | 管理员 | 用最近的 facts 跑 skill，返回原始 JSON |
| `/paper_pin p123` | 管理员 | 置顶某篇论文 |
| `/paper_skip p123` | 管理员 | 从选片中排除某篇论文 |

## 选片规则

1. **粗筛**：分类 + 标题/摘要关键词打分（标题权重更高），每方向保留约 200 个候选。
2. **LLM 分类**：批量输出 `primary`（唯一主方向）、`secondary`、`themes`、`relevance`。
3. **主题覆盖选片**：每方向先按主题取代表，再按分数补足到 5 篇；只使用主方向。
4. **每日上限 4n**（n=7 → 28）：超过时从篇数最多的方向退掉最低分论文。
5. **周报**：整周池子按 `impact_score`（相关度 + 是否正式发表 + 代码/数据/SOTA 信号）排序，每方向 3 篇；可用 `/paper_pin`、`/paper_skip` 人工纠偏。

## Skill 契约

每个 skill 目录包含：

```
SKILL.md            # frontmatter + 写作指令，支持 Jinja2 变量
facts.schema.json   # 输入事实包结构（文档）
copy.schema.json    # 输出 JSON 结构（强校验）
```

- 输入变量：`{{ facts_json }}`、`{{ date }}`、`{{ week_range }}`、`{{ language }}`
- 输出必须是严格 JSON（不要 markdown 代码块），插件会用 `copy.schema.json` 校验；
- 校验失败会带着错误重试一次，再失败则使用确定性降级文案；
- **只允许引用 facts 中的 `paper_id`**，渲染器和链接转发都由插件把 id 映射到真实 URL，避免模型编造链接。

## 黄金集

```bash
python scripts/golden_sample.py --db <paper_digest.db> \
  --start 2026-11-01 --end 2026-11-09 --per-direction 20 --out golden_set.csv
```

CSV 预留了 `human_primary`、`human_secondary`、`human_themes`、`human_relevance`、`notes` 列。分类黄金集建议 150 篇左右：每方向 20 篇 + 硬负例；两人独立标注、仲裁，目标 Cohen's kappa ≥ 0.7。影响力评估用 6–12 个月前论文的当前引用/venue 做回溯校准，不要用一周内引用。

## 数据源说明

| 源 | 用途 | 注意事项 |
|---|---|---|
| arXiv | 计算机/理论物理/天体物理/量子化学/计算生物/量子生物 | 3 秒/请求，按分类分页 |
| bioRxiv / medRxiv | 计算生物/医学 | cursor 分页 |
| Europe PMC | 医学/计算生物/量子生物 | 覆盖 PubMed + 预印本，含引用数 |
| Crossref | chemRxiv、天体/AAS、医学顶刊 | 元数据为主，摘要可能缺失 |
| OpenAlex | 后续增强（按 DOI） | 免费版不能按 created_date 检索 |

## 开发

```bash
python -m unittest discover -s tests -v
python -m compileall -q .
```

历史回填：

```bash
python scripts/backfill.py --start 2026-11-01 --end 2026-11-08
```

## License

MIT

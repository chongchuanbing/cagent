# 长期记忆（LongTermMemory）设计文档

> 版本：v1（2026-08-24）
> 模块位置：`cagent/memory/`、`cagent/tools/builtin/remember.py`、`cagent/prompts/memory_prompt.py`
> 相关配置：`config/agent.yaml` 的 `memory:` 段（支持热加载，保存后约 1s 生效）

## 1. 设计目标

1. **跨会话**：记忆在 session 之间持久存在，写入 `.data/memory/long_term/<namespace>/`，换一个 session 仍可召回。
2. **多次确认才晋升**：记忆不能由单次总结直接生成为长期记忆。借鉴 JDK8 分代收集（Generational GC）的晋升思想：候选记忆需要**在多个不同会话中反复出现**才能晋升为长期记忆；用户显式指定（"记住这个"）可走快速晋升直通。
3. **标签系统可配置**：标签的分类与取值支持两种生成方式——
   - **隐式（implicit）**：配置模型提示词，由 LLM 自由生成 `key: value` 标签；
   - **显式（explicit）**：在配置中枚举明确的标签分类与各分类的合法取值，LLM 只能从中选择。
   - 另有 **hybrid**（固定分类 + 自由扩展）与 **off**（不打标）两种组合模式。

## 2. JVM 分代模型映射

| JVM 概念 | 框架对应 | 说明 |
|---|---|---|
| Eden（新生代分配） | 每次 run 的原始素材 | ask_user 问答、各 step 结论、最终答案 |
| Minor GC | run 结束时的**提取**（Extractor） | 一次 LLM 调用完成提取 + 打标 + pinned 识别 |
| Survivor 区 | `candidates.jsonl` | 候选记忆，`age` 累计中 |
| MaxTenuringThreshold | `tenuring_threshold` 配置（默认 3） | 跨 N 个**不同 session** 重复出现才晋升 |
| 大对象直通老年代 | 快速晋升（pinned） | `remember` 工具 / 提取时识别用户显式话语 |
| Old Gen（老年代） | `tenured.jsonl` | 已晋升的长期记忆，跨会话召回 |
| 新生代清理 | TTL 淘汰 | 候选超 `candidate_ttl_days` 未复发则删除 |
| Major GC（二期） | 老年代降级归档 | 基于 `hits` 字段，尚未启用 |

**核心语义决策：age 按「不同 session_id」计数**。同一会话内重复提取不增加 age——否则一次长会话就能把自己熬成老年代，违背"经过多次晋升始终不变"的设计意图。判定依据是 `MemoryRecord.sessions` 列表（去重后的 session_id 集合，`age == len(sessions)`）。

## 3. 数据模型

定义于 `cagent/memory/schema.py`：

```python
class MemoryStatus(str, Enum):
    CANDIDATE = "candidate"    # 新生代：候选，等待多次确认
    TENURED   = "tenured"      # 老年代：已晋升的长期记忆

class MemoryRecord(BaseModel):
    id: str                    # 缺省按内容 SHA1 前 12 位生成 → 同一内容天然去重
    content: str
    status: MemoryStatus
    tags: Dict[str, str]       # 分类 -> 取值
    age: int = 1               # 出现过的不同会话数
    sessions: List[str]        # 判重 / age 计数依据
    pinned: bool = False       # 用户显式指定（快速晋升）
    hits: int = 0              # 被召回次数（二期淘汰机制的依据）
    created_at / updated_at: datetime
```

要点：

- **id = 内容 hash**：同一内容重复出现时天然命中同一记录，配合关键词相似度匹配构成双层去重。
- **hits 字段已落库但消费逻辑未启用**，为二期「老年代降级归档（Major GC）」预留。

## 4. 存储设计（`cagent/memory/long_term.py`）

```
.data/memory/long_term/<namespace>/
  ├── candidates.jsonl    # 新生代
  └── tenured.jsonl       # 老年代
```

- 每行一条 `MemoryRecord` 的 JSON（jsonl）。
- **写入策略：全量重写分区文件**。因为记录会随晋升 / 合并 / 冲突覆盖而变更，不适合 append-only；记忆条目数量级小，重写代价可忽略。
- 通过 `StorageBackend` 抽象读写（默认 `LocalFileStorage`），Web / 桌面端与 CLI 天然共享同一份记忆。
- `namespace`（默认 `default`）用于多租户 / 多人格隔离。

### 相似度与打分（无向量库）

中英文混合分词：英文按词（`[a-z0-9_]+`），中文按 **CJK bigram**（单字区分度不足）。

- **`similarity(a, b)`**：Jaccard 相似度 = `|tokens(a) ∩ tokens(b)| / |tokens(a) ∪ tokens(b)|`，用于**去重合并**时判断"是否为同一事实再次出现"，阈值 `match_threshold`（默认 0.6）。
- **召回打分 `_score`**：内容 token 与 query 的**重叠数** + **标签命中加权**（query 命中某标签取值的 token，该条 +2.0/个标签），用于排序取 Top-K。

设计取向：纯关键词方案零外部依赖、零额外 LLM 调用；提示词质量高时误差可控，`match_threshold` 可调。若后续需要语义级匹配，可在此层替换为向量相似度而不影响上层。

## 5. 提取器（`cagent/memory/extractor.py`）

run 结束时执行，相当于一次 Minor GC：

- **一次 LLM 调用完成**提取 + 打标 + pinned 识别，不多花 token：
  - system：`MEMORY_EXTRACT_SYSTEM_PROMPT`（可用 `prompts.memory_extract` 覆盖，支持 `{tags_instruction}` 占位符）；
  - user：总目标 + 各步骤结论与工具问答 + 最终答案（`build_memory_extract_user_prompt`）。
- 输出约定为 JSON 数组 `[{"content": ..., "tags": {...}, "pinned": false}]`，**容错解析**（兼容 ```json 代码块与前后多余文本）。
- 提取原则写在提示词里：只提取稳定、可复用的事实（用户偏好、已确认结论、重要背景），忽略一次性过程细节；用户明确要求记住的标记 `pinned=true`。
- **无 LLM 或调用失败时返回空列表**，不阻塞主流程。

### 标签清洗（`_validate_tags`）

LLM 输出的标签按模式过滤：

| 模式 | 固定分类（categories 枚举） | 分类外自由标签 |
|---|---|---|
| `implicit` | 不校验 | 放行（上限 `implicit_max_tags` 由提示词约束） |
| `explicit` | 取值必须在枚举内，否则丢弃 | **丢弃** |
| `hybrid` | 取值必须在枚举内，否则丢弃 | 放行 |
| `off` | — | 全部丢弃 |

固定分类的 `required` 标记只用于提示词文本（"必填/可选"），不做强校验——避免 LLM 硬凑错误取值。

## 6. 晋升管理（`cagent/memory/tenuring.py`）

`TenuringManager.absorb(records, session_id)` 的单条流转决策（按序判断）：

```
新候选 rec（本次 run 提取）
 │
 ├─ 1) 与老年代记录匹配（Jaccard ≥ match_threshold）
 │     → 同主题不同内容 = 事实更新：新内容覆盖旧内容，
 │       保留 id / age / hits，合并 tags，刷新 updated_at
 │       （返回 None，无晋升动作）
 │
 ├─ 2) 与既有候选匹配
 │     → 同一记忆再次出现：
 │       session_id 不在 sessions 中则追加；age = len(sessions)
 │       内容以最新为准，合并 tags，pinned 取或
 │       → age ≥ tenuring_threshold 或 pinned → migrate 到 tenured，
 │         发布 MEMORY_PROMOTED 事件
 │
 └─ 3) 全新候选
       → 写入 candidates.jsonl，age=1，sessions=[session_id]
```

每次 absorb 开始前先执行 `evict_stale`：候选 `updated_at` 超过 `candidate_ttl_days` 未复发则淘汰（0 = 永不过期）。

**冲突策略采用「新覆盖旧」**：符合"用户偏好变了"这类可变事实；若需要并存保留，改 `_absorb_one` 第 1 分支即可。

**快速晋升 `pin(content, tags)`**（remember 工具路径）：

- 与老年代已有记录匹配 → 更新内容并把 `pinned` 置 True（不重复发事件）；
- 否则新建 `status=TENURED, pinned=True` 记录直通老年代，发布 `MEMORY_PROMOTED` 事件。
- 注意：`MemoryService.remember()` **不受 `enabled` 开关限制**——用户显式意图优先于配置。

## 7. 标签系统配置

```yaml
memory:
  tags:
    mode: hybrid               # implicit | explicit | hybrid | off
    implicit_max_tags: 5       # implicit 模式下自由标签数量上限（提示词约束）
    categories:                # explicit / hybrid 模式下的固定分类
      domain:
        values: [work, life, study]
        required: false        # 仅影响提示词文本，不做强校验
      sensitivity:
        values: [public, private]
        required: false
```

- **隐式指定**（implicit）：提示词可整体用 `prompts.memory_extract` 覆盖，LLM 自由生成 `key: value`；
- **显式指定**（explicit）：`categories` 枚举分类与合法取值，提示词由 `build_tags_instruction()` 动态拼装（"必须从以下固定分类中选择，取值只能用给定枚举"），输出再经 `_validate_tags` 过滤，枚举外取值与自由标签都会被丢弃；
- **hybrid**：固定分类必选 + 自由扩展；**off**：不打标。

## 8. 主流程接线

```
Agent.run(goal, session_id)
 ├─ Agent 构造时：memory.enabled 且未显式注入 → 构建 MemoryService
 │    （llm_factory 延迟解析，避免构造期就建立模型连接）
 │
 ├─ run 开始（AgentLoop._run）
 │    memory.recall(goal, session_id, k=recall_k)
 │      → 发 MEMORY_RECALLED 事件
 │      → 召回结果转为 [{"memory": [content...]}] 前缀条目
 │        拼在 history 最前，注入每个 step 的 ReAct 上下文，
 │        且不受 history 窗口淘汰影响
 │
 ├─ run 执行：现有 plan + ReAct 流程不变
 │    · 召回记忆渲染进 build_react_user_prompt 的「长期记忆」段落
 │    · 用户说"记住…"时模型可调 remember 工具 → pinned 直通晋升
 │
 └─ run 结束（_summarize 之后）
      memory.absorb(goal, history, answer, session_id)
        → Extractor 提取候选（1 次 LLM 调用）
        → TenuringManager.absorb 分代流转（晋升/合并/淘汰）
        → 异常被吞掉：记忆沉淀失败不影响主流程
```

组件关系（`MemoryService` 为统一门面）：

| 组件 | 职责 |
|---|---|
| `MemoryService`（service.py） | 对外唯一入口：recall / absorb / remember；行为参数每次调用从 ConfigProvider 实时读取（热加载生效） |
| `LongTermMemory`（long_term.py） | 双区存储 + 记录级读写（upsert/migrate/get/remove）+ 召回打分 |
| `MemoryExtractor`（extractor.py） | run 素材 → 候选记录（一次 LLM 调用） |
| `TenuringManager`（tenuring.py） | 去重合并 / age 累计 / 晋升 / 冲突覆盖 / TTL 淘汰 / 快速晋升 |
| `RememberTool`（tools/builtin/remember.py） | 工具入口，经 `set_memory()` 延迟绑定 MemoryService；Agent.run 时若未绑定则自动补绑 |

## 9. 事件与 CLI

### 事件

| 事件 | 触发时机 | payload |
|---|---|---|
| `memory_recalled` | run 开始召回命中 ≥1 条 | `count`、`contents` |
| `memory_promoted` | 候选晋升 / pinned 直通 | `id`、`content`、`tags`、`age`、`pinned` |

CLI 端以品红色渲染，用户可感知"记忆被召回 / 已记住"。

### CLI 管理命令（相当于 GC 手动调优入口）

```
python -m clients.cli memory list [--status candidate|tenured] [--data-dir DIR]
python -m clients.cli memory promote <id>          # 手动晋升候选
python -m clients.cli memory forget <id>           # 删除记忆
python -m clients.cli memory tag <id> k=v [k=v..]  # 手动打标/改标
```

## 10. 配置项总览

| 配置 | 默认 | 说明 |
|---|---|---|
| `memory.enabled` | true | 总开关（显式 remember 不受限） |
| `memory.namespace` | default | 存储 / 隔离命名空间 |
| `memory.tenuring_threshold` | 3 | 跨 N 个不同会话出现后晋升 |
| `memory.candidate_ttl_days` | 14 | 候选未复发淘汰天数；0 = 永不过期 |
| `memory.recall_k` | 5 | run 开始召回条数；0 = 关闭召回 |
| `memory.recall_candidates` | false | 召回是否包含未晋升候选 |
| `memory.match_threshold` | 0.6 | 去重合并的 Jaccard 阈值 |
| `memory.tags.mode` | implicit | implicit / explicit / hybrid / off |
| `memory.tags.categories` | — | 固定分类与枚举取值 |
| `memory.tags.implicit_max_tags` | 5 | implicit 自由标签上限 |
| `prompts.memory_extract` | 内置 | 覆盖提取系统提示词（支持 `{tags_instruction}`） |

所有 `memory.*` 配置支持热加载（ConfigProvider mtime 轮询，约 1s 生效）。

## 11. 端到端生命周期示例

```
会话 s1：用户偏好"简洁的中文回答"被 ask_user 收集
  → run 结束提取为候选：{id: "ab12…", age: 1, sessions: ["s1"]}

会话 s2：同类偏好再次出现
  → Jaccard ≥ 0.6 匹配到既有候选 → 合并，age=2，sessions=["s1","s2"]

会话 s3：第三次出现
  → age=3 ≥ tenuring_threshold → migrate 至 tenured.jsonl
  → 发 memory_promoted 事件

会话 s4：run 开始
  → retrieve(goal) 命中该记忆 → 注入每个 step 的上下文前缀
  → 模型不再重复询问该偏好
```

若用户在任何会话中直接说"记住我喜欢表格输出"，模型调 `remember` 工具，该条 `pinned=true` **当场**进入老年代，无需等待三个会话。

## 12. 已知取舍与后续演进

| 项 | 现状 | 演进方向 |
|---|---|---|
| 去重判定 | 纯关键词 Jaccard（零 LLM 开销） | borderline 对可增加 LLM 二次确认 |
| 召回排序 | token 重叠 + 标签加权 | 可替换为向量相似度，接口不变 |
| 老年代淘汰 | `hits` 已计数，未消费 | 二期 Major GC：长期 hits=0 或被新事实覆盖多次的记忆降级归档 |
| 提取成本 | 每次 run 结束固定 +1 次 LLM 调用 | 可按 history 长度 / 是否有 ask_user 问答做触发条件 |
| 并发写 | 全量重写分区文件，无锁 | 多进程并发 run 时需加文件锁或改为队列串行化 |
| 提示词约束 | pinned 识别依赖模型自觉 | 可增加显式意图关键词前置匹配兜底 |

## 13. 测试覆盖（`tests/test_memory_tenuring.py`，13 个用例）

- 年龄累计与阈值晋升（跨不同 session）
- 同会话重复出现 age 不累计
- pinned 直通晋升（remember 工具路径）
- 候选 TTL 淘汰与不过期
- 冲突覆盖（新事实替换旧内容、保留 id/age）
- 标签三模式清洗（explicit 枚举外丢弃、hybrid 放行自由标签）
- 无 LLM 时跳过提取
- 召回默认只查 tenured、`recall_candidates` 开启后合并候选
- CLI list / promote / forget / tag 四个子命令
- 端到端：跨会话提取 → 晋升 → 召回注入 step 上下文（prompt 出现「长期记忆」段落）

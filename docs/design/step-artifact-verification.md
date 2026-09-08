# 修复方案：Step「完成语义」闭环

> 背景：会话 `646b53c406994e65baecdcfafac82d03`（cagent-intro-ppt）暴露的连环问题。
> 现象：`s13` 写 19 个幻灯片文件报 `run_tool 已达最大重试次数(2)`，实际 `slides/` 目录 0 文件；更早的 `STORY.md` / `DESIGN.md` 同样从未落盘却全部标 `done`，planner 因此反复追加内容重复的步骤（s6/s8/s11/s13/s14），形成"假成功 → 追加重复 → 再空转"的循环。
> 事后审计：s13 假 done 后 replan 追加 s14（apply_patch 重写 19 文件），s14 执行期间用户观察到"任务卡住且无文件创建"**手动终止进程**——s14 的 ReAct 回合全部在内存 `last_trace` 中蒸发（trace 0 行），meta 停留 `status=running`。
> 状态：仅设计，未改代码。日期：2026-09-07（同日补充中断审计与 P0-5）。

---

## 0. 根因定位（四个层级）

| # | 症状 | 根因 | 位置 | 后果 |
|---|------|------|------|------|
| ① | "只说话、不调工具"的回合被判成功 | 收敛语义 = "无 tool_call 即收敛"；未收敛兜底强制 `success=True` | `react.py:141-144`、`react.py:249-253`、熔断提前收敛 `react.py:189-193` | Step 无产出即标 `done`（trace 中 `filesystem.apply_patch` 真实调用 0 次，STORY/slides 全空） |
| ② | planner 无限追加语义重复步骤 | planner 信"假成功"；`adjust` 只基于成功结果增量 | `loop.py:104-118`、`planner.py:104-106` | s6/s8/s11/s13/s14 连环空转 |
| ③ | 写通道被无关失败熔断 | 失败账本按**工具名**计数，`run_tool` 门面下 find/写文件共用一把钥匙；软错误（SANDBOX/TIMEOUT）计入硬熔断 | `failure_ledger.py:28,34-48`、`react.py:163-193` | 探测阶段 2 次 `find` 失败（超时+越界）把 `run_tool` 打到上限，s13 的写文件调用被预熔断、从未执行 |
| ④ | UI 报错 trace 里查不到 | 熔断/失败分支不写 `last_trace` | `react.py:163-174` | 诊断盲区 |
| ⑤ | 进程终止后 s14 整段 trace 蒸发、meta 永久 running | trace 唯一落盘点在 step 结束后**批量冲刷**（`loop.py:83-86`），`finish()` 只在正常路径调用（`loop.py:132`）；无信号处理/无 finally 收尾 | `loop.py:47-49,83-86,132`、`storage/session.py` | 中断 step 的已发生回合全部丢失且不可审计（本次 s14 内容永久不可考）；sessions 目录 5+ 会话残留 `running`，属框架常态缺陷 |

修复目标：**让"完成"从"模型声称"变成"产物可验证"**，同时**让失败只惩罚它所属的操作**。

## 0.5 最小集已落地（2026-09-07 下午）

经评估"产物声明+校验太重"，先落地最小修复集（§10 批次前插入的最小批次，全部集中在 `failure_ledger.py` + `react.py`）：

| 改动 | 内容 | 对应章节 |
|------|------|---------|
| 软硬错误分层 | `SANDBOX/TIMEOUT/EXEC_ERROR/SCHEME_PATH_MISUSE` 及未知类型只记数、不计入总熔断；软错误重试预算 `soft_max_retries=3`（硬错误仍为 2）；软错误提示文案不再建议"改用其他工具" | §5.1 部分落地（op 化未做） |
| 未收敛不标成功 | `_summarize_unconverged` 兜底返回 `success=False`（总结保留在 output 供 replan 参考）；熔断提前收敛同样 `success=False` | §4.2 的最小子集 |
| 熔断回合落 trace | 单工具熔断与总熔断分支均 append `last_trace`（含 `ok: False`） | §7.1（原 P2-1） |

同步更新测试：`tests/test_failure_semantics.py`（新增 8 用例，含事故场景直接回归 `test_incident_soft_failures_do_not_preempt_write`）、`tests/test_six_layers.py`（L5 两用例改用硬错误类型）、`tests/test_react_planner.py`（unconverged 语义反转）。全量 298 测试通过。

产物声明/校验（P0-1/P0-2）、op 化、护栏、实时落盘**暂缓**——待用真实会话重跑验证最小集效果后再决定是否需要。

---


## 1. 设计总览

### 1.1 核心思路

引入 **Step 产物声明 + 产物校验器**作为完成判定的唯一事实来源：

```
Planner 生成 Plan ── 步骤声明 expected_artifacts（写文件类步骤必须声明）
        │
        ▼
ReActEngine 执行 ── 收敛前先问校验器：产物满足了吗？
        │              └ 满足 → 真收敛 DONE
        │              └ 不满足 → 不收敛，继续行动；迭代耗尽 → FAILED（不再强制 success=True）
        ▼
Executor 后置闸门 ── 无论引擎如何判定，产物缺失一律降级 FAILED（兜底）
        │
        ▼
AgentLoop ── FAILED → replan；护栏检测"同一产物反复失败" → 停止追加重复步骤
```

### 1.2 改动文件矩阵

| 优先级 | 改动 | 文件 | 风险 |
|--------|------|------|------|
| P0-1 | Step 增加 `expected_artifacts` 声明 + 规划提示词引导 | `schema/plan.py`、`prompts/planner_prompt.py`、`core/planner.py` | 低（新增默认空字段） |
| P0-2 | **产物校验器 ArtifactVerifier**（新文件）+ Executor 后置闸门 | `core/artifacts.py`（新）、`core/executor.py`、`core/agent.py` | 中（完成判定语义收紧） |
| P0-3 | ReAct 收敛/兜底改为"校验感知"；熔断提前收敛同步 | `core/react.py`、`prompts/react_prompt.py` | 中 |
| P0-4 | 失败账本：计数键加 op 维度 + 软硬错误分层 | `core/failure_ledger.py`、`core/react.py` | 低-中 |
| P0-5 | **trace 逐回合实时落盘 + trace 行元数据（step_id/ts）+ 中断兜底收尾** | `core/react.py`、`core/loop.py`、`storage/session.py`、`clients/cli/chat.py` | 中（I/O 频率上升；改存储时序） |
| P1-1 | 防僵尸重复步骤（无进展护栏） | `core/loop.py`、`prompts/planner_prompt.py` | 中（涉及终止条件） |
| P2-1 | 失败/熔断回合落 `last_trace`，闭合诊断 | `core/react.py` | 低 |
| P2-2 | 配置化开关 | `config/schema.py`、`config/agent.yaml` | 低 |

---

## 2. P0-1 Step 产物声明

### 2.1 数据模型 `cagent/schema/plan.py`

```python
class Step(BaseModel):
    id: str
    description: str
    depends_on: List[str] = Field(default_factory=list)
    status: StepStatus = StepStatus.PENDING
    result: Optional[StepResult] = None
    # ===== 新增 =====
    # 本步骤预期产出的文件（workspace 相对路径，支持通配符）。
    # 留空 = 分析型步骤，不做产物校验（保持原有收敛语义）。
    expected_artifacts: List[str] = Field(default_factory=list)
```

`StepResult` 增加可选字段，供校验降级/失败信息透出：

```python
class StepResult(BaseModel):
    step_id: str
    success: bool
    output: str = ""
    error: Optional[str] = None
    # ===== 新增 =====
    missing_artifacts: List[str] = Field(default_factory=list)  # 校验未通过的产物
    verified: bool = False                                       # 是否执行过产物校验
```

> 兼容性：`default_factory=list` / 默认 `False`，旧 `plan.json` 反序列化、旧调用方全部向后兼容。

### 2.2 解析与调整 `cagent/core/planner.py`

- `_build_plan`（L50-68）：`expected_artifacts=s.get("expected_artifacts", []) or []`（类型防御：非 list 置空）。
- `Plan.apply_adjust`（`schema/plan.py:54-102`）：`modify`/`add` 分支同步支持该字段。

### 2.3 规划提示词 `cagent/prompts/planner_prompt.py`

`PLANNER_SYSTEM_PROMPT` JSON 示例与规则追加：

```text
{
  "steps": [
    {"id": "s1", "description": "编写内容大纲 STORY.md", "depends_on": [],
     "expected_artifacts": ["cagent-intro-ppt/STORY.md"]}
  ]
}
规则：
- 步骤若包含"写 / 创建 / 生成 / 保存 <文件>"动作，必须在 expected_artifacts 声明
  产物路径（workspace 相对路径；多个同构文件可用通配符，如 slides/slide-*.md）。
- 纯分析 / 调研 / 回复类步骤不填 expected_artifacts。
```

`REPLAN_SYSTEM_PROMPT` / `ADJUST_SYSTEM_PROMPT` 同步补充：修订时**必须保留或修正** `expected_artifacts`；新增步骤同样遵守声明规则。

---

## 3. P0-2 产物校验器（核心改动）

### 3.1 新文件 `cagent/core/artifacts.py`

```python
"""产物校验器：以"文件真实存在"作为 Step 完成的唯一事实来源。"""
import fnmatch
from pathlib import Path
from typing import Callable, List, Optional

from ..schema.plan import Step


class ArtifactVerifier:
    """把 step.expected_artifacts（workspace 相对路径/通配符）解析为物理路径并断言存在。"""

    def __init__(
        self,
        resolve: Optional[Callable[[str], Path]] = None,
        strict_trace: bool = False,
    ):
        # resolve("workspace://x/y" 或裸相对路径) -> 物理 Path；缺省直接原样 Path
        self._resolve = resolve or (lambda s: Path(s))

    def verify(self, step: Step, trace: List[dict]) -> "ArtifactCheck":
        arts = getattr(step, "expected_artifacts", None) or []
        if not arts:
            return ArtifactCheck(checked=False, missing=[])
        missing: List[str] = []
        for art in arts:
            if not self._exists(art):
                missing.append(art)
        return ArtifactCheck(checked=True, missing=missing)

    def _exists(self, artifact: str) -> bool:
        phys = self._resolve(artifact)
        if _has_glob(artifact):
            # 通配符：对父目录展开，任一命中且非空即视为存在
            hits = list(phys.parent.glob(phys.name)) if phys.parent.exists() else []
            return any(p.is_file() and p.stat().st_size > 0 for p in hits)
        return phys.is_file() and phys.stat().st_size > 0
```

```python
class ArtifactCheck:
    def __init__(self, checked: bool, missing: List[str]):
        self.checked = checked   # 本步是否声明了产物（无声明=False，跳过校验）
        self.missing = missing   # 缺失（不存在 / 空文件 / 通配符零命中）的产物清单
```

设计说明：

- **文件存在 + 非空** 为主判据——模型"假写"（只在结论文本里给内容）不会产生文件，直接判缺失；`size > 0` 防空壳占位。
- **通配符**让"19 个 slide 文件"这种批量产物一句声明搞定：`slides/slide-*.md`。数量级断言（"恰好 19 个"）属进阶，本期不做，可在 `ArtifactCheck` 扩展 `min_count` 语义（见 §12 后续项）。
- `strict_trace`（预留）：追加"trace 中必须存在对该产物的成功 Add/Update"断言，防"文件由外部预置导致误过"。默认关闭，因会话 scratch 为新建目录、无预置文件场景。
- 校验器不触碰网络/沙箱，只做只读 `stat`/`glob`，安全。

### 3.2 Executor 后置闸门 `cagent/core/executor.py`

`execute_step`（L24-32）在引擎返回后做**强制**校验——无论引擎内部如何收敛，这是最后一道闸门：

```python
def execute_step(self, step, history=None, goal=None) -> StepResult:
    step.status = StepStatus.RUNNING
    result = self.react_engine.run(step, history=history, goal=goal)
    # ===== 新增：产物后置校验（兜底闸门）=====
    verifier = getattr(self.react_engine, "artifact_verifier", None)
    if verifier is not None and result.success:
        trace = self.react_engine.last_trace or []
        check = verifier.verify(step, trace)
        if check.checked and check.missing:
            result = StepResult(
                step_id=step.id,
                success=False,
                output=result.output,
                error=(
                    f"步骤声称完成但产物缺失：{', '.join(check.missing)}。"
                    "请实际调用文件写入工具生成这些文件后再收尾。"
                ),
                missing_artifacts=check.missing,
                verified=True,
            )
        elif check.checked:
            result.verified = True
    step.status = StepStatus.DONE if result.success else StepStatus.FAILED
    return result
```

> 关键：即使引擎已 `success=True`，只要产物缺失就**降级 FAILED** → `loop.py:120 should_replan` 触发 replan，而不是继续 `adjust` 追加新步骤。这直接掐断"假成功"链条。

### 3.3 接线 `cagent/core/agent.py`

`_build_loop`（L178-196）：构造校验器并注入引擎（单一事实来源，Executor 从引擎取）：

```python
path_space = path_space if path_space is not None else self._resolve_path_space()
verifier = None
if path_space is not None:
    verifier = ArtifactVerifier(
        resolve=lambda s: path_space.resolve(s, default_mount="workspace", mode="write")
    )
react = self._react_engine or ReActEngine(
    ...,
    artifact_verifier=verifier,   # 新增构造参数
)
```

---

## 4. P0-3 ReAct 收敛语义修正 `cagent/core/react.py`

### 4.1 收敛点改造（L141-144）

现在：无 `tool_calls` → 直接 `success=True`。改为"校验感知收敛"：

```python
if not resp.tool_calls:
    # 无产物声明的分析型步骤：维持原语义，文本即收敛
    if not getattr(step, "expected_artifacts", None):
        self.last_trace.append({"final": resp.content})
        return StepResult(step_id=step.id, success=True, output=resp.content or "")
    # 声明了产物：先校验，满足才收敛
    check = self._verify_artifacts(step)
    if check.checked and not check.missing:
        self.last_trace.append({"final": resp.content})
        return StepResult(step_id=step.id, success=True, output=resp.content or "",
                          verified=True)
    # 未满足：追加提示并继续迭代（消耗预算），防"纯文本假收敛"无限套娃
    missing = ", ".join(check.missing)
    messages.append(Message(
        role=MessageRole.SYSTEM,
        content=(
            f"本步骤声明了预期产物但尚未满足，缺失：{missing}。\n"
            "你必须调用文件写入工具实际生成这些文件，观察成功返回后再收尾。"
            "不允许仅凭文字描述代替文件写入。"
        ),
    ))
    self._emit(EventType.TOOL_RESULT, {"name": "(verifier)", "content": f"产物缺失：{missing}", "ok": False}, step_id=step.id)
    continue
```

配套小工具方法：

```python
def _verify_artifacts(self, step: Step):
    if self.artifact_verifier is None:
        return ArtifactCheck(checked=False, missing=[])
    return self.artifact_verifier.verify(step, self.last_trace)
```

### 4.2 未收敛兜底（L249-253）与熔断提前收敛（L189-193）

现在两者都无脑 `success=True`（`_summarize_unconverged` 有值即成功）。统一改为：

```python
fallback = self._summarize_unconverged(messages)
if fallback:
    check = self._verify_artifacts(step)
    # 声明了产物但缺失 → 真失败（触发 replan），不再"尽力成功"
    if check.checked and check.missing:
        self.last_trace.append({"final": fallback, "unconverged": True})
        return StepResult(
            step_id=step.id, success=False, output=fallback,
            error=f"ReAct 未收敛且产物缺失：{', '.join(check.missing)}",
            missing_artifacts=check.missing,
        )
    self.last_trace.append({"final": fallback, "unconverged": True})
    return StepResult(step_id=step.id, success=True, output=fallback)
```

熔断提前收敛（L189-193）同构处理。这样：
- 分析型步骤（无产物）：行为不变（`unconverged` 总结仍算成功，向后兼容）；
- 生产型步骤（有产物）：空转到底 → `success=False` + 缺失清单 → Executor 标 FAILED → **replan**。planner 拿到失败原因（缺失文件），才有信息修正策略，而不是像现在这样盲目追加重复步骤。

### 4.3 提示词软约束 `cagent/prompts/react_prompt.py`

在步骤级 system prompt 追加一句（仅在步骤声明产物时注入）：

```text
本步骤预期产出：{artifacts}。收尾前必须调用文件写入工具实际创建这些文件，
并确认工具返回成功；不得仅用文字描述代替交付。
```

---

## 5. P0-4 失败账本：op 粒度 + 软硬分层

### 5.1 `cagent/core/failure_ledger.py`

```python
# ===== 新增：错误类型分层 =====
# 硬错误：环境/契约级，重复犯说明该操作不可行 → 允许熔断该操作
HARD_KINDS = {"TOOL_UNAVAILABLE", "PARAM_MISSING", "PARAM_VALIDATION", "CIRCUIT"}
# 软错误：单次用法/环境瞬时问题 → 只提示，不烧总熔断
SOFT_KINDS = {"SANDBOX", "TIMEOUT", "EXEC_ERROR", "未知错误", None}
# 软错误同操作允许的重试上限（给模型试错空间，但防止无限循环）
MAX_SOFT_RETRIES = 3
```

计数键从 `tool_name` 升级为 **`(tool_name, op)`**：

```python
def _key(self, tool_name: str, op: Optional[str]) -> str:
    # op 用于门面工具（run_tool 的 path）区分叶子操作
    return f"{tool_name}:{op or '*'}"

def record_failure(self, tool_name, error_kind=None, op=None) -> None:
    key = self._key(tool_name, op)
    self._tool_failures[key] = self._tool_failures.get(key, 0) + 1
    if error_kind:
        self._failure_reasons[key] = error_kind
    # 只有硬错误才累计全局熔断；软错误（SANDBOX/TIMEOUT/EXEC）不进总闸
    if error_kind in HARD_KINDS:
        self._total_failures += 1

def should_retry(self, tool_name, op=None) -> bool:
    key = self._key(tool_name, op)
    kind = self._failure_reasons.get(key)
    limit = self.max_retries_per_tool if kind in HARD_KINDS else MAX_SOFT_RETRIES
    return self._tool_failures.get(key, 0) < limit

def is_circuit_broken(self) -> bool:   # 语义不变，但只由硬错误驱动
    return self._total_failures >= self.max_total_failures
```

`get_retry_hint` 同步改签名 `(tool_name, op=None)`，提示文案携带 op，让模型知道**是哪个操作**失败、失败多少次。

### 5.2 `cagent/core/react.py` 调用点适配（L155-247）

在每个工具调用的 try 分支，从参数提取 op：

```python
def _op_of(name: str, args: dict) -> Optional[str]:
    # run_tool 是门面：以 path（叶子操作）为粒度，写文件不再被 find 连坐
    if name == "run_tool":
        p = args.get("path")
        return p if isinstance(p, str) else None
    return None  # 普通工具退化为工具名粒度（原行为）
```

- L163 `should_retry(name)` → `should_retry(name, self._op_of(name, args))`
- L220-221 `record_failure(name, error_kind)` → `record_failure(name, error_kind, self._op_of(name, args))`
- L164 / L225 的 hint 文案同传 op。
- L177 `is_circuit_broken()` 不变——因软错误已不进 `_total_failures`，本案的 2 次 `find` 失败（TIMEOUT+SANDBOX 均为软）**不会再触发全局熔断**；且两次失败 key 不同作用域也与写文件隔离。

> 本会话修复后推演：L90 `find /` TIMEOUT → 记 `run_tool:shell.run_command` soft×1；L91 越界 SANDBOX → soft×2；均 < MAX_SOFT_RETRIES，且软错误不进总闸 → 不熔断。s13 调 `run_tool`（path=`filesystem.apply_patch`）→ 全新 key，计数 0 → 正常执行。

### 5.3 语义落定：注释与实现一致

- 保留 run 级 `reset()`（`agent.py:230`，跨 run 隔离）不变；
- 修正 `react.py:109-110` 误导注释：账本实际在 Agent 级共享、run 开头重置一次，**按 `(tool, op)` 维度在 run 内自然衰减**（不同 op 互不影响），删除"每 step 重置但未实现"的表述。如后续需要"同一步骤内快速重试预算"，新增 `ledger.begin_step()` 由 `Executor.execute_step` 调用（本期不做，避免过度设计）。

---

## 6. P0-5 trace 实时落盘与中断兜底

> 动机（本会话实证）：trace 唯一落盘点是 `loop.py:83-86` 的 step 末批量冲刷；`finish()` 只在 `loop.py:132` 正常路径调用。用户在 s14 卡死（无文件产出、看不到进展）后手动终止进程 → s14 已发生的全部 ReAct 回合蒸发、meta 永久 `running`。且 sessions 目录 5+ 历史会话同为 `running` 残留——"终止不留痕"是框架常态，任何事故都无法事后完整审计。

### 6.1 逐回合实时落盘（`core/react.py` + `storage/session.py`）

把"攒 last_trace、step 末冲刷"改为"每回合即时 append"：

```python
# ReActEngine 构造注入 recorder（或轻量 append 回调 trace_sink）
class ReActEngine:
    def __init__(self, ..., trace_sink: Optional[Callable[[dict], None]] = None):
        self.trace_sink = trace_sink

    def _record(self, entry: dict):
        self.last_trace.append(entry)          # 内存保留（兼容既有读取方）
        if self.trace_sink:
            self.trace_sink(entry)             # 即时落盘（幂等 append 一行 JSON）
```

所有 `self.last_trace.append(...)` 处（thought/action+observation/final/失败回合）统一走 `_record()`。`loop.py:83-86` 的批量冲刷改为 no-op 兜底（或仅在 sink 缺席时使用，保留无 recorder 场景如纯测试的可运行性）。

落盘频率：每次回合 1 行追加写（`open(path, "a")`），量级 = LLM 交互轮数（本会话 111 行 / 17 分钟），I/O 开销可忽略。

### 6.2 trace 行元数据（可审计性前提）

```python
entry = {
    "ts": time.time(),            # 回合时间戳
    "step_id": step.id,           # 归属步骤（loop 不必再按 final 切段猜）
    "action" / "thought" / "final" / "observation": ...,
    "ok": bool,                   # 该回合是否失败（熔断/工具错误为 False）
}
```

缺失行不再是"看起来连续"——每行带 ts + step_id，中断点可由最后一行明确标出。

### 6.3 中断兜底收尾（`core/loop.py` + 入口层）

`AgentLoop.run` 主循环包 `try/finally`：

```python
def run(self, goal=None, history=None):
    try:
        ...existing loop...
        recorder.finish("done")
    finally:
        # 任何退出路径（异常/取消/信号）都保证收尾
        recorder.finish("interrupted")   # finish 幂等：已 finish 则跳过
        recorder.record_plan(plan)      # 冻结最终 plan 状态
```

`SessionRecorder.finish` 改幂等（重复调用跳过），并支持 `status ∈ {done, failed, interrupted}`。CLI `chat.py` / Web 入口对 `KeyboardInterrupt`（Ctrl-C）显式捕获后同样走 finally（Python 的 finally 本身覆盖 KeyboardInterrupt，此处主要是确认无 `except: pass` 吞掉）。

> 交互设计说明：用户手动终止**必须是合法操作**（本会话用户正是因为"卡住且无产出"才终止，这是正确判断）——框架的义务不是阻止终止，而是终止后状态自洽：trace 完整、meta 如实标 `interrupted`、resume 时能从断点继续而不是把半截 run 当正常结束。

### 6.4 resume 感知（`storage/session.py`）

`load_history` 折叠 prior trace 时，遇 `status=interrupted` 的会话在 history 前缀注入一条提示：

```text
[系统] 上次运行被手动中断于 step <s14>（已发生 N 回合，其中工具写入 0 次）。
中断前该步骤未完成，相关产物可能缺失，请先核验再继续。
```

配合 P0-2 产物校验，resume 后的重复执行会自动对齐真实文件状态，而非依赖不可靠的 plan 状态。

---

## 7. P1-1 防僵尸重复步骤（无进展护栏）

### 6.1 `cagent/core/loop.py`

维护"产物 → 失败次数"账本，step FAILED 时累计；replan 前检测：

```python
# _run 内初始化
self._artifact_failures: Dict[str, int] = {}

# step 结束后（L96 附近）：
if not result.success:
    for art in getattr(result, "missing_artifacts", []) or []:
        self._artifact_failures[art] = self._artifact_failures.get(art, 0) + 1

# replan 之前（L120 前）：
if self._halt_no_progress():
    break   # 终止循环，_summarize 会给用户"部分完成+阻塞原因"结论
```

```python
def _halt_no_progress(self) -> bool:
    """同一产物连续失败达到阈值，说明 planner 反复追加的是僵尸步骤。"""
    return any(c >= 2 for c in self._artifact_failures.values())
```

护栏触发后不抛异常、不静默——最终 `_summarize` 会把 FAILED 步骤与"产物反复缺失"如实汇总给用户，用户可以人工介入（改描述/换策略），而不是框架自嗨地无限追加重复步骤。

> 阈值 2 的含义：首次失败 replan 重试可接受；第二次仍失败（同一产物）则判定"该路径走不通"，交给用户。`missing_artifacts` 为空的历史 FAILED（非产物类失败）不受护栏影响，仍走普通 replan 语义。

### 6.2 `cagent/prompts/planner_prompt.py`（REPLAN）

规则追加：

```text
- 若失败步骤声明了 expected_artifacts：不要为同一批产物反复追加与已失败步骤
  描述重复的新步骤。应分析失败观察（缺哪些文件/什么错误），更换写入策略
  （换工具、拆小步、先写清单再逐个写）；确无可行路径时，输出一个
  「向用户报告阻塞」的步骤而不是继续堆重复步骤。
```

---

## 8. P2 诊断与体验

### 7.1 失败/熔断回合落 trace（`react.py:163-193`）【已被 P0-5 §6.1 吸收】

> 原列为 P2；因 P0-5 将所有回合（含失败/熔断）统一走 `_record()` 即时落盘，此项随之升级完成，不再单列实现。

现在熔断分支直接 `continue`/提前收敛，不写 `last_trace` → UI 有、trace 无，本次诊断绕了大圈。改为：每个熔断/失败回合在返回前统一追加 `{"action": {...}, "observation": obs_content, "ok": False}`，让 `trace.jsonl` 与 UI 完全一致。

### 7.2 配置化（`config/schema.py` + `config/agent.yaml`）

```yaml
# 完成语义（可选，缺省用内置默认）
completion:
  artifact_check: true      # 步骤声明 expected_artifacts 时执行产物校验（后置闸门）
  halt_repeat_failures: 2   # 同一产物连续失败 N 次后停止追加重复步骤；0 = 关闭护栏
  soft_retries: 3           # 软错误（SANDBOX/TIMEOUT/EXEC）同操作重试上限
trace:
  realtime_flush: true       # 逐回合即时落盘（P0-5）；false 回退 step 末批量冲刷
```

`ReActEngine` / `Executor` / `AgentLoop` 从 `config.get_config()` 读取（走热加载通道）。

---

## 9. 测试计划

### 9.1 新增 `tests/test_artifact_verification.py`

| 用例 | 场景 | 期望 |
|------|------|------|
| V1 | step 声明 `STORY.md`，ReAct 只产文本、无 tool_call | 收敛前校验不通过 → 追加提示 → 耗尽 → `success=False` + `missing_artifacts=['...STORY.md']`；Executor 标 FAILED |
| V2 | step 声明产物，模型经 `apply_patch` 真写成功 | 校验通过 → DONE（`verified=True`） |
| V3 | 写空文件（0 字节） | 判缺失（size=0）→ FAILED |
| V4 | 通配符 `slides/slide-*.md`，写入 3 个命中 | 通过 |
| V5 | `run_tool` 的 find 失败（SANDBOX）×2 后，同 run 内调 `run_tool` 写 `filesystem.apply_patch` | **可正常执行**（op 隔离，回归本会话场景） |
| V6 | SANDBOX/TIMEOUT 软失败 ×5 | `is_circuit_broken()` 仍 False；同 op 软失败达 3 才提示 |
| V7 | 无 `expected_artifacts` 的分析步骤，纯文本收尾 | 行为与旧版一致（success=True）——兼容性用例 |
| V8 | 同一产物 FAILED 2 次后 replan | `_halt_no_progress()` 为 True，loop 终止并汇总阻塞 |
| V9 | step 执行中注入 `KeyboardInterrupt`（模拟手动终止） | 已发生回合**已实时落盘**（trace 行含 ts/step_id）；meta `status=interrupted`；`finish` 幂等不重写 |
| V10 | 熔断回合（`should_retry=False`） | 该回合在 trace 中可见（`ok=False`），UI 与 trace 一致 |

### 9.2 回归

- `tests/test_react_planner.py`：熔断文案、hint 签名变化（op 参数）需同步；
- `tests/test_plan_adjust.py`：`modify`/`add` 新增字段透传；
- `tests/test_six_layers.py` / `tests/test_session_space.py` / `tests/test_plugin_system.py`：schema 新增默认字段不影响既有路径；
- 端到端复现：用本会话同款 goal（生成 PPT 写 N 个文件）跑一次，断言 slides 目录产物齐全、无重复追加步骤。

---

## 10. 实施顺序与验收

| 批次 | 内容 | 验收 |
|------|------|------|
| 批次 A（P0-1+P0-2） | 产物声明 + 校验器 + Executor 闸门 | V1/V2/V3/V7 过；旧功能回归绿 |
| 批次 B（P0-3） | ReAct 收敛/兜底改造 | 端到端"假写"不再标 done；分析步骤行为不变 |
| 批次 C（P0-4） | 账本 op 化 + 软硬分层 | V5/V6 过；本会话场景不再预熔断 |
| 批次 D（P0-5） | trace 实时落盘 + 元数据 + 中断兜底 | V9/V10 过；手动终止后 trace 完整、meta=interrupted、resume 有中断提示 |
| 批次 E（P1-1） | 无进展护栏 | V8 过；s13/s14 型重复不再发生 |
| 批次 F（P2） | 配置化开关 | 开关可热关；`realtime_flush=false` 回退旧冲刷语义 |

每批独立可回滚（改动集中在 2-3 文件/批），建议按 A→F 顺序合入，A 批合入即可单点验证"假成功"是否消失；D 批（P0-5）使 trace 首次成为可信审计依据，可与 A 并行开发（改动面不重叠：A/B/C 动"完成判定"，D 动"落盘时序"）。

## 11. 兼容性与风险

| 项 | 说明 |
|----|------|
| 向后兼容 | `Step.expected_artifacts` / `StepResult` 新字段全部带默认值；未声明产物的步骤行为与旧版逐字节一致 |
| 误伤面 | 产物校验**只作用于声明了 expected_artifacts 的步骤**——分析型步骤零影响；声明义务由 planner prompt 引导，模型漏声明的最坏情况 = 回到旧行为（不更差） |
| 成本 | 校验为纯只读文件 stat/glob，无 LLM 调用、无额外网络 |
| 语义变化 | 最大变化是"空转的生产型步骤现在会 FAILED 并 replan"——这正是目标；配合护栏（P1-1）不会造成 replan 风暴 |
| I/O 频率 | P0-5 逐回合 append：量级 = LLM 轮数（本会话 111 行/17 分钟），可忽略；`realtime_flush=false` 可回退 |
| 中断语义 | `finish("interrupted")` 与正常 `done` 区分；resume 侧只多一条提示前缀，不改变执行逻辑 |
| 回滚 | 每批次独立 commit；`completion.artifact_check=false` 可整体关闭后置闸门回到旧语义 |

## 12. 遗留/后续项（本期不做）

- 产物**数量**断言（如"恰好 19 个"）：`expected_artifacts` 增加 `{"path": "slides/*.md", "min_count": 19}` 对象语法；
- `strict_trace` 模式（校验 trace 中确有对产物的成功写操作，防预置文件误过）；
- 跨 step 的"同产物"依赖：B step 依赖 A step 产出的文件时，由 planner 在 `depends_on` 表达（已有机制），本期仅补 prompt 引导。

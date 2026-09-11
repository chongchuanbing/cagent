# 学习笔记：Agent 长程任务断点续传

> 来源：《Agent 长程任务断点续传：从框架 Checkpoint 到跨进程恢复的完整实践》
> 作者：绍清（淘天集团 - 直播技术团队）｜ 发布：大淘宝技术 2026-09-09
> 原文：https://mp.weixin.qq.com/s/Vy2HpOyr7wVmPTVWa3mGig
> 整理：2026-09-10，结合 cagent 框架现状做对照

---

## 1. TL;DR（一句话总结）

长程 Agent 任务中断后无法靠简单重试恢复，必须从断点续传。本文给出
**「框架层 Checkpoint + 上层任务调度」两层架构**：底层用框架的 Checkpoint
做进程内状态保存/恢复，上层用「任务表 + MQ 摘流」做跨进程（应用重启/机器下线）
的任务恢复协调。核心约束是**不改框架源码**，全部基于 Spring AI Alibaba(SAA)
已有的 Hook / CheckpointSaver / InterruptableAction 能力扩展。

---

## 2. 为什么需要断点续传（问题定义）

业务里有三类场景对中断恢复有强诉求：

| 场景 | 特征 | 恢复触发方 |
|------|------|-----------|
| 长程任务 | 多步骤、执行时间不可控 | 系统/用户 |
| A2A 多 Agent 协同 | 系统自治、无人值守 | **必须系统自动** |
| Human-in-the-loop | 需人工确认（权限/澄清） | 用户触发，可见可控 |

**关键认知**：长程任务上下文状态庞大、执行时间不可控，中断后**无法通过简单重试
恢复**。而稳定的「中断机制」本身是前提——若在合适的拦截点没把任务稳稳中断，就会
状态不一致，恢复时无法判断「已完成 / 待重试」的边界。

### 中断的三分类

| 类型 | 例子 | 协作性质 | 恢复手段 |
|------|------|---------|---------|
| 用户主动中断 | 取消、暂停 | 协作式 | 框架 Hook 检测 + 存 Checkpoint |
| 工具执行中断 | 权限不足、需澄清 | 协作式 | 框架 Hook 检测 + 存 Checkpoint |
| 系统级中断 | 应用重启、机器下线 | **非协作式** | 上层调度 + MQ 协调恢复 |

> 蓝绿发布 + 摘流等待是「避免中断发生」的思路（旧机器跑完长任务再回收），但成本高、
> 且覆盖不了非发布场景的中断。因此本文选**应用层断点续传**，成本更低、覆盖更广。

---

## 3. 整体架构：两层

```
┌─────────────────────────────────────────────┐
│  上层调度（跨进程）                            │
│   · 任务表 agent_task：记录请求/会话ID/状态     │
│   · MQ 摘流：下线前先停消费者，防孤儿任务        │
│   · 恢复流程：读 DB 状态 → 恢复框架 Checkpoint  │
│     → 从中断节点继续                          │
└───────────────────┬─────────────────────────┘
                    │ 触发
┌───────────────────┴─────────────────────────┐
│  框架层（进程内） Spring AI Alibaba            │
│   · CheckpointSaver：每节点执行完自动存点      │
│   · Hook 机制：注入关机检测/消息顺序修正/Mock   │
│   · threadId 关联 Checkpoint，无需显式传 ID    │
└─────────────────────────────────────────────┘
```

设计原则：**底层（框架 Checkpoint）管状态保存与恢复，上层（调度）管恢复触发方式**。
不同场景只是上层选不同的触发方。

---

## 4. 框架层机制详解

### 4.1 Checkpoint 保存时机（SAA 1.1.2.0）

- 每个节点**正常执行完毕**后自动保存 Checkpoint。
- **不保存**的只有两种：`__END__` 节点 和 **异常场景**。
- 恢复时通过 `RunnableConfig.threadId` 从 `BaseCheckpointSaver` 查最新 Checkpoint，
  自动恢复 state / 消息历史 / 当前节点 ID / 下一节点 ID，无需额外传 Checkpoint ID。

### 4.2 关键约束：不改框架源码

框架是公共依赖，改源码 = 维护私有 fork，升级成本极高。所有扩展都挂在框架已有能力上：
`InterruptableAction`、`Hook`、`CheckpointSaver`。

### 4.3 关机信号检测（GraphEnvironmentMonitor）

下线脚本 → HTTP 调 `/check/shutdown/signal` → 置内存共享变量 →
`GraphEnvironmentMonitor.isShutdownSignalDetected()` 返回 true。
正在跑的任务在**当前节点完成后**检测到信号，触发中断并存 Checkpoint（协作式、优雅）。

### 4.4 恢复时消息顺序 bug（已修复，issue #4662）

恢复执行时，用户新发的消息被插到消息列表**第一条**而非末尾，导致上下文错乱。
这是 SAA 框架 bug，新版本已修。**旧版本 workaround**：用 `ResumeMessageOrderHook`
在 `BEFORE_MODEL` 阶段把 `pendingUserMessage` 追加到列表末尾（见原文 3.4 代码）。
建议：优先升级框架，避免额外 Hook。

### 4.5 多工具调用的 Checkpoint 限制（核心痛点）

模型一次返回 3 个工具调用（toolA/B/C）时，框架在 **for 循环里顺序执行，中间不存
Checkpoint**。若 toolA 完、toolB 中崩溃，恢复后**三个全重跑**。

折中方案 —— `ToolRecordInterceptor` + Tool Mock：
- 检测到中断 → 未执行的工具返回 mock 响应（`status="error"`），该 mock 经 Checkpoint 持久化。
- 恢复时框架识别「哪些已有结果（含 mock）/ 哪些需重试」，已完成的不再重跑。

### 4.6 工具执行循环的中断跳过（HumanInterventionException）

需要中断（权限不足 / 表单 / 对话框确认）时：
1. `ToolInterceptor` 抛 `HumanInterventionException`；
2. `ObservationInterceptor` 捕获 → `agentInterruptManager.interrupt()` 设中断标记
   → SSE 推送中断通知（弹窗/表单）→ 返回 mock 响应；
3. 后续工具的 `ToolRecordInterceptor` 执行前检测到中断标记 → **直接跳过返回 mock**。

价值：用异常快速跳出工具循环，配合中断标记让后续工具自动跳过，最后 Checkpoint 保存
完整工具响应状态。恢复时按已有响应判断重试。

### 4.7 子图 threadId 后缀问题（多 Agent 嵌套）

父 Agent 调子 Agent（嵌套子图），若父子用**同一个 CheckpointSaver 实例**，框架自动给
子图 `threadId` 拼 `_subgraph_{agentName}` 后缀：
`plugin_xxx` → `plugin_xxx_subgraph_SUB_AGENT_A`。

影响：中断失效（用带后缀 ID 调 `interrupt()`）、状态恢复查不到、日志追踪错乱。

解法：在 `CheckpointAgentHook.beforeAgent()` 把原始 `threadId` 写进 metadata，
统一用 `ITool.getActualThreadId()` 取。优先级：`ACTUAL_THREAD_ID` → `conversationId`。

---

## 5. 上层调度：跨进程恢复

### 5.1 任务表 agent_task

复杂长程任务（尤其 A2A）用任务表记录 Agent 类型 / 业务键 / 会话 ID / 过程关键数据。
**简单单轮交互无需此表**，直接依赖框架 Checkpoint 即可。

### 5.2 MQ 摘流：防止关机机器消费新消息

核心问题：正在下线的机器若消费了恢复任务却没执行就关机，任务变「孤儿」。

解法：下线脚本里**先摘流 MQ，再停应用**。
`offline()` → `offline_mq()` 调 MQ 客户端 `allConsumersOffline` 接口 → Broker 把该
消费者移出消费组 → 后续消息分发到健康机器。摘流后、关机前已消费但没跑完的消息，因
状态仍为 `WORKING`，新实例启动后会经 MQ 重新消费 → 触发恢复。

### 5.3 恢复流程

外部 MQ 消息触发 → 读 DB 状态 → 从 Checkpoint 恢复框架状态 → 从中断节点继续。

---

## 6. 三个核心坑速查表

| # | 问题 | 根因 | 解法 | 是否需改框架 |
|---|------|------|------|------------|
| 1 | 恢复时消息顺序错乱 | SAA bug #4662 | 升级版本 / ResumeMessageOrderHook | 否（已修） |
| 2 | 多工具调用中间无法存 Checkpoint | for 循环内不落点 | ToolRecordInterceptor + Mock 响应 | **是（待框架侧）** |
| 3 | 子图 threadId 后缀致中断失效 | 同 Saver 实例自动加后缀 | Hook 记原始 ID 到 metadata | 否 |

后续：跟进框架侧「工具级粒度断点续传」、HITL 扩展机制完善（issue #4091）。

---

## 7. 对 cagent 的启示（重点对照）

cagent 当前已有相关基础设施，但**断点续传尚未体系化**。逐项对照：

| 文章机制 | cagent 现状 | 差距 / 可借鉴 |
|---------|------------|--------------|
| 每节点自动 Checkpoint | `storage/session.py` SessionRecorder 落盘 meta/plan/trace，但**ReAct 单步内无细粒度落点** | 可在 ReAct 循环每轮 Thought/Action 后增量落盘 observation |
| 关机信号检测 | 无 | 加 `GraphEnvironmentMonitor` 式信号量，在 `loop.py` 主循环节点边界检测 |
| 消息顺序修正 Hook | 无 before/after model Hook | 引入 Hook 点（参考 `events/emitter.py` 已有事件体系扩展为拦截点） |
| 多工具 Mock 跳过 | ReAct 单步通常单 Action，矛盾较轻 | 若支持一次多 Action，需 ToolRecordInterceptor 等价物 |
| 中断异常跳出 | 无 HumanInterventionException 等价 | 可在 `react.py` 用异常 + 中断标记实现 HITL |
| 子图 threadId 后缀 | 当前 ReactEngine 无嵌套子图 threadId，但多 Agent 编排将来会踩 | 提前在 `runtime/session_scope.py` 统一会话 ID 语义 |
| 上层 MQ 摘流 | CLI 本地运行，无分布式 | 部署形态若上云需「先摘流再停」模式 |

**最值得借鉴的三点**：
1. **不动框架源码**的扩展哲学 —— cagent 也应把「读取/中断语义」建在已有工具链与 Hook 上，
   而非新增工具抽象（与你定的工具设计原则一致：改造点放工具链本身、配置驱动）。
2. **协作式中断优于非协作式** —— 在所有「节点边界」留检测点，让任务在可控位置暂停，
   而非硬杀。这对 cagent 的 `loop.py` 主循环尤其重要。
3. **Checkpoint 粒度决定恢复质量** —— 节点级 > 整任务级；工具级（待框架）最优。cagent
   当前是「任务级 plan/trace 落盘」，可先补「step 级结论落盘」再视需要细化到 action 级。

---

## 8. 可复用模式清单（落地 checklist）

- [ ] 定义 `InterruptableAction` / 中断标记：让工具能在权限不足、需澄清时优雅中断
- [ ] 在 loop / react 节点边界加「关机/取消信号」检测点（协作式中断）
- [ ] Checkpoint 落盘时机：每 step 完成后 + 每轮 observation 后（增量）
- [ ] 恢复入口：用 session_id/threadId 关联 Checkpoint，自动 `initializeFromResume`
- [ ] HITL：异常 + SSE 通知（cagent 已有 `events` SSE 体系可复用）
- [ ] 多 Agent 嵌套时统一会话 ID 语义，避免子图后缀错乱
- [ ] 分布式部署：下线脚本先摘流（MQ/RPC）再停进程，防孤儿任务

---

## 9. 参考资料

- Spring AI Alibaba 源码：https://github.com/alibaba/spring-ai-alibaba
- 社区文档：https://java2ai.com/
- 消息顺序 bug：https://github.com/alibaba/spring-ai-alibaba/issues/4662
- HITL 扩展机制：https://github.com/alibaba/spring-ai-alibaba/issues/4091

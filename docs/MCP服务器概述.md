# MCP 服务器概述

## 一、背景与动机

飞书开放平台提供了数百个原始 API 端点。现有的大多数 AI 工具（包括飞书官方插件）对这些端点做一对一封装，将 API 细节直接暴露给模型——工具数量多、参数复杂、缺乏场景引导，使得模型在实际使用中容易出错。

与此同时，企业级工具面向团队协作设计，在个人使用场景中存在大量冗余能力（多人忙闲查询、群组管理、审批流程等），并假设工具的调用者有足够的飞书领域知识来操作细粒度 API。

本项目以**单用户个人助理**为核心场景，针对性地重新设计了工具接口和调用逻辑，目标是：

- 以最少的工具数量覆盖个人日常需求（日程、任务、消息、文档）
- 自动处理 API 的复杂性，让模型只需表达"想做什么"
- 支持任何 AI Agent 以 MCP 协议持久化接入飞书，成为可通过飞书消息随时触达的个人助理

---

## 二、使用层级

服务器分三个渐进式使用层级，按需接入：

| 层级 | 能力 | 所需授权 |
|---|---|---|
| 消息通知 | 发送消息、搜索用户，AI 作为通知 bot | App Token（无需 OAuth） |
| 双向交互 | 收发消息、卡片交互、定时任务，AI 成为可回话的个人助理 | App Token（无需 OAuth） |
| 完整功能 | 上述全部 + 日历、任务、文档读写 | OAuth（设备码流程，自动引导） |

每一层级是上一层级的严格超集——可从最小配置启动，在需要时再追加授权。

---

## 三、工具集设计

### 设计原则

**精简而非完整**：对标飞书官方插件的 16+ 日历工具、12+ 任务工具，本服务器分别将其精简为 4 个和 8 个。工具数量的减少降低了模型的选择负担，同时通过工具内部的逻辑组合维持了完整强大的功能覆盖。

**合并同质操作**：将"列出"和"搜索"合并为同一工具（通过参数区分），将"完成"和"取消完成"合并为 `patch` 的 `completed` 布尔参数，减少模型需要学习的工具接口数量。

**内化 API 复杂性**：飞书 API 中一些繁琐的多步操作（如修改重复日程的单个实例需要先实例化）由工具内部自动处理，对模型透明。

### 工具列表

**授权**

| 工具 | 说明 |
|---|---|
| `feishu_auth_status` | 检查当前授权状态及已授权模块 |
| `feishu_auth_init` | 启动 OAuth 设备码流程，最多等待 10 分钟；未授权时自动向所有者发送授权卡片 |
| `feishu_auth_whoami` | 返回已授权用户的 open_id 和姓名 |
| `feishu_auth_issue_token` | 发放时效性代理 token，供第三方脚本安全调用飞书 API（见授权共享逻辑） |

**人员**

| 工具 | 说明 |
|---|---|
| `feishu_people_search` | 按姓名搜索用户，返回 open_id，供其他工具使用 |

**日历**

| 工具 | 说明 |
|---|---|
| `feishu_calendar_create` | 创建日程（支持地点、提醒、重复规则） |
| `feishu_calendar_list` | 列出/搜索日程；默认范围：本周一至下周日 |
| `feishu_calendar_patch` | 修改日程；自动处理重复日程实例物化 |
| `feishu_calendar_delete` | 删除日程；自动处理重复日程实例物化 |

**任务**

| 工具 | 说明 |
|---|---|
| `feishu_task_create` | 创建任务（自动分配给当前用户） |
| `feishu_task_list` | 列出我的任务，或指定任务清单中的任务 |
| `feishu_task_patch` | 修改标题、截止日期、完成状态、负责人 |
| `feishu_task_delete` | 删除任务 |
| `feishu_task_list_lists` | 列出所有任务清单 |
| `feishu_task_create_list` | 创建任务清单 |
| `feishu_task_patch_list` | 重命名任务清单 |
| `feishu_task_delete_list` | 删除任务清单 |

**消息**

| 工具 | 说明 |
|---|---|
| `feishu_im_send` | 发送文本、富文本、互动卡片或文件/图片 |
| `feishu_im_read` | 读取会话近期消息 |
| `feishu_im_search` | 按关键词搜索消息 |
| `feishu_im_fetch_resource` | 下载消息附件到本地 |
| `feishu_im_patch` | 编辑已发送的消息 |
| `feishu_im_watch` | 阻塞等待新消息、卡片回调或定时事件 |

**互动卡片**

| 工具 | 说明 |
|---|---|
| `feishu_im_send_confirm` | 发送确认/取消对话框 |
| `feishu_im_send_form` | 发送表单卡片（支持密码输入框） |
| `feishu_im_send_progress` | 发送多步骤进度追踪卡片 |
| `feishu_im_patch_progress` | 更新进度卡片到下一步 |

**定时任务**

| 工具 | 说明 |
|---|---|
| `feishu_schedule_create` | 创建一次性或周期性（cron）提醒 |
| `feishu_schedule_list` | 列出待触发的定时任务 |
| `feishu_schedule_delete` | 删除定时任务 |

**文档**

| 工具 | 说明 |
|---|---|
| `feishu_doc_search` | 按关键词搜索文档和 Wiki |
| `feishu_doc_fetch` | 获取文档内容（纯文本 + 块列表） |
| `feishu_doc_create` | 新建文档 |
| `feishu_doc_append` | 追加块（段落、标题、列表、代码、图片等） |
| `feishu_doc_patch` | 编辑或替换已有块 |
| `feishu_doc_delete` | 删除块 |
| `feishu_doc_delete_file` | 从云盘永久删除整个文档 |

---

## 四、关键设计决策

### 重复日程实例物化

飞书 Task v2 和 Calendar API 对重复日程的单个实例修改有特殊要求：直接 PATCH 会影响整个序列的所有未来实例。

本服务器在修改或删除重复日程的特定实例前，自动执行物化流程：

1. 检测 `event_id` 后缀是否为时间戳（长度 > 2 表示特定实例）
2. 向父事件 POST 参与者，携带 `instance_start_time_admin` 参数，触发实例物化
3. 物化完成后再对该实例执行 patch/delete

这使得"修改下周三的例会地点而不影响其他周"这类操作对模型完全透明，无需额外引导。

### 列表与搜索的统一

日历 `list` 工具同时支持时间范围和关键词两种查询模式：

- **仅时间范围**：调用 `instance_view` API，返回展开的单个实例（含地点信息），硬限制 40 天
- **仅关键词**：调用 `calendarEvent.search` API，返回事件模板
- **同时指定**：交叉引用，取 search 结果的父事件 ID 与 instance_view 结果的交集，保留有地点信息的实例

这一设计修复了飞书官方 search API 不返回地点信息的问题。

### 输出格式精简

- 重复日程按父事件分组，压缩展示
- 事件 ID 做前缀压缩（取中段），减少 token 消耗
- 已取消状态（`status: 'cancelled'`）的实例在客户端过滤，不传给模型

---

## 五、Watch Loop 模式

`feishu_im_watch` 是 Agent 模式的核心：单次阻塞调用可返回三类事件：

- **用户消息**：飞书聊天中的新消息
- **卡片回调**：用户点击互动卡片按钮后的回调（含表单数据）
- **定时事件**：通过 `feishu_schedule_create` 注册的提醒到期触发

Agent 接收事件 → 处理 → 通过 `feishu_im_send` 回复 → 立即重新调用 `feishu_im_watch`，形成持续的事件驱动循环。无需 Webhook，无需轮询，无需额外进程。

---

## 六、Skill 文件系统

服务器将各模块的 `SKILL.md` 文件以 MCP prompts 的形式对外暴露，可按需注入上下文，向模型提供特定模块的使用规范和边界说明：

| Prompt | 内容 |
|---|---|
| `lark-mcp-guide` | 概览：标识符、授权、所有模块 |
| `feishu-calendar` | 日历操作指南 |
| `feishu-task` | 任务操作指南 |
| `feishu-people` | 用户搜索和 open_id 解析 |
| `feishu-im` | 消息收发、文件、编辑 |
| `feishu-interactive-cards` | 卡片使用场景和规则 |
| `feishu-watch-loop` | Watch loop 模式规范 |
| `feishu-doc` | 文档操作指南 |

---

## 七、与官方实现对比

飞书官方目前提供三种 AI 接入方式，本服务器与它们的定位和能力有显著差异。

### 官方实现概述

| 实现 | 仓库 | 定位 |
|---|---|---|
| **lark-openapi-mcp** | larksuite/lark-openapi-mcp | 飞书 OpenAPI 的完整 MCP 封装，覆盖 50+ 业务域、1271 个工具 |
| **openclaw-lark** | larksuite/openclaw-lark | 面向企业场景的多模态插件，~31 个多动作工具，覆盖日历/任务/IM/Bitable/Sheets/Wiki/Drive/Chat，含流式卡片和权限策略 |
| **lark-cli** | larksuite/cli | 面向开发者和 Agent 的 CLI，23 个结构化 skill + 200+ 精选命令 + WebSocket 事件监听 |

### 功能对比

| 维度 | **本服务器** | lark-openapi-mcp | openclaw-lark | lark-cli |
|---|---|---|---|---|
| **目标场景** | 单用户个人助理 | 企业全 API 覆盖 | 企业团队协作 | 开发者 / Agent |
| **工具总数** | 37个简单工具 | 1271 | ~31 个多动作工具 | 23 skills + 200+ 命令 |
| **覆盖域** | 日历/任务/IM/文档/人员/授权 | 50+ 全域 | 日历/任务/IM/Bitable/Sheets/Wiki/Drive/Chat | 日历/任务/IM/文档 |
| **日历动作数** | 4 | 41 | 15（9事件+3日历+2参会人+1忙闲） | 1 skill |
| **任务动作数** | 8 | 74 | 21（5任务+6清单+5分区+2子任务+3评论） | 1 skill |
| **消息动作数** | 6 | 66 | 9（2消息+3读取+1附件+2群聊+1成员） | 1 skill |
| **文档动作数** | 7 | 19 | 15（7文件+5评论+2媒体+1搜索） | 1 skill |
| **重复日程修改** | 自动实例化，对模型透明 | 需调用方处理 | 需调用方处理 | 需调用方处理 |
| **列表与搜索** | 合并为单一工具 | 分开接口 | 分开接口 | 分开接口 |
| **Watch Loop** | ✓ 内置，消息/卡片/定时统一 | ✗ | ✗ | ✓ WebSocket 事件订阅 |
| **互动卡片** | ✓ 确认框、表单、进度卡片，支持自定义卡片 | ✗ 仅普通卡片消息 | ✓ 含流式卡片 | ✗ |
| **卡片回调处理** | ✓ 在 watch loop 中统一处理 | ✗ | ✓ | ✗ |
| **定时 cron 任务** | ✓ | ✗ | ✗ | ✓ 事件订阅 |
| **Token 代理共享授权** | ✓（见授权共享逻辑） | ✗ | ✗ | ✗ |
| **授权状态持久化及refresh** | ✓ 加密存储，跨会话有效 | ✗ 每次初始化 | ✓ | ✓ |
| **卡片注入防护** | 建议过滤（见隐写原理） | 未缓解 | 同样存在漏洞 | 不适用 |
| **Skill 文件系统** | ✓ 8 个模块级 SKILL.md | ✗ | ✓ | ✓ 23 个 skill |

### 小结

- **lark-openapi-mcp**：追求完整性，1271 个工具覆盖所有 API，但缺少 watch loop 和互动卡片，不适合持续在线的 Agent 模式。
- **openclaw-lark**：覆盖面最广（含 Bitable、Sheets、Wiki、Chat 管理），支持流式卡片和互动回调，第三方脚本无法共享授权。
- **lark-cli**：开发者友好，有 WebSocket 事件订阅和完整原始 API 访问，但卡片交互能力弱，无个人日历/任务场景的深度优化。
- **本服务器**：以 watch loop 为核心构建持续在线的 Agent，互动卡片和 cron 调度使 AI 可在飞书中主动发起交互；Token 代理机制使第三方脚本无缝且可控地共享授权，这是其他实现均不具备的能力。

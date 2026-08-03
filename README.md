# astrbot_plugin_isittrue（是真的吗）

![logo](logo.png)

群聊事实核查小工具。@机器人说出你想核实的事情，或**引用一条消息**（支持文本 / 图片 / 合并转发），AI 自动判断真假，返回 `✅ 真的喵` / `❌ 假的喵` / `⚠️ 布吉岛` + 中文解释。

移植自 [@花火](https://github.com/yhArcadia) 的 `is-it-true.js`，无需额外 API Key，即装即用。

## 触发方式

插件按以下优先级判断是否触发（命中即停）。触发词由 `trigger_phrases` 配置，默认是 `真的吗`：

| # | 触发路径 | 条件 | 受开关控制 | 核查对象 |
|---|---------|------|-----------|---------|
| 1 | **@机器人 + 触发词** | 消息 @ 了机器人 **且** 文本命中触发词 | 否（始终生效） | 引用消息（若有），否则去掉 @ 和触发词后的本句 |
| 2 | **引用 + 触发词** | 引用了一条消息，且本句文本命中触发词 | 否（始终生效） | 被引用的原消息；本句剩余文字作为「用户补充」 |
| 3 | **结尾监听** | 本句以任一触发词结尾，兼容 `？` / `?` | `listen_suffix`（默认关） | 本句去掉结尾触发词后的内容 |
| 4 | **开头监听** | 本句以任一触发词开头 | `listen_prefix`（默认关） | 本句去掉开头触发词后的内容 |

说明：
- **仅 @机器人但不带触发词不会触发**（如 `@机器人 实时金价` 会被忽略），避免拦截无关消息。
- 提取内容时**优先取引用 / 合并转发**；即使用路径 1/3/4，只要附带了引用，核查的就是引用内容，本句可写补充说明（例如「重点看第三条」）。
- 英文触发词按边界匹配，避免 `true` 误伤 `trust` 这类子串。
- 支持文本与图片（图片需 Provider 支持多模态，且 `enable_vision` 开启）。
- 命中后统一流程：冷却检测 → 提取内容 →（可选）联网搜索 → 调用 Provider → 返回判定 + 中文解释。

### 判定结果

模型首行返回 `true` / `false` / `unknown`（也兼容部分中文写法），插件展示为：

| 模型输出 | 默认展示 | 含义 |
|---------|---------|------|
| `true` | `✅ 真的喵` | 内容属实 |
| `false` | `❌ 假的喵` | 内容不实 |
| `unknown` | `⚠️ 布吉岛` | 无法核实（主观观点 / 预测 / 缺乏可验证事实 / 缺资料） |

展示文案可通过 `true_label` / `false_label` / `unknown_label` 自定义。

### 示例

- `@机器人 太阳从西边升起真的吗`
- 引用一条消息，回复：`真的吗` / `这真的吗？` / `真的吗 重点看第三条`
- （开启结尾监听）`地球是平的真的吗？`
- （开启开头监听）`真的吗 地球是平的`
- （自定义 `trigger_phrases=["求证"]` 并开启结尾监听）`今天停课求证？`

## 配置项

| 字段 | 说明 | 默认 |
|------|------|------|
| `cooldown` | 用户冷却时间（秒），按 user_id 分别计时 | 10 |
| `trigger_phrases` | 检测触发词列表，可在 WebUI 每行添加一个 | `["真的吗"]` |
| `listen_suffix` | 监听触发词结尾的消息（无需 @） | false |
| `listen_prefix` | 监听触发词开头的消息（无需 @） | false |
| `group_blacklist` | 群黑名单（填群号） | `[]` |
 | `provider_id` | 事实核查模型 Provider ID；**留空=当前默认 Provider** | `""` |
 | `provider_fallbacks` | 模型回退链（Provider ID 列表）：主模型失败后按顺序尝试，每个模型先带图再剥图；留空=只用主模型 | `[]` |
 | `enable_vision` | 是否启用图片分析（需多模态） | true |
| `enable_web_search` | 启用联网搜索增强（见下） | **false** |
| `search_timeout` | 联网搜索超时（秒） | 30 |
| `max_content_chars` | 待核文本最大字符数（超长截断） | 2500 |
| `max_search_chars` | 搜索资料写入 prompt 的上限 | 2000 |
| `max_forward_depth` | 合并转发最大嵌套层数（硬上限 8） | 4 |
| `max_forward_nodes` | 合并转发最大节点数（硬上限 100） | 40 |
| `max_forward_fetch` | 远程 get_forward_msg 次数（硬上限 20，含嵌套；重复 id 跳过） | 8 |
| `max_forward_images` | 合并转发最多提取图片数（硬上限 20） | 8 |
| `true_label` / `false_label` / `unknown_label` | 三态展示文案 | 真的喵 / 假的喵 / 布吉岛 |
| `system_prompt` | 事实核查系统提示词 | 见默认值 |

## 联网搜索增强（可选）

默认走 `provider.text_chat` 裸调用，**不经过 Agent / function-calling**，因此模型**不会自动触发 MCP 工具**，只能凭自身知识判断；时效内容容易答错或返回「布吉岛」。

开启 `enable_web_search` 后，插件会在调模型前：

1. 从待核文本 / 用户补充 / 图片中提炼可搜索的核心主张（长文会先抽取）
2. 按 `search_provider` 选渠道检索（见下表），把结果写入结构化 prompt 的「参考资料」段

### 搜索渠道（`search_provider`，默认 `auto`）

| 渠道 | 行为 |
|------|------|
| `auto` | 按框架配置的 `websearch_provider` 优先，失败自动降级：Tavily 多 Key 轮换 + failover（401/403/429/432 自动换 Key）→ 其他框架内置工具（bocha / baidu_ai_search / brave / firecrawl）→ Anysearch 兜底 |
| `tavily` | 直连 Tavily `/search`，自动读取框架 `provider_settings.websearch_tavily_key`（多 Key round-robin），无需手动填 Key |
| `anysearch` | 优先已注册的 `anysearch_search` 工具；工具不可用时用内置 HTTP 客户端直连 Anysearch 并尝试读取 anysearch 插件配置中的 `api_key` |
| `bocha` / `baidu_ai_search` / `brave` / `firecrawl` | 调用框架内置对应搜索工具 |
| `none` | 关闭联网搜索 |

- **不是硬依赖**：未安装 `astrbot_plugin_anysearch` 时本插件仍可正常加载与使用
- **默认关闭**：需要时效增强时再手动打开（`enable_web_search`）
- **纯图片场景**：会先让多模态模型从图中提取一句可搜索关键词，再检索
- **视觉降级**：模型不支持图片（image_url 400）时，自动剥图重试纯文本判定并在备注中提示
- **兜底**：全部渠道失败 / 超时 / 无结果时自动回退「仅凭模型知识」，并在备注中提示

## 模型回退（可选）

默认只用一个模型（`provider_id` 或默认 Provider）。配置 `provider_fallbacks` 后可实现多模型容灾：

- 主模型调用失败（网络 / 400 / 限流等）→ 自动按列表顺序切换下一个 Provider ID
- 每个模型依次尝试「带图」→「剥图纯文本」两级降级（模型不支持图片时）
- 全部模型都失败才提示"判断失败"
- Provider ID 示例：`go/deepseek-v4-flash`、`st/sensenova-6.7-flash-lite` 等（以 AstrBot 设置页为准）

## 引用 / 图文 / 合并转发

| 场景 | 行为 |
|------|------|
| 引用文本 | 核查引用内容；本句剩余文字作为「用户补充」 |
| 引用图文 | 文本 + 图片一并送模型（需 `enable_vision`） |
| 当前消息带图带字 | 一并提取；触发词会从文本中剥离 |
| 合并转发（Nodes / Forward） | 尽量展开节点文本与图片；失败会在备注中说明 |
| 嵌套转发 | 框架 `extract_quoted_message_*` 会对引用里的转发做有限跳数展开；当前消息里的裸 Forward 走 OneBot `get_forward_msg` 尽力展开 |
| 恶意深层嵌套 | 插件侧有硬预算：深度 / 节点数 / 远程拉取次数 / 图片数 / 字符数，并做 forward id 去重防环；超限截断并在备注提示，不拖死 bot |

## 错误处理

| 错误类型 | 用户提示 |
|---------|---------|
| 图片内容审核拦截（sensitive / 1026） | 图片内容被AI服务商安全审核拦截，无法判断，请更换图片后重试 |
| 频率限制（429 / rate_limit / quota） | AI服务当前繁忙，请稍后重试 |
| 超时（timeout） | 判断超时，请稍后重试 |
| 内容过长（context_length） | 内容过长，超出AI处理限制，请精简后重试 |
| 其他异常 | 判断失败，请稍后重试 |

## 安装

将整个 `astrbot_plugin_isittrue` 目录放到 AstrBot 的 `data/plugins/`（或 `data/addons/plugins/`）下，在 WebUI 插件管理中重载即可。

要求 AstrBot **>= 4.0.0**（引用消息解析能力）。

## 文件结构

```
astrbot_plugin_isittrue/
├── metadata.yaml        # 插件元信息
├── _conf_schema.json    # 可视化配置 schema
├── main.py              # 插件主逻辑
├── logo.png             # 插件图标
└── README.md
```

## 更新日志

### v1.2.1

- **fix/security**: 合并转发展开增加硬兜底——最大嵌套层、节点数、远程拉取次数、图片数、字符数，重复 forward id 跳过，超限截断并写备注

### v1.2.0

- **feat**: 重写 system / user prompt，明确能力边界（不假装联网、缺资料时倾向 unknown）
- **feat**: 引用消息 + 本句补充（如「重点看第三条」）一并送审
- **feat**: 合并转发（Nodes / Forward）尽力展开图文
- **feat**: 长文主张抽取 + 搜索词生成；`max_content_chars` / `max_search_chars` 截断
- **feat**: 更稳健的 true/false/unknown 解析（兼容中文与常见包装）
- **feat**: 恢复 `group_blacklist`；`provider_id` 默认留空走当前默认模型
- **feat**: 可自定义三态展示文案
- **feat**: 联网搜索与 anysearch 彻底解耦——优先工具，其次内置 HTTP 客户端，无硬依赖
- **fix**: 触发词边界匹配更精准（英文词避免子串误伤）
- **chore**: `astrbot_version` 提升为 `>=4.0.0`

### v1.1.1

- **fix**: 安装/加载不再硬依赖 `astrbot_plugin_anysearch`。未安装时插件可正常启用；仅在开启联网搜索时再懒加载 Anysearch，失败则自动回退到模型判断

### v1.1.0

- **feat**: 联网搜索增强 — 支持通过 `astrbot_plugin_anysearch` 联网检索后再核查
- **feat**: 纯图片场景自动提取搜索关键词
- **feat**: 友好错误提示 — 按 LLM 错误类型给出针对性提示
- **fix**: 修正判定结果文案
- **docs**: 重写 README

### v1.0.0

- 初始版本 — 群聊事实核查插件，支持 @触发 / 引用触发 / 前后缀监听 / 图片分析
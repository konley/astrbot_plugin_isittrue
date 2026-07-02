# astrbot_plugin_isittrue（是真的吗）
[
AstrBot 版「是真的吗」插件，移植[@渔火](https://github.com/yhArcadia)的 `is-it-true.js`。

群聊事实核查小工具。@机器人说出你想核实的事情，或**引用一条消息**（支持文本 / 图片），AI 自动判断真假，返回 `✅ 真的` / `❌ 假的` / `⚠️ 布吉岛` + 中文解释。无需额外 API Key，即装即用。

## 与原 JS 的关键差异

- **无需在本插件里填写 LLM API Key**。默认使用 `provider_id` 指定的 AstrBot Provider（当前默认 `openai/gpt-5.5`），找不到时回退到当前默认 Provider。
- 冷却使用内存字典实现（不依赖 Redis）。
- 配置项通过 AstrBot 可视化配置面板（`_conf_schema.json`）填写。

## 触发方式

插件按以下优先级判断是否触发（命中即停）。触发词由 `trigger_phrases` 配置，默认是 `真的吗`：

| # | 触发路径 | 条件 | 受开关控制 | 核查对象 |
|---|---------|------|-----------|---------|
| 1 | **@机器人 + 触发词** | 消息 @ 了机器人 **且** 文本包含任一触发词 | 否（始终生效） | 引用消息（若有），否则去掉 @ 和触发词后的本句内容 |
| 2 | **引用 + 触发词** | 引用了一条消息，且本句文本包含任一触发词 | 否（始终生效） | 被引用的原消息 |
| 3 | **结尾监听** | 本句以任一触发词结尾，兼容 `？` / `?` | `listen_suffix`（默认关） | 本句去掉结尾触发词后的内容 |
| 4 | **开头监听** | 本句以任一触发词开头 | `listen_prefix`（默认关） | 本句去掉开头触发词后的内容 |

说明：
- **仅 @机器人但不带触发词不会触发**（如 `@机器人 实时金价` 会被忽略），避免拦截无关消息。
- 提取内容时**始终优先取引用消息**，因此即便走路径 1/3/4，只要附带了引用，核查的就是引用内容。
- 支持文本与图片（图片需 Provider 模型支持多模态，且 `enable_vision` 开启）。
- 命中后统一流程：冷却检测 → 提取内容 → 调用 Provider → 返回判定结果 + 中文解释。

### 判定结果

模型首行返回 `true` / `false` / `unknown`，插件展示为对应文案：

| 模型输出 | 展示文案 | 含义 |
|---------|---------|------|
| `true` | `✅ 真的` | 内容属实 |
| `false` | `❌ 假的` | 内容不实 |
| `unknown` | `⚠️ 布吉岛` | 无法核实（主观观点 / 预测 / 缺乏可验证事实） |

### 示例

- `@机器人 太阳从西边升起真的吗`
- 引用一条消息，回复：`真的吗` / `这真的吗？`
- （开启结尾监听）`地球是平的真的吗？`
- （开启开头监听）`真的吗 地球是平的`
- （自定义 `trigger_phrases=["求证"]` 且开启结尾监听）`今天停课求证？`

## 配置项

| 字段 | 说明 | 默认 |
|------|------|------|
| `cooldown` | 用户冷却时间（秒），按 user_id 分别计时 | 10 |
| `trigger_phrases` | 检测触发词列表，可在 WebUI 每行添加一个 | ["真的吗"] |
| `listen_suffix` | 监听触发词结尾的消息（无需 @） | false |
| `listen_prefix` | 监听触发词开头的消息（无需 @） | false |
| `provider_id` | 事实核查模型 Provider ID | openai/gpt-5.5 |
| `enable_vision` | 是否启用图片分析（需模型支持多模态） | true |
| `enable_web_search` | 启用联网搜索增强（见下） | false |
| `search_timeout` | 联网搜索超时（秒），超时自动回退 | 30 |
| `system_prompt` | 事实核查系统提示词 | 见默认值 |

## 联网搜索增强（可选）

默认走 `provider.text_chat` 裸调用，**不经过 Agent / function-calling，因此不会触发 MCP 工具**，模型只能凭自身知识判断，时效性内容易答错或返回"布吉岛"。

开启 `enable_web_search` 后，插件会在调用模型前，先用 `astrbot_plugin_anysearch` 注册的 `anysearch_search` 工具联网检索关键词，把检索结果拼进 prompt 再交给模型核查。若工具未注册但 Anysearch 插件目录存在，会直接读取 Anysearch 插件配置并调用同一个 Anysearch API。

- **前置条件**：已安装并启用 `astrbot_plugin_anysearch`。`api_key` 仍按 Anysearch 插件自己的配置管理。
- **兜底机制**：若 Anysearch 未安装、未启用、搜索失败或超时，自动回退到"仅凭模型知识判断"的原有流程，不会报错中断。

## 安装

将整个 `astrbot_plugin_isittrue` 目录放到 AstrBot 的 `data/plugins/` 下，
在 WebUI 插件管理中重载即可。

## 文件结构

```
astrbot_plugin_isittrue/
├── metadata.yaml        # 插件元信息
├── _conf_schema.json    # 可视化配置 schema
├── main.py              # 插件主逻辑
└── README.md
```

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Forward, Image, Node, Nodes, Plain, Reply
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core.utils.quoted_message_parser import (
    extract_quoted_message_images,
    extract_quoted_message_text,
)

LOG_PREFIX = "[是真的吗]"
DEFAULT_TRIGGER_PHRASES = ("真的吗",)
DEFAULT_TRUE_LABEL = "✅ 真的喵"
DEFAULT_FALSE_LABEL = "❌ 假的喵"
DEFAULT_UNKNOWN_LABEL = "⚠️ 布吉岛"
DEFAULT_MAX_SEARCH_QUERIES = 2
# 联网搜索渠道（与 AstrBot 框架 provider_settings.websearch_provider 取值对齐）
SEARCH_PROVIDER_OPTIONS = (
    "auto",
    "tavily",
    "anysearch",
    "bocha",
    "baidu_ai_search",
    "brave",
    "firecrawl",
    "none",
)
# auto 模式的降级顺序：tavily 优先（多 Key 轮换+failover），anysearch 兜底
SEARCH_FALLBACK_ORDER = (
    "tavily",
    "bocha",
    "baidu_ai_search",
    "brave",
    "firecrawl",
    "anysearch",
)
# Tavily 等 Key 相关失败状态码：换下一个 Key 重试
_RETRYABLE_HTTP_STATUSES = frozenset({401, 403, 429, 432})
# 框架/客户端常见图片占位，不能当有效待核文本或搜索词
_IMAGE_PLACEHOLDER_RE = re.compile(
    r"^\s*(?:"
    r"\[(?:Image|image|IMAGE|图片|圖像|写真)\]"
    r"|【(?:图片|圖像)】"
    r"|\[(?:img|IMG)\]"
    r")+\s*$"
)
_IMAGE_PLACEHOLDER_TOKEN_RE = re.compile(
    r"\[(?:Image|image|IMAGE|图片|圖像|写真|img|IMG)\]|【(?:图片|圖像)】"
)
DEFAULT_SYSTEM_PROMPT = (
    "你是群聊事实核查助手。综合「待核主张」「原始文本」「图片」和「参考资料」作答；"
    "不要假装自己刚刚联网搜索，也不要编造不存在的链接或新闻。\n"
    "判断原则：\n"
    "1. 只核查可验证的事实主张；主观观点、价值判断、玩笑、预测优先 unknown。\n"
    "2. 若内容含多条主张，只核查最核心、最可验证的一条，并在解释中点明。\n"
    "3. 必须同时参考图片可见内容与文字；任一侧有关键事实信息都不可忽略。\n"
    "4. 涉及时效信息时：有可用参考资料或图文与公开报道高度一致，可判 true/false；"
    "仅当图文与资料均不足以支撑时才 unknown，不要仅因「没有官网全文」就 unknown。\n"
    "5. 媒体报道截图、部门回应等可作为佐证，但需在解释里写明依据与不确定点"
    "（例如日期可能有误、后续结论未出）。\n"
    "6. 图片看不清、无文字主张、纯主观内容 → unknown，并说明限制。\n"
    "7. 必须严格按以下格式输出，第一行只能是单个英文单词：\n"
    "第一行：true / false / unknown\n"
    "第二行起：中文解释，100字以内，说明依据与不确定点。"
)
DEFAULT_PLAN_PROMPT = (
    "你在为事实核查准备材料。请同时阅读用户文字与图片（若有），"
    "提炼最值得核查的一条核心主张，并决定是否需要联网搜索、搜什么。\n"
    "规则：\n"
    "1. 文字与图片都要看；图中有新闻标题/正文/图表时必须纳入主张。\n"
    "2. 忽略 [Image]、[图片] 等占位符，它们不是有效内容。\n"
    "3. 无任何可验证事实（纯情绪、玩笑、无信息图）时：NEED_SEARCH=no，CLAIM 可空，SEARCH=none。\n"
    "4. 搜索词要可直接用于搜索引擎：含人物/地点/事件/媒体名等实体，"
    "禁止输出「图片」「截图」「[Image]」「如图」等空词。\n"
    "5. 最多给出 2 个搜索词，用 | 分隔；不需要搜索时 SEARCH=none。\n"
    "6. 严格按下面四行输出，不要其它解释：\n"
    "CLAIM: <一句核心主张，可空>\n"
    "SEARCH: <词1> | <词2> 或 none\n"
    "NEED_SEARCH: yes 或 no\n"
    "NOTE: <可选一句说明>"
)

# 合并转发展开硬上限，防恶意深层嵌套 / 环
DEFAULT_MAX_FORWARD_DEPTH = 4
DEFAULT_MAX_FORWARD_NODES = 40
DEFAULT_MAX_FORWARD_FETCH = 8
DEFAULT_MAX_FORWARD_IMAGES = 8


class _ForwardBudget:
    """Shared expansion budget for nested forward/Nodes trees."""

    __slots__ = (
        "max_depth",
        "max_nodes",
        "max_fetch",
        "max_images",
        "max_chars",
        "nodes",
        "fetches",
        "images",
        "chars",
        "seen_forward_ids",
        "notes",
        "truncated",
    )

    def __init__(
        self,
        *,
        max_depth: int,
        max_nodes: int,
        max_fetch: int,
        max_images: int,
        max_chars: int,
    ) -> None:
        self.max_depth = max(1, int(max_depth))
        self.max_nodes = max(1, int(max_nodes))
        self.max_fetch = max(1, int(max_fetch))
        self.max_images = max(1, int(max_images))
        self.max_chars = max(200, int(max_chars))
        self.nodes = 0
        self.fetches = 0
        self.images = 0
        self.chars = 0
        self.seen_forward_ids: set[str] = set()
        self.notes: list[str] = []
        self.truncated = False

    def note(self, msg: str) -> None:
        if msg and msg not in self.notes:
            self.notes.append(msg)

    def stop(self, reason: str) -> None:
        self.truncated = True
        self.note(reason)

    def allow_depth(self, depth: int) -> bool:
        if self.truncated:
            return False
        if depth > self.max_depth:
            self.stop(f"合并转发嵌套超过 {self.max_depth} 层，已截断。")
            return False
        return True

    def allow_node(self) -> bool:
        if self.truncated:
            return False
        if self.nodes >= self.max_nodes:
            self.stop(f"合并转发节点超过 {self.max_nodes} 条，已截断。")
            return False
        self.nodes += 1
        return True

    def allow_fetch(self, forward_id: str) -> bool:
        if self.truncated:
            return False
        fid = (forward_id or "").strip()
        if not fid:
            return False
        if fid in self.seen_forward_ids:
            self.note("检测到重复/循环的合并转发 id，已跳过。")
            return False
        if self.fetches >= self.max_fetch:
            self.stop(f"合并转发远程展开超过 {self.max_fetch} 次，已截断。")
            return False
        self.seen_forward_ids.add(fid)
        self.fetches += 1
        return True

    def take_images(self, urls: list[str]) -> list[str]:
        kept: list[str] = []
        for url in urls:
            if self.images >= self.max_images:
                self.note(f"合并转发图片超过 {self.max_images} 张，已截断。")
                break
            if not url or url in kept:
                continue
            kept.append(url)
            self.images += 1
        return kept

    def take_text(self, text: str) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        if self.chars >= self.max_chars:
            self.note(f"合并转发文本超过 {self.max_chars} 字，已截断。")
            return ""
        remain = self.max_chars - self.chars
        if len(text) > remain:
            text = text[: max(0, remain - 12)].rstrip() + "\n…(已截断)"
            self.chars = self.max_chars
            self.note(f"合并转发文本超过 {self.max_chars} 字，已截断。")
            return text
        self.chars += len(text)
        return text

    def summary_note(self) -> str:
        return "；".join(self.notes)


class _InlineAnySearchClient:
    """Minimal Anysearch HTTP client. No hard dependency on other plugins."""

    endpoint = "https://api.anysearch.com/mcp"

    def __init__(self, api_key: str = "", timeout: int = 30) -> None:
        self.api_key = (api_key or "").strip()
        self.timeout = max(5, int(timeout or 30))

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def search(self, query: str, *, max_results: int = 5) -> str:
        import aiohttp

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "search",
                "arguments": {
                    "query": query,
                    "max_results": max_results,
                },
            },
        }
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.post(
                self.endpoint,
                json=payload,
                headers=self._headers(),
            ) as response:
                text = await response.text()
                if response.status >= 400:
                    raise RuntimeError(
                        f"AnySearch HTTP {response.status}: {text[:300]}"
                    )

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"AnySearch invalid JSON: {text[:300]}") from exc

        if "error" in data:
            error = data["error"]
            if isinstance(error, dict):
                message = error.get("message") or json.dumps(error, ensure_ascii=False)
            else:
                message = str(error)
            raise RuntimeError(f"AnySearch API error: {message}")

        result = data.get("result", {})
        content = result.get("content", [])
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    return str(item.get("text", ""))
        return json.dumps(result, ensure_ascii=False, indent=2)


@register(
    "astrbot_plugin_isittrue",
    "konley",
    "是真的吗——群聊事实核查小工具。@机器人说出你想核实的事情，或引用一条消息，AI 自动判断真假。无需额外 API，即装即用。",
    "1.5.0",
    "https://github.com/konley/astrbot_plugin_isittrue",
)
class IsItTrue(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        config = config or {}
        self.cooldown: int = max(0, int(config.get("cooldown", 10) or 0))
        self.listen_suffix: bool = bool(config.get("listen_suffix", False))
        self.listen_prefix: bool = bool(config.get("listen_prefix", False))
        self.enable_vision: bool = bool(config.get("enable_vision", True))
        self.provider_id: str = str(config.get("provider_id", "") or "").strip()
        raw_fallbacks = config.get("provider_fallbacks", []) or []
        if isinstance(raw_fallbacks, str):
            raw_fallbacks = [s.strip() for s in raw_fallbacks.split(",") if s.strip()]
        self.provider_fallbacks: list[str] = [
            str(p).strip() for p in raw_fallbacks if str(p).strip()
        ]
        self.trigger_phrases: tuple[str, ...] = self._normalize_trigger_phrases(
            config.get("trigger_phrases", DEFAULT_TRIGGER_PHRASES)
        )
        self.group_blacklist: set[str] = self._parse_id_set(
            config.get("group_blacklist", [])
        )
        self.enable_web_search: bool = bool(config.get("enable_web_search", False))
        self.search_provider: str = (
            str(config.get("search_provider", "auto") or "auto").strip().lower()
        )
        if self.search_provider not in SEARCH_PROVIDER_OPTIONS:
            self.search_provider = "auto"
        self._tavily_key_idx: int = 0  # Tavily 多 Key 轮换指针
        self.search_timeout: int = max(5, int(config.get("search_timeout", 30) or 30))
        self.max_search_queries: int = min(
            3,
            max(
                1,
                int(
                    config.get("max_search_queries", DEFAULT_MAX_SEARCH_QUERIES)
                    or DEFAULT_MAX_SEARCH_QUERIES
                ),
            ),
        )
        self.max_content_chars: int = max(
            200, int(config.get("max_content_chars", 2500) or 2500)
        )
        self.max_search_chars: int = max(
            200, int(config.get("max_search_chars", 2000) or 2000)
        )
        # 合并转发兜底：深度/节点/远程拉取/图片，全部硬夹紧
        self.max_forward_depth: int = min(
            8,
            max(
                1,
                int(
                    config.get("max_forward_depth", DEFAULT_MAX_FORWARD_DEPTH)
                    or DEFAULT_MAX_FORWARD_DEPTH
                ),
            ),
        )
        self.max_forward_nodes: int = min(
            100,
            max(
                1,
                int(
                    config.get("max_forward_nodes", DEFAULT_MAX_FORWARD_NODES)
                    or DEFAULT_MAX_FORWARD_NODES
                ),
            ),
        )
        self.max_forward_fetch: int = min(
            20,
            max(
                1,
                int(
                    config.get("max_forward_fetch", DEFAULT_MAX_FORWARD_FETCH)
                    or DEFAULT_MAX_FORWARD_FETCH
                ),
            ),
        )
        self.max_forward_images: int = min(
            20,
            max(
                1,
                int(
                    config.get("max_forward_images", DEFAULT_MAX_FORWARD_IMAGES)
                    or DEFAULT_MAX_FORWARD_IMAGES
                ),
            ),
        )
        self.true_label: str = (
            str(
                config.get("true_label", DEFAULT_TRUE_LABEL) or DEFAULT_TRUE_LABEL
            ).strip()
            or DEFAULT_TRUE_LABEL
        )
        self.false_label: str = (
            str(
                config.get("false_label", DEFAULT_FALSE_LABEL) or DEFAULT_FALSE_LABEL
            ).strip()
            or DEFAULT_FALSE_LABEL
        )
        self.unknown_label: str = (
            str(
                config.get("unknown_label", DEFAULT_UNKNOWN_LABEL)
                or DEFAULT_UNKNOWN_LABEL
            ).strip()
            or DEFAULT_UNKNOWN_LABEL
        )
        self.system_prompt: str = (
            str(
                config.get("system_prompt", DEFAULT_SYSTEM_PROMPT)
                or DEFAULT_SYSTEM_PROMPT
            ).strip()
            or DEFAULT_SYSTEM_PROMPT
        )
        self.plan_prompt: str = (
            str(
                config.get("plan_prompt", DEFAULT_PLAN_PROMPT) or DEFAULT_PLAN_PROMPT
            ).strip()
            or DEFAULT_PLAN_PROMPT
        )
        self._cooldowns: dict[str, float] = {}

    async def initialize(self) -> None:
        logger.info(
            f"{LOG_PREFIX} 插件已加载 v1.5.0 | triggers={list(self.trigger_phrases)} "
            f"web_search={self.enable_web_search} provider={self.search_provider} "
            f"vision={self.enable_vision} "
            f"fallback_providers={self.provider_fallbacks or '-'} "
            f"max_search_queries={self.max_search_queries} "
            f"blacklist={len(self.group_blacklist)}"
        )

    async def terminate(self) -> None:
        logger.info(f"{LOG_PREFIX} 插件卸载/重载")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_message(self, event: AstrMessageEvent):
        """Trigger paths:
        1) @bot + trigger phrase
        2) reply/quote + trigger phrase
        3) optional suffix/prefix listen
        """
        group_id = self._safe_group_id(event)
        if group_id and group_id in self.group_blacklist:
            return

        triggered, strip_keyword = self._match_trigger(event)
        if not triggered:
            return

        logger.info(
            f"{LOG_PREFIX} 触发 | strip={strip_keyword!r} "
            f"web_search={self.enable_web_search} vision={self.enable_vision} "
            f"group={group_id or '-'}"
        )

        user_id = str(event.get_sender_id() or "")
        now = time.time()
        last = self._cooldowns.get(user_id)
        if last is not None and self.cooldown > 0 and now - last < self.cooldown:
            remain = int(self.cooldown - (now - last)) + 1
            yield event.plain_result(f"检测冷却中，请 {remain} 秒后再试。")
            return

        bundle = await self._extract_bundle(event, strip_keyword)
        raw_text = bundle["text"]
        images = bundle["images"]
        raw_supplement = bundle["supplement"]
        source = bundle["source"]
        image_note = bundle["image_note"]

        text = self._sanitize_claim_text(raw_text)
        supplement = self._sanitize_claim_text(raw_supplement)

        logger.info(
            f"{LOG_PREFIX} 提取 | source={source} text_len={len(text)} "
            f"raw_text_len={len(raw_text or '')} images={len(images)} "
            f"supplement_len={len(supplement)} note={image_note!r}"
        )

        if not text and not images and not supplement:
            return

        provider = self._resolve_provider()
        if provider is None:
            yield event.plain_result(
                "当前未配置任何大模型提供商，请在 AstrBot 后台配置后再使用。"
            )
            return

        self._cooldowns[user_id] = now

        notes: list[str] = []
        if image_note:
            notes.append(image_note)
        if raw_text and not text:
            notes.append("引用/原文中的图片占位符已忽略，将主要依据图片与有效文字。")

        plan = await self._plan_verification(
            text=text, images=images, supplement=supplement, source=source
        )
        claim = plan.get("claim") or ""
        queries = list(plan.get("queries") or [])
        need_search = bool(plan.get("need_search"))
        plan_note = str(plan.get("note") or "").strip()
        plan_fallback = bool(plan.get("fallback"))

        logger.info(
            f"{LOG_PREFIX} 规划 | claim={claim!r} need_search={need_search} "
            f"queries={queries!r} fallback={plan_fallback} note={plan_note!r}"
        )
        if plan_note:
            notes.append(plan_note)

        search_block = ""
        if self.enable_web_search:
            if need_search and queries:
                blocks: list[str] = []
                per_limit = max(
                    400,
                    self.max_search_chars
                    // max(1, min(len(queries), self.max_search_queries)),
                )
                for q in queries[: self.max_search_queries]:
                    logger.info(f"{LOG_PREFIX} 准备联网搜索：{q!r}")
                    one = await self._web_search(q, event=event)
                    if one:
                        blocks.append(f"### 查询：{q}\n{one}")
                        logger.info(
                            f"{LOG_PREFIX} 搜索成功 query={q!r} len={len(one)} "
                            f"preview={one[:120]!r}"
                        )
                    else:
                        logger.info(f"{LOG_PREFIX} 搜索空结果 query={q!r}")
                if blocks:
                    search_block = self._truncate(
                        "\n\n".join(blocks), self.max_search_chars
                    )
                else:
                    notes.append("联网搜索未返回可用资料，已回退图文综合判断。")
                    logger.info(f"{LOG_PREFIX} 全部搜索空结果，回退兜底")
            elif need_search and not queries:
                notes.append("模型认为需要联网但未给出有效搜索词，已跳过搜索。")
                logger.info(f"{LOG_PREFIX} need_search 但无 queries，跳过搜索")
            else:
                notes.append("模型判定无需联网，已直接综合图文判断。")
                logger.info(f"{LOG_PREFIX} 规划判定无需搜索")
        else:
            logger.info(f"{LOG_PREFIX} 联网搜索未开启")

        if images and not self.enable_vision:
            notes.append("已关闭图片分析，仅依据文本判断。")

        prompt = self._build_user_prompt(
            source=source,
            text=text,
            images=images,
            supplement=supplement,
            search_block=search_block,
            notes=notes,
            claim=claim,
        )
        image_urls = images if self.enable_vision else []
        degrade_prompt = prompt
        if image_urls:
            degrade_notes = notes + [
                "图片无法被当前模型识别（视觉通道不可用），本次仅依据文字与参考资料判定。"
            ]
            degrade_prompt = self._build_user_prompt(
                source=source,
                text=text,
                images=[],
                supplement=supplement,
                search_block=search_block,
                notes=degrade_notes,
                claim=claim,
            )

        try:
            llm_resp, _label, degraded = await self._chat_with_fallback(
                prompt_with_images=prompt,
                prompt_without_images=degrade_prompt,
                image_urls=image_urls,
                system_prompt=self.system_prompt,
                stage="终判",
            )
            content = (llm_resp.completion_text or "").strip()
            if degraded:
                logger.info(f"{LOG_PREFIX} 模型返回（降级判定）：{content[:200]!r}")
            else:
                logger.info(f"{LOG_PREFIX} 模型返回：{content[:200]!r}")
        except Exception as e:  # noqa: BLE001
            err_msg = str(e)
            logger.exception(f"{LOG_PREFIX} 全部模型回退链调用失败: {err_msg}")
            yield event.plain_result(self._friendly_error(err_msg))
            return

        if not content:
            yield event.plain_result("模型未返回有效内容。")
            return

        yield event.plain_result(self._format_verdict(content))

    def _resolve_provider(self):
        if self.provider_id:
            provider = self.context.get_provider_by_id(self.provider_id)
            if provider is not None:
                return provider
            logger.warning(
                f"{LOG_PREFIX} 未找到 provider_id={self.provider_id!r}，回退默认 Provider"
            )
        return self.context.get_using_provider()

    def _resolve_providers(self) -> list[Any]:
        """模型回退链：主 provider → provider_fallbacks 依次 → 默认 provider 兜底，去重。"""
        order: list[Any] = []
        seen_ids: set[str] = set()

        def _add(p) -> None:
            if p is None:
                return
            try:
                pid = str(p.provider_config.get("id", ""))
            except Exception:  # noqa: BLE001
                pid = ""
            if pid in seen_ids:
                return
            seen_ids.add(pid)
            order.append(p)

        if self.provider_id:
            _add(self.context.get_provider_by_id(self.provider_id))
        for fallback_id in self.provider_fallbacks:
            if fallback_id and fallback_id not in seen_ids:
                _add(self.context.get_provider_by_id(fallback_id))
        _add(self.context.get_using_provider())
        if not order:
            order.append(self.context.get_using_provider())
        return order

    def _provider_label(self, provider) -> str:
        try:
            return (
                str(provider.provider_config.get("id", "")) or type(provider).__name__
            )
        except Exception:  # noqa: BLE001
            return type(provider).__name__

    async def _chat_with_fallback(
        self,
        *,
        prompt_with_images: str,
        prompt_without_images: str,
        image_urls: list[str],
        system_prompt: str,
        stage: str,
    ) -> tuple[Any, str, bool]:
        """按模型回退链调用 text_chat。

        每个模型依次尝试「带图」→「剥图」，全部失败才抛最后一个异常。
        返回 (llm_resp, provider_label, degraded)。
        """
        providers = self._resolve_providers()
        if not providers:
            raise RuntimeError("未配置任何可用的模型 Provider")
        last_err: Exception | None = None
        for provider in providers:
            label = self._provider_label(provider)
            # 1) 带图尝试
            try:
                resp = await provider.text_chat(
                    prompt=prompt_with_images,
                    image_urls=image_urls,
                    system_prompt=system_prompt,
                )
                logger.info(
                    f"{LOG_PREFIX} {stage} 调用成功 provider={label} vision={'on' if image_urls else 'off'}"
                )
                return resp, label, False
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning(
                    f"{LOG_PREFIX} {stage} provider={label} 带图调用失败：{e}"
                )
            # 2) 剥图重试（当前模型可能不支持图片）
            if image_urls:
                try:
                    resp = await provider.text_chat(
                        prompt=prompt_without_images,
                        image_urls=[],
                        system_prompt=system_prompt,
                    )
                    logger.info(f"{LOG_PREFIX} {stage} 剥图重试成功 provider={label}")
                    return resp, label, True
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    logger.warning(
                        f"{LOG_PREFIX} {stage} provider={label} 剥图调用失败：{e}"
                    )
        if last_err is not None:
            raise last_err
        raise RuntimeError(f"{stage} 全部模型回退链调用失败")

    def _build_user_prompt(
        self,
        *,
        source: str,
        text: str,
        images: list[str],
        supplement: str,
        search_block: str,
        notes: list[str],
        claim: str = "",
    ) -> str:
        raw_text = self._truncate(
            text or "（无有效文本，请主要依据图片判断）",
            self.max_content_chars,
        )
        claim_text = self._truncate(
            (claim or text or "").strip() or "（无明确主张，请综合图文判断）",
            min(500, self.max_content_chars // 2),
        )
        supplement = self._truncate(supplement, min(500, self.max_content_chars // 2))
        search_block = self._truncate(search_block, self.max_search_chars)

        parts = [
            "【任务】请综合文字与图片核查真实性，并严格按系统要求的格式输出。",
            f"【来源】{source}",
            f"【待核主张】{claim_text}",
        ]
        if supplement:
            parts.append(f"【用户补充】{supplement}")
        if text and claim and text.strip() != claim.strip():
            parts.append(f"【原始文本】{raw_text}")
        elif not claim:
            parts.append(f"【原始文本】{raw_text}")
        if images:
            if self.enable_vision:
                parts.append(
                    f"【图片】共 {len(images)} 张，已随请求附带；"
                    "请阅读图中文字、标题、图表，与主张、资料交叉核对。"
                )
            else:
                parts.append(f"【图片】共 {len(images)} 张，但当前未启用图片分析。")
        if search_block:
            parts.append(
                "【参考资料】以下为联网结果，可能含噪声或无关页；"
                "可作佐证但不要把搜索首页/无关聚合页当铁证。\n"
                f"{search_block}"
            )
        else:
            parts.append("【参考资料】无（请主要依据图文本身；依据不足则 unknown）")
        if notes:
            parts.append("【备注】" + "；".join(notes))
        parts.append(
            "【输出】第一行 true/false/unknown；第二行起中文解释（100字以内）。"
        )
        return "\n".join(parts)

    def _format_verdict(self, content: str) -> str:
        lines = content.strip().splitlines()
        if not lines:
            return content

        rest = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""
        verdict = self._parse_verdict_token(lines[0].strip())

        if verdict is None:
            for line in lines[:4]:
                verdict = self._parse_verdict_token(line.strip())
                if verdict is not None:
                    remaining = [ln for ln in lines if ln.strip() != line.strip()]
                    rest = "\n".join(remaining).strip()
                    break

        if verdict is None:
            lowered = content.lower()
            if re.search(r"\btrue\b|属实|为真|是真的", lowered) and not re.search(
                r"\bfalse\b|不实|为假|是假的", lowered
            ):
                verdict = "true"
            elif re.search(r"\bfalse\b|不实|为假|是假的|谣言", lowered):
                verdict = "false"
            elif re.search(r"\bunknown\b|无法核实|不确定|布吉岛", lowered):
                verdict = "unknown"

        if verdict is None:
            return content

        label = {
            "true": self.true_label,
            "false": self.false_label,
            "unknown": self.unknown_label,
        }[verdict]
        if rest:
            rest_lines = rest.splitlines()
            if (
                rest_lines
                and self._parse_verdict_token(rest_lines[0].strip()) == verdict
            ):
                rest = "\n".join(rest_lines[1:]).strip()
        return f"{label}\n{rest}" if rest else label

    @staticmethod
    def _parse_verdict_token(token: str) -> str | None:
        if not token:
            return None
        cleaned = token.strip().strip("`\"'“”‘’").lower()
        cleaned = re.sub(r"^[\s#*：:\-\d\.\)\]]+", "", cleaned).strip()
        cleaned = re.sub(
            r"^(判定|结论|结果|答案|verdict|answer)\s*[:：\-]?\s*",
            "",
            cleaned,
        ).strip()
        m = re.match(
            r"^(true|false|unknown|属实|不实|无法核实|不确定|真|假)"
            r"(?:\s|[（(【\[.,，。!！:：]|$)",
            cleaned,
        )
        if m:
            raw = m.group(1)
            return {
                "true": "true",
                "false": "false",
                "unknown": "unknown",
                "属实": "true",
                "不实": "false",
                "无法核实": "unknown",
                "不确定": "unknown",
                "真": "true",
                "假": "false",
            }.get(raw)

        compact = re.sub(r"[\s#*（）()【】\[\].,，。!！:：]", "", cleaned)
        return {
            "true": "true",
            "false": "false",
            "unknown": "unknown",
            "属实": "true",
            "为真": "true",
            "真": "true",
            "真的": "true",
            "不实": "false",
            "为假": "false",
            "假": "false",
            "假的": "false",
            "无法核实": "unknown",
            "不确定": "unknown",
            "未知": "unknown",
        }.get(compact)

    @classmethod
    def _is_image_placeholder(cls, text: str) -> bool:
        raw = (text or "").strip()
        if not raw:
            return False
        if _IMAGE_PLACEHOLDER_RE.fullmatch(raw):
            return True
        compact = re.sub(r"\s+", "", raw)
        if not compact:
            return False
        stripped = _IMAGE_PLACEHOLDER_TOKEN_RE.sub("", compact)
        return not stripped.strip()

    @classmethod
    def _sanitize_claim_text(cls, text: str) -> str:
        raw = (text or "").strip()
        if not raw:
            return ""
        if cls._is_image_placeholder(raw):
            return ""
        cleaned = _IMAGE_PLACEHOLDER_TOKEN_RE.sub(" ", raw)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if cls._is_image_placeholder(cleaned):
            return ""
        return cleaned

    @classmethod
    def _is_useless_search_query(cls, query: str) -> bool:
        q = (query or "").strip()
        if not q:
            return True
        if cls._is_image_placeholder(q):
            return True
        compact = re.sub(r"[\s\-_|｜]+", "", q).lower()
        if not compact:
            return True
        if compact in {
            "none",
            "n/a",
            "na",
            "null",
            "无",
            "无搜索",
            "不需要",
            "不需要搜索",
            "图片",
            "截图",
            "image",
            "images",
            "photo",
            "如图",
            "见图",
        }:
            return True
        if len(compact) <= 1:
            return True
        return False

    def _fallback_plan(
        self,
        *,
        text: str,
        images: list[str],
        supplement: str,
        reason: str = "",
    ) -> dict[str, Any]:
        """Rule fallback when LLM planning fails or returns garbage."""
        base = "\n".join(p for p in (text, supplement) if p).strip()
        base = re.sub(r"\s+", " ", base).strip()
        claim = base[:80] if base else ""
        queries: list[str] = []
        need_search = False
        note = reason

        if base and not self._is_useless_search_query(base):
            need_search = True
            queries = [base[:80]]
        elif images and self.enable_vision:
            # 有图无有效字：需要搜索，但把 query 留给规划失败后的空列表；
            # 调用方若 enable_web_search 会记 note；终判仍带图。
            need_search = True
            note = (note + "；" if note else "") + (
                "规划回退：仅有图片，终判将依赖视觉；无可靠搜索词则跳过联网"
            )
            # 尝试用极短通用描述不如不搜；保持 queries 空
            queries = []
        else:
            need_search = False
            if not claim and not images:
                note = (note + "；" if note else "") + "无有效图文可核查"

        return {
            "claim": claim,
            "queries": queries,
            "need_search": need_search,
            "note": note.strip("；"),
            "fallback": True,
        }

    async def _plan_verification(
        self,
        *,
        text: str,
        images: list[str],
        supplement: str,
        source: str,
    ) -> dict[str, Any]:
        """Joint text+image plan: claim + whether/what to search."""
        vision_urls = images[:3] if (images and self.enable_vision) else []
        # 纯短有效文本且无图：可直接当 claim/query，省一次规划调用
        if text and not supplement and not vision_urls and len(text) <= 80:
            q = re.sub(r"\s+", " ", text).strip()[:80]
            if not self._is_useless_search_query(q):
                plan = {
                    "claim": q,
                    "queries": [q],
                    "need_search": True,
                    "note": "",
                    "fallback": False,
                }
                logger.info(f"{LOG_PREFIX} 规划 | 短文本直通 claim={q!r}")
                return plan

        user_bits = [
            f"来源：{source}",
            f"文字：{text or '（无有效文字）'}",
        ]
        if supplement:
            user_bits.append(f"用户补充：{supplement}")
        if vision_urls:
            user_bits.append(f"图片：已附带 {len(vision_urls)} 张，请阅读图中信息。")
        elif images:
            user_bits.append(f"图片：有 {len(images)} 张但当前未启用视觉。")
        else:
            user_bits.append("图片：无")
        plan_prompt_text = "\n".join(user_bits)
        # 剥图版 prompt：视觉通道全部不可用时使用
        degrade_bits = [bit for bit in user_bits if not bit.startswith("图片：")]
        degrade_bits.append(f"图片：有 {len(images)} 张但视觉通道不可用，本次未附带。")
        degrade_prompt_text = "\n".join(degrade_bits)

        try:
            resp, _label, _degraded = await self._chat_with_fallback(
                prompt_with_images=plan_prompt_text,
                prompt_without_images=degrade_prompt_text,
                image_urls=vision_urls,
                system_prompt=self.plan_prompt,
                stage="规划",
            )
            raw = (resp.completion_text or "").strip()
            logger.info(f"{LOG_PREFIX} 规划原始返回：{raw[:240]!r}")
            parsed = self._parse_plan_response(raw)
            if parsed is not None:
                return parsed
            logger.warning(f"{LOG_PREFIX} 规划结果无法解析，走规则回退")
            return self._fallback_plan(
                text=text,
                images=images,
                supplement=supplement,
                reason="规划输出无法解析，已规则回退",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{LOG_PREFIX} 规划调用失败，走规则回退：{e}")
            return self._fallback_plan(
                text=text,
                images=images,
                supplement=supplement,
                reason=f"规划调用失败：{e}",
            )

    def _parse_plan_response(self, raw: str) -> dict[str, Any] | None:
        if not raw:
            return None

        claim = ""
        search_raw = ""
        need_raw = ""
        note = ""

        # 行协议
        for line in raw.splitlines():
            s = line.strip()
            if not s:
                continue
            m = re.match(
                r"^(CLAIM|SEARCH|NEED_SEARCH|NOTE)\s*[:：\-]\s*(.*)$",
                s,
                flags=re.I,
            )
            if not m:
                continue
            key = m.group(1).upper()
            val = m.group(2).strip()
            if key == "CLAIM":
                claim = val
            elif key == "SEARCH":
                search_raw = val
            elif key == "NEED_SEARCH":
                need_raw = val
            elif key == "NOTE":
                note = val

        # 宽松 JSON
        if not (claim or search_raw or need_raw):
            try:
                start = raw.find("{")
                end = raw.rfind("}")
                if start >= 0 and end > start:
                    data = json.loads(raw[start : end + 1])
                    if isinstance(data, dict):
                        claim = str(
                            data.get("claim") or data.get("CLAIM") or ""
                        ).strip()
                        sq = (
                            data.get("search")
                            or data.get("SEARCH")
                            or data.get("queries")
                        )
                        if isinstance(sq, list):
                            search_raw = " | ".join(str(x) for x in sq)
                        else:
                            search_raw = str(sq or "").strip()
                        need_raw = str(
                            data.get("need_search") or data.get("NEED_SEARCH") or ""
                        ).strip()
                        note = str(data.get("note") or data.get("NOTE") or "").strip()
            except Exception:  # noqa: BLE001
                pass

        # 仍没有结构：若整段很短且像搜索词，当 claim
        if not (claim or search_raw or need_raw):
            compact = re.sub(r"\s+", " ", raw).strip()
            if 2 <= len(compact) <= 60 and not self._is_useless_search_query(compact):
                claim = compact[:80]
                search_raw = compact[:80]
                need_raw = "yes"
            else:
                return None

        claim = re.sub(r"\s+", " ", claim).strip().strip(" \"'`")
        if self._is_image_placeholder(claim) or claim.lower() in {
            "none",
            "n/a",
            "无",
            "空",
        }:
            claim = ""
        claim = claim[:120]

        need_search = self._parse_need_search(need_raw, search_raw)
        queries = self._split_search_queries(search_raw)
        if (
            need_search
            and not queries
            and claim
            and not self._is_useless_search_query(claim)
        ):
            queries = [claim[:80]]
        if not need_search:
            queries = []

        return {
            "claim": claim,
            "queries": queries[: self.max_search_queries],
            "need_search": need_search,
            "note": re.sub(r"\s+", " ", note).strip()[:120],
            "fallback": False,
        }

    @staticmethod
    def _parse_need_search(need_raw: str, search_raw: str) -> bool:
        n = (need_raw or "").strip().lower()
        if n in {"yes", "y", "true", "1", "需要", "是", "要"}:
            return True
        if n in {"no", "n", "false", "0", "不需要", "否", "不"}:
            return False
        s = (search_raw or "").strip().lower()
        if not s or s in {"none", "n/a", "na", "null", "无", "不需要"}:
            return False
        return True

    def _split_search_queries(self, search_raw: str) -> list[str]:
        raw = (search_raw or "").strip()
        if not raw:
            return []
        low = raw.lower().strip()
        if low in {"none", "n/a", "na", "null", "无", "不需要", "不需要搜索"}:
            return []
        parts = re.split(r"\s*[|｜]\s*|\n+", raw)
        out: list[str] = []
        seen: set[str] = set()
        for part in parts:
            q = re.sub(r"\s+", " ", part).strip().strip(" \"'`")
            q = q[:80]
            if self._is_useless_search_query(q):
                continue
            key = q.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(q)
            if len(out) >= self.max_search_queries:
                break
        return out

    async def _web_search(
        self, query: str, event: AstrMessageEvent | None = None
    ) -> str:
        """搜索渠道适配层入口。

        search_provider 指定渠道时只走该渠道；auto 时按框架配置的
        websearch_provider 优先，其余渠道按固定顺序自动降级。
        任一渠道失败/无 Key/空结果都不抛异常，返回 "" 让上层走兜底。
        """
        query = (query or "").strip()
        if not query or self.search_provider == "none":
            return ""

        provider = self.search_provider
        if provider != "auto":
            try:
                return await self._search_with(provider, query, event)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"{LOG_PREFIX} {provider} 搜索失败：{e}")
                return ""

        last_err: Exception | None = None
        for cand in self._auto_search_order():
            try:
                result = await self._search_with(cand, query, event)
                if result:
                    return result
                logger.info(f"{LOG_PREFIX} {cand} 返回空结果，尝试下一渠道")
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning(f"{LOG_PREFIX} {cand} 搜索失败，尝试下一渠道：{e}")
        if last_err:
            logger.warning(f"{LOG_PREFIX} 全部搜索渠道失败：{last_err}")
        return ""

    def _auto_search_order(self) -> list[str]:
        """auto 优先级：框架配置的 websearch_provider 放首位，其余固定顺序兜底。"""
        order: list[str] = []
        try:
            cfg = self.context.get_config(umo=None)
            provider_settings = cfg.get("provider_settings", {}) or {}
            framework_provider = (
                str(provider_settings.get("websearch_provider", "") or "")
                .strip()
                .lower()
            )
            if framework_provider in SEARCH_FALLBACK_ORDER:
                order.append(framework_provider)
        except Exception:  # noqa: BLE001
            pass  # 读框架配置失败按未配置处理，走默认降级顺序
        for p in SEARCH_FALLBACK_ORDER:
            if p not in order:
                order.append(p)
        return order

    def _read_framework_keys(self, setting_name: str) -> list[str]:
        """从框架 provider_settings 读取 Key 列表（websearch_tavily_key 等）。"""
        try:
            cfg = self.context.get_config(umo=None)
            provider_settings = cfg.get("provider_settings", {}) or {}
            raw = provider_settings.get(setting_name, [])
        except Exception:  # noqa: BLE001
            return []
        if isinstance(raw, str):
            raw = [raw] if raw.strip() else []
        if not isinstance(raw, list):
            return []
        return [str(k).strip() for k in raw if str(k).strip()]

    async def _search_with(
        self, provider: str, query: str, event: AstrMessageEvent | None = None
    ) -> str:
        """按渠道分发：tavily 直连 / anysearch 工具或内联 / 其余走框架内置工具。"""
        if provider == "anysearch":
            return await self._anysearch_search(query, event)
        if provider == "tavily":
            return await self._tavily_search(query)
        return await self._builtin_tool_search(provider, query, event)

    async def _tavily_search(self, query: str) -> str:
        """直连 Tavily /search：从框架配置读取 Key，round-robin + 失败自动换 Key。"""
        keys = self._read_framework_keys("websearch_tavily_key")
        if not keys:
            logger.info(f"{LOG_PREFIX} 框架未配置 Tavily Key，跳过该渠道")
            return ""

        import aiohttp

        payload = {"query": query, "max_results": 5, "search_depth": "basic"}
        timeout = aiohttp.ClientTimeout(total=self.search_timeout)
        last_err: Exception | None = None
        for _ in range(len(keys)):
            key = keys[self._tavily_key_idx % len(keys)]
            self._tavily_key_idx += 1
            try:
                async with (
                    aiohttp.ClientSession(timeout=timeout, trust_env=True) as session,
                    session.post(
                        "https://api.tavily.com/search",
                        json=payload,
                        headers={
                            "Authorization": f"Bearer {key}",
                            "Content-Type": "application/json",
                        },
                    ) as resp,
                ):
                    if resp.status == 200:
                        data = await resp.json()
                        items = data.get("results", []) or []
                        if not items:
                            return ""
                        lines = []
                        for item in items[:5]:
                            title = str(item.get("title") or "无标题")
                            url = str(item.get("url") or "")
                            content = str(item.get("content") or "")
                            lines.append(
                                f"### {title}\n- **URL**: {url}\n- **内容**: {content[:500]}"
                            )
                        return "## Tavily 搜索结果\n" + "\n\n".join(lines)
                    reason = (await resp.text())[:200]
                    if resp.status in _RETRYABLE_HTTP_STATUSES:
                        last_err = RuntimeError(f"HTTP {resp.status} {reason}")
                        continue
                    logger.warning(f"{LOG_PREFIX} Tavily HTTP {resp.status}: {reason}")
                    return ""
            except Exception as e:  # noqa: BLE001
                last_err = e
                continue
        if last_err is not None:
            logger.warning(f"{LOG_PREFIX} Tavily 全部 Key 失败：{last_err}")
            raise last_err
        return ""

    async def _anysearch_search(
        self, query: str, event: AstrMessageEvent | None = None
    ) -> str:
        """Anysearch 渠道：优先 anysearch_search 工具，否则内联 HTTP 直连。"""
        try:
            tool_manager = getattr(self.context, "get_llm_tool_manager", lambda: None)()
            tool = tool_manager.get_func("anysearch_search") if tool_manager else None
            if tool and getattr(tool, "active", True) and hasattr(tool, "run"):
                logger.info(f"{LOG_PREFIX} 使用 anysearch_search 工具搜索")
                result = await asyncio.wait_for(
                    tool.run(event, query=query, max_results=5),
                    timeout=self.search_timeout,
                )
                return self._truncate(str(result or "").strip(), self.max_search_chars)

            api_key = self._read_anysearch_api_key()
            logger.info(f"{LOG_PREFIX} 使用内置 Anysearch HTTP 客户端搜索")
            client = _InlineAnySearchClient(
                api_key=api_key, timeout=self.search_timeout
            )
            result = await asyncio.wait_for(
                client.search(query, max_results=5),
                timeout=self.search_timeout + 5,
            )
            return self._truncate(str(result or "").strip(), self.max_search_chars)
        except TimeoutError as e:
            logger.warning(f"{LOG_PREFIX} Anysearch 搜索超时：{e}")
            return ""
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{LOG_PREFIX} Anysearch 搜索异常：{e}")
            return ""

    async def _builtin_tool_search(
        self, provider: str, query: str, event: AstrMessageEvent | None = None
    ) -> str:
        """调用框架内置 web_search_{provider} 工具（如 bocha/brave/firecrawl 等）。"""
        tool_manager = getattr(self.context, "get_llm_tool_manager", lambda: None)()
        tool = tool_manager.get_func(f"web_search_{provider}") if tool_manager else None
        if (
            tool is None
            or not getattr(tool, "active", True)
            or not hasattr(tool, "call")
        ):
            return ""
        logger.info(f"{LOG_PREFIX} 使用框架内置工具 web_search_{provider} 搜索")
        try:
            from astrbot.core.astr_agent_context import AstrAgentContext

            agent_ctx = AstrAgentContext(context=self.context, event=event)
            result = await asyncio.wait_for(
                tool.call(agent_ctx, query=query, max_results=5),
                timeout=self.search_timeout + 10,
            )
            text = str(getattr(result, "result", result) or "").strip()
            if not text or text.startswith("Error:"):
                return ""
            return self._truncate(text, self.max_search_chars)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{LOG_PREFIX} 内置工具 web_search_{provider} 调用失败：{e}")
            return ""

    @staticmethod
    def _read_anysearch_api_key() -> str:
        candidates = []
        try:
            data_path = Path(get_astrbot_data_path())
            candidates.extend(
                [
                    data_path / "config" / "astrbot_plugin_anysearch_config.json",
                    data_path
                    / "plugin_configs"
                    / "astrbot_plugin_anysearch_config.json",
                    data_path
                    / "plugin_configs"
                    / "astrbot_plugin_anysearch"
                    / "config.json",
                ]
            )
        except Exception:  # noqa: BLE001
            pass

        for config_path in candidates:
            if not config_path.is_file():
                continue
            try:
                data = json.loads(config_path.read_text(encoding="utf-8-sig"))
                key = str(data.get("api_key", "") or "")
                if key:
                    return key
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"{LOG_PREFIX} 读取 Anysearch 配置失败：{config_path} | {e}"
                )
        return ""

    @staticmethod
    def _friendly_error(err_msg: str) -> str:
        msg = err_msg.lower()
        if "sensitive" in msg or "content_filter" in msg or "1026" in msg:
            return "图片内容被AI服务商安全审核拦截，无法判断，请更换图片后重试。"
        if "rate_limit" in msg or "429" in msg or "quota" in msg:
            return "AI服务当前繁忙，请稍后重试。"
        if "timeout" in msg or "timed out" in msg:
            return "判断超时，请稍后重试。"
        if "context_length" in msg or "too long" in msg or "maximum context" in msg:
            return "内容过长，超出AI处理限制，请精简后重试。"
        return "判断失败，请稍后重试。"

    @classmethod
    def _normalize_trigger_phrases(cls, value: object) -> tuple[str, ...]:
        if isinstance(value, str):
            raw_items = [value]
        elif isinstance(value, list | tuple | set):
            raw_items = list(value)
        else:
            raw_items = []
        phrases: list[str] = []
        seen: set[str] = set()
        for item in raw_items:
            phrase = str(item).strip()
            if phrase and phrase not in seen:
                phrases.append(phrase)
                seen.add(phrase)
        return tuple(phrases or DEFAULT_TRIGGER_PHRASES)

    @staticmethod
    def _parse_id_set(value: object) -> set[str]:
        if isinstance(value, str):
            raw_items = re.split(r"[\s,，;；]+", value)
        elif isinstance(value, list | tuple | set):
            raw_items = list(value)
        else:
            raw_items = []
        result: set[str] = set()
        for item in raw_items:
            text = str(item).strip()
            if text:
                result.add(text)
        return result

    @staticmethod
    def _safe_group_id(event: AstrMessageEvent) -> str:
        try:
            gid = event.get_group_id()
        except Exception:  # noqa: BLE001
            return ""
        return str(gid or "").strip()

    def _match_trigger(self, event: AstrMessageEvent) -> tuple[bool, str]:
        plain = self._plain_text(event).strip()
        trigger_phrases = sorted(self.trigger_phrases, key=len, reverse=True)
        has_keyword = any(self._phrase_hit(plain, phrase) for phrase in trigger_phrases)

        if self._is_at_me(event) and has_keyword:
            return True, ""

        if self._has_reply(event) and has_keyword:
            return True, ""

        if not plain:
            return False, ""

        if self.listen_suffix:
            for phrase in trigger_phrases:
                for key in (f"{phrase}？", f"{phrase}?", phrase):
                    if plain.endswith(key) and self._phrase_hit(plain, phrase):
                        return True, key

        if self.listen_prefix:
            for phrase in trigger_phrases:
                if plain.startswith(phrase) and self._phrase_hit(plain, phrase):
                    return True, phrase

        return False, ""

    def _phrase_hit(self, text: str, phrase: str) -> bool:
        """Boundary-aware phrase match for ascii tokens; normal contains for CJK."""
        if not text or not phrase:
            return False
        if phrase == text:
            return True
        if text.startswith(phrase) or text.endswith(phrase):
            return True
        if re.fullmatch(r"[A-Za-z0-9_+\-]+", phrase):
            pattern = rf"(?<![A-Za-z0-9_]){re.escape(phrase)}(?![A-Za-z0-9_])"
            return re.search(pattern, text) is not None
        return phrase in text

    def _has_reply(self, event: AstrMessageEvent) -> bool:
        return any(isinstance(comp, Reply) for comp in event.get_messages())

    def _plain_text(self, event: AstrMessageEvent) -> str:
        return "".join(
            c.text for c in event.get_messages() if isinstance(c, Plain) and c.text
        )

    def _is_at_me(self, event: AstrMessageEvent) -> bool:
        self_id = str(event.get_self_id())
        for comp in event.get_messages():
            if isinstance(comp, At) and str(getattr(comp, "qq", "")) == self_id:
                return True
        return False

    async def _extract_bundle(
        self, event: AstrMessageEvent, strip_keyword: str = ""
    ) -> dict[str, Any]:
        """Extract claim content + optional user supplement from current message."""
        chain = list(event.get_messages())
        self_id = str(event.get_self_id())

        current_text, current_images = self._parse_chain(
            [
                c
                for c in chain
                if not (isinstance(c, At) and str(getattr(c, "qq", "")) == self_id)
            ]
        )
        current_text = self._strip_triggers(current_text, strip_keyword)

        (
            forward_text,
            forward_images,
            forward_note,
        ) = await self._extract_forward_from_chain(event, chain)
        if forward_text or forward_images:
            return {
                "source": "合并转发",
                "text": forward_text,
                "images": list(dict.fromkeys([*forward_images, *current_images])),
                "supplement": current_text,
                "image_note": forward_note,
            }

        for comp in chain:
            if not isinstance(comp, Reply):
                continue
            quoted_text = ""
            quoted_images: list[str] = []
            image_note = ""
            try:
                quoted_text = (await extract_quoted_message_text(event, comp)) or ""
                quoted_images = await extract_quoted_message_images(event, comp)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"{LOG_PREFIX} 解析引用消息失败，回退消息链：{e}")
                image_note = "引用消息完整展开失败，已尽量使用本地可见内容。"
            if not quoted_text and not quoted_images and comp.chain:
                quoted_text, quoted_images = self._parse_chain(comp.chain)
            if not quoted_text and not quoted_images:
                quoted_text = str(
                    getattr(comp, "message_str", "") or getattr(comp, "text", "") or ""
                ).strip()
            if quoted_text or quoted_images:
                # 保留原始 quoted_text 供上层 raw 对比；纯占位由 handle 再 sanitize
                return {
                    "source": "引用消息",
                    "text": quoted_text,
                    "images": list(dict.fromkeys([*quoted_images, *current_images])),
                    "supplement": current_text,
                    "image_note": image_note,
                }

        return {
            "source": "当前消息",
            "text": current_text,
            "images": current_images,
            "supplement": "",
            "image_note": "",
        }

    def _new_forward_budget(self) -> _ForwardBudget:
        return _ForwardBudget(
            max_depth=self.max_forward_depth,
            max_nodes=self.max_forward_nodes,
            max_fetch=self.max_forward_fetch,
            max_images=self.max_forward_images,
            max_chars=self.max_content_chars,
        )

    async def _extract_forward_from_chain(
        self, event: AstrMessageEvent, chain: list
    ) -> tuple[str, list[str], str]:
        budget = self._new_forward_budget()
        texts: list[str] = []
        images: list[str] = []

        for comp in chain:
            if budget.truncated:
                break
            if isinstance(comp, Nodes):
                t, imgs = self._parse_nodes(comp, budget=budget, depth=1)
                if t:
                    texts.append(t)
                images.extend(imgs)
            elif isinstance(comp, Node):
                t, imgs = self._parse_node(comp, budget=budget, depth=1)
                if t:
                    texts.append(t)
                images.extend(imgs)
            elif isinstance(comp, Forward):
                fid = str(getattr(comp, "id", "") or "").strip()
                if not fid:
                    continue
                t, imgs = await self._fetch_forward_by_id(
                    event, fid, budget=budget, depth=1
                )
                if t:
                    texts.append(t)
                images.extend(imgs)

        note = budget.summary_note()
        if budget.truncated:
            logger.warning(
                f"{LOG_PREFIX} 合并转发展开被截断 | nodes={budget.nodes}/{budget.max_nodes} "
                f"fetch={budget.fetches}/{budget.max_fetch} images={budget.images}/{budget.max_images} "
                f"note={note!r}"
            )
        return "\n".join(texts).strip(), list(dict.fromkeys(images)), note

    async def _fetch_forward_by_id(
        self,
        event: AstrMessageEvent,
        forward_id: str,
        *,
        budget: _ForwardBudget | None = None,
        depth: int = 1,
    ) -> tuple[str, list[str]]:
        """Best-effort OneBot get_forward_msg with nest/cycle budget."""
        budget = budget or self._new_forward_budget()
        if not budget.allow_depth(depth):
            return "", []
        if not budget.allow_fetch(forward_id):
            return "", []

        try:
            client = getattr(event, "bot", None)
            if client is None:
                budget.note("合并转发未能展开（无可用 OneBot 客户端）。")
                return "", []

            result = None
            api = getattr(client, "api", client)
            for method_name in ("get_forward_msg", "getForwardMsg"):
                method = getattr(api, method_name, None)
                if not callable(method):
                    continue
                try:
                    result = await method(id=forward_id)
                except TypeError:
                    try:
                        result = await method(forward_id)
                    except Exception:  # noqa: BLE001
                        result = await method(**{"message_id": forward_id})
                break

            if result is None and hasattr(client, "call_action"):
                result = await client.call_action("get_forward_msg", id=forward_id)
            if not result:
                budget.note("合并转发展开失败。")
                return "", []

            payload = result
            if isinstance(result, dict) and "data" in result:
                payload = result.get("data") or {}

            messages = []
            if isinstance(payload, dict):
                messages = payload.get("messages") or payload.get("message") or []

            text_parts: list[str] = []
            images: list[str] = []
            if isinstance(messages, list):
                for msg in messages:
                    if budget.truncated:
                        break
                    if not isinstance(msg, dict):
                        continue
                    if not budget.allow_node():
                        break
                    sender = msg.get("sender") or {}
                    name = ""
                    if isinstance(sender, dict):
                        name = str(sender.get("nickname") or sender.get("card") or "")
                    content = msg.get("content") or msg.get("message") or []
                    t, imgs = await self._parse_onebot_content(
                        event, content, budget=budget, depth=depth
                    )
                    t = budget.take_text(t)
                    imgs = budget.take_images(imgs)
                    if t:
                        text_parts.append(f"{name}: {t}" if name else t)
                    images.extend(imgs)
            return "\n".join(text_parts).strip(), list(dict.fromkeys(images))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{LOG_PREFIX} get_forward_msg 失败：{e}")
            budget.note(f"合并转发展开失败：{e}")
            return "", []

    def _parse_nodes(
        self,
        nodes: Nodes,
        *,
        budget: _ForwardBudget | None = None,
        depth: int = 1,
    ) -> tuple[str, list[str]]:
        budget = budget or self._new_forward_budget()
        if not budget.allow_depth(depth):
            return "", []
        parts: list[str] = []
        images: list[str] = []
        for node in getattr(nodes, "nodes", []) or []:
            if budget.truncated:
                break
            t, imgs = self._parse_node(node, budget=budget, depth=depth)
            if t:
                parts.append(t)
            images.extend(imgs)
        return "\n".join(parts).strip(), images

    def _parse_node(
        self,
        node: Node,
        *,
        budget: _ForwardBudget | None = None,
        depth: int = 1,
    ) -> tuple[str, list[str]]:
        budget = budget or self._new_forward_budget()
        if not budget.allow_depth(depth):
            return "", []
        if not budget.allow_node():
            return "", []
        name = str(getattr(node, "name", "") or "").strip()
        content = getattr(node, "content", None) or []
        text, images = self._parse_chain(content, budget=budget, depth=depth)
        text = budget.take_text(text)
        images = budget.take_images(images)
        if text and name:
            text = f"{name}: {text}"
        return text, images

    async def _parse_onebot_content(
        self,
        event: AstrMessageEvent,
        content: object,
        *,
        budget: _ForwardBudget,
        depth: int,
    ) -> tuple[str, list[str]]:
        text_parts: list[str] = []
        images: list[str] = []
        if isinstance(content, str):
            return content.strip(), []
        if not isinstance(content, list):
            return "", []

        nested_forward_ids: list[str] = []
        for seg in content:
            if budget.truncated:
                break
            if not isinstance(seg, dict):
                continue
            seg_type = str(seg.get("type", "")).lower()
            data = seg.get("data") or {}
            if seg_type in {"text", "plain"}:
                text_parts.append(str(data.get("text", "") or ""))
            elif seg_type == "image":
                url = data.get("url") or data.get("file") or data.get("path") or ""
                if url:
                    images.append(str(url))
            elif seg_type == "forward":
                fid = str(data.get("id") or data.get("message_id") or "").strip()
                if fid:
                    nested_forward_ids.append(fid)
                else:
                    text_parts.append("[嵌套合并转发]")
            elif seg_type == "node":
                # rare raw node dict; keep text-ish fallback
                inner = data.get("content") or []
                t, imgs = await self._parse_onebot_content(
                    event, inner, budget=budget, depth=depth + 1
                )
                if t:
                    text_parts.append(t)
                images.extend(imgs)

        # 嵌套 forward：在预算内继续展开，超出则只留占位
        for fid in nested_forward_ids:
            if budget.truncated or not budget.allow_depth(depth + 1):
                text_parts.append("[嵌套合并转发已截断]")
                break
            nested_text, nested_imgs = await self._fetch_forward_by_id(
                event, fid, budget=budget, depth=depth + 1
            )
            if nested_text:
                text_parts.append(nested_text)
            elif not budget.truncated:
                text_parts.append("[嵌套合并转发]")
            images.extend(nested_imgs)

        return " ".join(p for p in text_parts if p).strip(), images

    def _strip_triggers(self, text: str, strip_keyword: str = "") -> str:
        if not text:
            return ""
        text = text.strip()
        if strip_keyword and text:
            if text.endswith(strip_keyword):
                text = text[: -len(strip_keyword)].strip()
            elif text.startswith(strip_keyword):
                text = text[len(strip_keyword) :].strip()
            return text

        phrases = sorted(self.trigger_phrases, key=len, reverse=True)
        for phrase in phrases:
            if re.fullmatch(r"[A-Za-z0-9_+\-]+", phrase):
                text = re.sub(
                    rf"(?<![A-Za-z0-9_]){re.escape(phrase)}(?![A-Za-z0-9_])",
                    " ",
                    text,
                )
            else:
                text = text.replace(phrase, " ")
        text = re.sub(r"[？?]+", " ", text)
        return " ".join(text.split()).strip()

    def _parse_chain(
        self,
        chain: list,
        *,
        budget: _ForwardBudget | None = None,
        depth: int = 0,
    ) -> tuple[str, list[str]]:
        """Parse message chain.

        When budget is provided, nested Nodes/Node are expanded under the same
        forward-expansion limits. Without budget, nested forwards are ignored to
        avoid unbounded recursion on plain current-message parsing.
        """
        text_parts: list[str] = []
        images: list[str] = []
        for comp in chain or []:
            if budget is not None and budget.truncated:
                break
            if isinstance(comp, Plain) and comp.text:
                text_parts.append(comp.text.strip())
            elif isinstance(comp, Image):
                url = (
                    getattr(comp, "url", None)
                    or getattr(comp, "file", None)
                    or getattr(comp, "path", None)
                )
                if url:
                    images.append(str(url))
            elif isinstance(comp, Nodes):
                if budget is None:
                    # 顶层普通解析不递归展开，避免无预算时被嵌套打爆
                    text_parts.append("[合并转发]")
                    continue
                t, imgs = self._parse_nodes(comp, budget=budget, depth=depth + 1)
                if t:
                    text_parts.append(t)
                images.extend(imgs)
            elif isinstance(comp, Node):
                if budget is None:
                    text_parts.append("[合并转发节点]")
                    continue
                t, imgs = self._parse_node(comp, budget=budget, depth=depth + 1)
                if t:
                    text_parts.append(t)
                images.extend(imgs)
            elif isinstance(comp, Forward):
                # bare Forward needs async API; only handled in _extract_forward_from_chain
                text_parts.append("[合并转发]")
        return " ".join(p for p in text_parts if p).strip(), list(dict.fromkeys(images))

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        text = text or ""
        if limit <= 0 or len(text) <= limit:
            return text
        keep = max(0, limit - 12)
        return text[:keep].rstrip() + "\n…(已截断)"

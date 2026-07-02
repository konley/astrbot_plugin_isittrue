import asyncio
import json
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image, Plain, Reply
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core.utils.quoted_message_parser import (
    extract_quoted_message_images,
    extract_quoted_message_text,
)
from data.plugins.astrbot_plugin_anysearch.client import AnySearchClient, AnySearchError


@register(
    "astrbot_plugin_isittrue",
    "you",
    "是真的吗——群聊事实核查小工具。@机器人说出你想核实的事情，或引用一条消息，AI 自动判断真假。无需额外 API，即装即用。",
    "1.0.0",
)
class IsItTrue(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        config = config or {}
        self.cooldown: int = int(config.get("cooldown", 10))
        self.listen_suffix: bool = bool(config.get("listen_suffix", False))
        self.listen_prefix: bool = bool(config.get("listen_prefix", False))
        self.enable_vision: bool = bool(config.get("enable_vision", True))
        self.provider_id: str = str(config.get("provider_id", "openai/gpt-5.5")).strip()
        self.trigger_phrases: tuple[str, ...] = self._normalize_trigger_phrases(
            config.get("trigger_phrases", self.DEFAULT_TRIGGER_PHRASES)
        )
        # Web search enhancement: fetch references before sending the prompt to the model.
        self.enable_web_search: bool = bool(config.get("enable_web_search", False))
        self.search_timeout: int = int(config.get("search_timeout", 30))
        self.system_prompt: str = config.get(
            "system_prompt",
            "你是一个事实核查专家。用户会向你提供一段或多段内容（可能包含文本和图片）。"
            "请你仔细分析，判断内容的整体真实性。\n"
            "如果判断涉及时效性信息（如实时数据、最新事件、价格行情等），且你具备联网搜索能力，"
            "必须先联网检索核实后再下结论，不要仅凭记忆臆测。\n"
            "必须以如下格式回答（严格遵守，第一行只能是单个单词，不要输出任何额外前缀）：\n"
            "第一行：true（属实）/ false（不实）/ unknown（无法核实，如主观观点、预测、缺乏可验证事实）\n"
            "第二行起：用中文给出简洁的解释（100字以内）。",
        )
        # 用户冷却记录： {user_id: last_ts}
        self._cooldowns: dict[str, float] = {}

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_message(self, event: AstrMessageEvent):
        """触发方式：
        1) @机器人 + 配置的触发词（始终生效）
        2) listen_suffix 开启：消息以配置的触发词结尾
        3) listen_prefix 开启：消息以配置的触发词开头
        """
        triggered, strip_keyword = self._match_trigger(event)
        if not triggered:
            return

        logger.info(
            f"[是真的吗] 触发判断 | 关键词剥离={strip_keyword!r} | "
            f"enable_web_search={self.enable_web_search} enable_vision={self.enable_vision}"
        )

        # 冷却检测
        user_id = event.get_sender_id()
        now = time.time()
        last = self._cooldowns.get(user_id)
        if last is not None and now - last < self.cooldown:
            remain = int(self.cooldown - (now - last)) + 1
            yield event.plain_result(f"检测冷却中，请 {remain} 秒后再试。")
            return

        # 提取内容（优先引用消息，否则当前消息去除 @ / 触发关键词）
        text, images = await self._extract_content(event, strip_keyword)
        logger.info(
            f"[是真的吗] 提取内容 | text={text!r} | images={len(images)}张 {images}"
        )
        if not text and not images:
            yield event.plain_result(
                "没有找到有效的文本或图片内容，请引用一条消息或直接在艾特后发送内容。"
            )
            return

        # 调用配置的大模型，默认使用 openai/gpt-5.5。
        provider = self._resolve_provider()
        if provider is None:
            yield event.plain_result(
                "当前未配置任何大模型提供商，请在 AstrBot 后台配置后再使用。"
            )
            return

        self._cooldowns[user_id] = now

        # Optional web search enhancement through astrbot_plugin_anysearch.
        search_block = ""
        if self.enable_web_search:
            # 确定搜索关键词：有文本直接用；纯图片则先让多模态模型从图中提取关键词
            query = text
            if not query and images:
                query = await self._query_from_images(provider, images)
                logger.info(f"[是真的吗] 从图片提取搜索关键词：{query!r}")
            if query:
                logger.info(f"[是真的吗] 准备联网搜索：{query!r}")
                search_block = await self._web_search(query, event=event)
                logger.info(
                    f"[是真的吗] 联网搜索结果长度={len(search_block)}"
                    + (
                        f" | 预览：{search_block[:120]!r}"
                        if search_block
                        else " | （空，已回退兜底）"
                    )
                )
            else:
                logger.info(
                    "[是真的吗] 已开启联网搜索，但未能得到有效搜索关键词，跳过搜索"
                )
        else:
            logger.info("[是真的吗] 联网搜索未开启（enable_web_search=False）")

        prompt = text or "请判断所给图片内容的真实性。"
        if search_block:
            prompt = (
                f"以下是联网检索到的参考资料（可能含时效性信息），请结合它来核查：\n"
                f"{search_block}\n\n待核查内容：{prompt}"
            )

        try:
            llm_resp = await provider.text_chat(
                prompt=prompt,
                image_urls=images if self.enable_vision else [],
                system_prompt=self.system_prompt,
            )
            content = (llm_resp.completion_text or "").strip()
            logger.info(f"[是真的吗] 模型返回：{content[:200]!r}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"[是真的吗] 调用大模型失败: {e}")
            yield event.plain_result("判断失败，请稍后重试。")
            return

        if not content:
            yield event.plain_result("模型未返回有效内容。")
            return

        # 后处理：解析首行判定（true/false/unknown），转为友好中文标签
        content = self._format_verdict(content)
        yield event.plain_result(content)

    def _resolve_provider(self):
        """Resolve the configured fact-check provider.

        Returns:
            Configured provider when available, otherwise the current default provider.
        """
        if self.provider_id:
            provider = self.context.get_provider_by_id(self.provider_id)
            if provider is not None:
                return provider
            logger.warning(
                f"[是真的吗] 未找到配置的 provider_id={self.provider_id!r}，回退默认 Provider"
            )
        return self.context.get_using_provider()

    @staticmethod
    def _format_verdict(content: str) -> str:
        """将模型返回的首行判定转为带标签的展示文本。

        识别 true / false / unknown 三态；无法识别时原样返回并标注。
        """
        lines = content.split("\n", 1)
        head = lines[0].strip().lower().rstrip("。.!！,，")
        rest = lines[1].strip() if len(lines) > 1 else ""

        label_map = {
            "true": "✅ 真的喵",
            "false": "❌ 假的喵",
            "unknown": "⚠️ 布吉岛",
        }
        for key, label in label_map.items():
            if head == key or head.startswith(key):
                return f"{label}\n{rest}" if rest else label

        # 未按格式返回：原样输出，避免丢失信息
        return content

    # ---------- 联网搜索（可选增强） ----------

    async def _query_from_images(self, provider, images: list[str]) -> str:
        """纯图片场景：先让多模态模型把图中的关键事实提炼成一句可搜索的关键词。

        失败返回空串，主流程会跳过搜索、回退到直接视觉判断。
        """
        if not self.enable_vision:
            return ""
        try:
            resp = await provider.text_chat(
                prompt="请用一句话（30字以内）概括这张图片中最关键、最适合用于联网核查的事实主张，"
                "只输出该句子本身，不要解释、不要标点修饰。",
                image_urls=images,
            )
            return (resp.completion_text or "").strip().replace("\n", " ")[:60]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[是真的吗] 从图片提取关键词失败，跳过搜索：{e}")
            return ""

    async def _web_search(
        self, query: str, event: AstrMessageEvent | None = None
    ) -> str:
        """Search the web through Anysearch and return prompt-ready references.

        Args:
            query: Search query extracted from text or images.
            event: Optional source event passed to the registered LLM tool.

        Returns:
            Search text limited for prompt injection, or an empty string on failure.
        """
        try:
            tool_manager = getattr(self.context, "get_llm_tool_manager", lambda: None)()
            tool = tool_manager.get_func("anysearch_search") if tool_manager else None
            if tool and getattr(tool, "active", True) and hasattr(tool, "run"):
                logger.info("[是真的吗] 使用 anysearch_search 工具执行联网搜索")
                result = await asyncio.wait_for(
                    tool.run(event, query=query, max_results=5),
                    timeout=self.search_timeout,
                )
                return str(result or "").strip()[:2000]

            config_path = (
                Path(get_astrbot_data_path())
                / "config"
                / "astrbot_plugin_anysearch_config.json"
            )
            api_key = ""
            if config_path.exists():
                data = json.loads(config_path.read_text(encoding="utf-8-sig"))
                api_key = str(data.get("api_key", ""))
            logger.info("[是真的吗] 使用 Anysearch client 执行联网搜索")
            client = AnySearchClient(api_key=api_key, timeout=self.search_timeout)
            result = await client.search(query, max_results=5)
            return str(result or "").strip()[:2000]
        except AnySearchError as e:
            logger.warning(f"[是真的吗] Anysearch 联网搜索失败，回退兜底：{e}")
            return ""
        except TimeoutError as e:
            logger.warning(f"[是真的吗] Anysearch 联网搜索超时，回退兜底：{e}")
            return ""
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[是真的吗] Anysearch 联网搜索异常，回退兜底：{e}")
            return ""

    # ---------- 辅助方法 ----------

    DEFAULT_TRIGGER_PHRASES = ("真的吗",)

    @classmethod
    def _normalize_trigger_phrases(cls, value: object) -> tuple[str, ...]:
        """Normalize trigger phrase config.

        Args:
            value: Raw value loaded from plugin config.

        Returns:
            Non-empty tuple of stripped trigger phrases.
        """
        if isinstance(value, str):
            raw_items = [value]
        elif isinstance(value, list | tuple | set):
            raw_items = value
        else:
            raw_items = []

        phrases = []
        seen = set()
        for item in raw_items:
            phrase = str(item).strip()
            if phrase and phrase not in seen:
                phrases.append(phrase)
                seen.add(phrase)
        return tuple(phrases or cls.DEFAULT_TRIGGER_PHRASES)

    def _match_trigger(self, event: AstrMessageEvent) -> tuple[bool, str]:
        """判断是否触发，返回 (是否触发, 需从文本中剥离的关键词)。

        关键词剥离仅用于"开头/结尾"模式，避免把触发词当成待核查内容。
        """
        # 纯文本（用于关键词/前后缀匹配）
        plain = self._plain_text(event).strip()
        trigger_phrases = sorted(self.trigger_phrases, key=len, reverse=True)
        has_keyword = any(phrase in plain for phrase in trigger_phrases)

        # 1) @机器人 且 文本含触发词 → 触发（仅 @ 不带触发词不拦截）
        if self._is_at_me(event) and has_keyword:
            return True, ""

        # 2) 引用消息 + 自带触发词 → 触发（不受开关控制）
        #    对被引用的原消息进行核查
        if self._has_reply(event) and has_keyword:
            return True, ""

        if not plain:
            return False, ""

        # 3) 结尾监听
        if self.listen_suffix:
            for phrase in trigger_phrases:
                for key in (f"{phrase}？", f"{phrase}?", phrase):
                    if plain.endswith(key):
                        return True, key

        # 4) 开头监听
        if self.listen_prefix:
            for phrase in trigger_phrases:
                if plain.startswith(phrase):
                    return True, phrase

        return False, ""

    def _has_reply(self, event: AstrMessageEvent) -> bool:
        """判断消息是否引用了另一条消息。"""
        return any(isinstance(comp, Reply) for comp in event.get_messages())

    def _plain_text(self, event: AstrMessageEvent) -> str:
        """拼接当前消息的纯文本部分。"""
        return "".join(
            c.text for c in event.get_messages() if isinstance(c, Plain) and c.text
        )

    def _is_at_me(self, event: AstrMessageEvent) -> bool:
        """判断消息是否 @ 了机器人。"""
        self_id = str(event.get_self_id())
        for comp in event.get_messages():
            if isinstance(comp, At) and str(comp.qq) == self_id:
                return True
        return False

    async def _extract_content(
        self, event: AstrMessageEvent, strip_keyword: str = ""
    ) -> tuple[str, list[str]]:
        """提取待核查的文本与图片，优先取引用消息。"""
        chain = event.get_messages()

        # 优先：引用消息（Reply）中的内容；QQ 合并转发需通过 OneBot API 展开。
        for comp in chain:
            if not isinstance(comp, Reply):
                continue
            try:
                quoted_text = (await extract_quoted_message_text(event, comp)) or ""
                quoted_images = await extract_quoted_message_images(event, comp)
                if quoted_text or quoted_images:
                    return quoted_text, quoted_images
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[是真的吗] 解析引用消息失败，回退消息链解析：{e}")
            if comp.chain:
                text, images = self._parse_chain(comp.chain)
                if text or images:
                    return text, images

        # 否则：当前消息，去除 @机器人 部分
        self_id = str(event.get_self_id())
        cleaned = [c for c in chain if not (isinstance(c, At) and str(c.qq) == self_id)]
        text, images = self._parse_chain(cleaned)

        # 剥离触发关键词：前后缀模式按指定词剥离；@模式直接去掉全部触发词
        if strip_keyword and text:
            if text.endswith(strip_keyword):
                text = text[: -len(strip_keyword)].strip()
            elif text.startswith(strip_keyword):
                text = text[len(strip_keyword) :].strip()
        elif text:
            for phrase in sorted(self.trigger_phrases, key=len, reverse=True):
                text = text.replace(phrase, " ")
            text = " ".join(text.split()).strip()
        return text, images

    def _parse_chain(self, chain: list) -> tuple[str, list[str]]:
        """解析消息链，返回 (文本, 图片URL列表)。"""
        text_parts: list[str] = []
        images: list[str] = []
        for comp in chain:
            if isinstance(comp, Plain) and comp.text:
                text_parts.append(comp.text.strip())
            elif isinstance(comp, Image):
                url = getattr(comp, "url", None) or getattr(comp, "file", None)
                if url:
                    images.append(url)
        return " ".join(p for p in text_parts if p).strip(), images

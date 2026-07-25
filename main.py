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
DEFAULT_SYSTEM_PROMPT = (
    "你是群聊事实核查助手。你只能依据用户提供的「待核内容」「图片」和「参考资料」作答，"
    "不要假装自己刚刚联网搜索，也不要编造不存在的链接或新闻。\n"
    "判断原则：\n"
    "1. 只核查可验证的事实主张；主观观点、价值判断、玩笑、预测优先 unknown。\n"
    "2. 若内容含多条主张，只核查最核心、最可验证的一条，并在解释中点明。\n"
    "3. 涉及时效信息（股价、比分、突发新闻、最新政策等）且没有可用参考资料时，优先 unknown，"
    "不要用过期记忆硬判。\n"
    "4. 图片仅在可辨认关键文字/图表时作为证据；看不清就说明限制。\n"
    "5. 必须严格按以下格式输出，第一行只能是单个英文单词：\n"
    "第一行：true / false / unknown\n"
    "第二行起：中文解释，100字以内，说明依据与不确定点。"
)


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
    "1.2.0",
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
        self.trigger_phrases: tuple[str, ...] = self._normalize_trigger_phrases(
            config.get("trigger_phrases", DEFAULT_TRIGGER_PHRASES)
        )
        self.group_blacklist: set[str] = self._parse_id_set(
            config.get("group_blacklist", [])
        )
        self.enable_web_search: bool = bool(config.get("enable_web_search", False))
        self.search_timeout: int = max(5, int(config.get("search_timeout", 30) or 30))
        self.max_content_chars: int = max(
            200, int(config.get("max_content_chars", 2500) or 2500)
        )
        self.max_search_chars: int = max(
            200, int(config.get("max_search_chars", 2000) or 2000)
        )
        self.true_label: str = (
            str(config.get("true_label", DEFAULT_TRUE_LABEL) or DEFAULT_TRUE_LABEL).strip()
            or DEFAULT_TRUE_LABEL
        )
        self.false_label: str = (
            str(config.get("false_label", DEFAULT_FALSE_LABEL) or DEFAULT_FALSE_LABEL).strip()
            or DEFAULT_FALSE_LABEL
        )
        self.unknown_label: str = (
            str(
                config.get("unknown_label", DEFAULT_UNKNOWN_LABEL) or DEFAULT_UNKNOWN_LABEL
            ).strip()
            or DEFAULT_UNKNOWN_LABEL
        )
        self.system_prompt: str = str(
            config.get("system_prompt", DEFAULT_SYSTEM_PROMPT) or DEFAULT_SYSTEM_PROMPT
        ).strip() or DEFAULT_SYSTEM_PROMPT
        self._cooldowns: dict[str, float] = {}

    async def initialize(self) -> None:
        logger.info(
            f"{LOG_PREFIX} 插件已加载 v1.2.0 | triggers={list(self.trigger_phrases)} "
            f"web_search={self.enable_web_search} vision={self.enable_vision} "
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
        text = bundle["text"]
        images = bundle["images"]
        supplement = bundle["supplement"]
        source = bundle["source"]
        image_note = bundle["image_note"]

        logger.info(
            f"{LOG_PREFIX} 提取 | source={source} text_len={len(text)} "
            f"images={len(images)} supplement_len={len(supplement)} note={image_note!r}"
        )

        if not text and not images:
            yield event.plain_result(
                "没有找到有效的文本或图片内容，请引用一条消息或直接在艾特后发送内容。"
            )
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

        search_block = ""
        if self.enable_web_search:
            search_query = await self._build_search_query(
                provider, text=text, images=images, supplement=supplement
            )
            if search_query:
                logger.info(f"{LOG_PREFIX} 准备联网搜索：{search_query!r}")
                search_block = await self._web_search(search_query, event=event)
                if search_block:
                    logger.info(
                        f"{LOG_PREFIX} 搜索成功 len={len(search_block)} "
                        f"preview={search_block[:120]!r}"
                    )
                else:
                    notes.append("联网搜索未返回可用资料，已回退模型知识判断。")
                    logger.info(f"{LOG_PREFIX} 搜索空结果，回退兜底")
            else:
                notes.append("未能生成有效搜索词，已跳过联网搜索。")
                logger.info(f"{LOG_PREFIX} 无有效搜索词，跳过搜索")
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
        )
        image_urls = images if self.enable_vision else []

        try:
            llm_resp = await provider.text_chat(
                prompt=prompt,
                image_urls=image_urls,
                system_prompt=self.system_prompt,
            )
            content = (llm_resp.completion_text or "").strip()
            logger.info(f"{LOG_PREFIX} 模型返回：{content[:200]!r}")
        except Exception as e:  # noqa: BLE001
            err_msg = str(e)
            logger.exception(f"{LOG_PREFIX} 调用大模型失败: {err_msg}")
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

    def _build_user_prompt(
        self,
        *,
        source: str,
        text: str,
        images: list[str],
        supplement: str,
        search_block: str,
        notes: list[str],
    ) -> str:
        claim_text = text or "（无文本，请主要依据图片判断）"
        claim_text = self._truncate(claim_text, self.max_content_chars)
        supplement = self._truncate(supplement, min(500, self.max_content_chars // 2))
        search_block = self._truncate(search_block, self.max_search_chars)

        parts = [
            "【任务】请核查下列内容的真实性，并严格按系统要求的格式输出。",
            f"【来源】{source}",
        ]
        if supplement:
            parts.append(f"【用户补充】{supplement}")
        parts.append(f"【待核文本】{claim_text}")
        if images:
            if self.enable_vision:
                parts.append(f"【图片】共 {len(images)} 张，已随请求附带，请结合图片内容。")
            else:
                parts.append(f"【图片】共 {len(images)} 张，但当前未启用图片分析。")
        if search_block:
            parts.append(f"【参考资料】\n{search_block}")
        else:
            parts.append("【参考资料】无")
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
            if rest_lines and self._parse_verdict_token(rest_lines[0].strip()) == verdict:
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

    async def _build_search_query(
        self,
        provider,
        *,
        text: str,
        images: list[str],
        supplement: str,
    ) -> str:
        base = "\n".join(p for p in (text, supplement) if p).strip()
        if base and len(base) <= 80:
            return re.sub(r"\s+", " ", base)[:80]

        if base:
            extracted = await self._llm_extract_claim(provider, base, images=images)
            if extracted:
                return extracted
            compact = re.sub(r"\s+", " ", base).strip()
            return compact[:80]

        if images and self.enable_vision:
            return await self._query_from_images(provider, images)
        return ""

    async def _llm_extract_claim(
        self, provider, text: str, images: list[str] | None = None
    ) -> str:
        snippet = self._truncate(text, 1200)
        try:
            resp = await provider.text_chat(
                prompt=(
                    "从下列内容中提取最值得事实核查的一条核心主张，改写成可搜索的中文短句。"
                    "只输出短句本身，不超过40字，不要解释。\n\n"
                    f"{snippet}"
                ),
                image_urls=(images[:3] if (images and self.enable_vision) else []),
            )
            out = (resp.completion_text or "").strip().replace("\n", " ")
            out = re.sub(r"\s+", " ", out).strip(" \"'`")
            return out[:60]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{LOG_PREFIX} 提取核查主张失败：{e}")
            return ""

    async def _query_from_images(self, provider, images: list[str]) -> str:
        if not self.enable_vision:
            return ""
        try:
            resp = await provider.text_chat(
                prompt=(
                    "请用一句话（30字以内）概括图片中最关键、最适合联网核查的事实主张，"
                    "只输出该句子本身，不要解释。"
                ),
                image_urls=images[:3],
            )
            return (resp.completion_text or "").strip().replace("\n", " ")[:60]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{LOG_PREFIX} 从图片提取关键词失败：{e}")
            return ""

    async def _web_search(
        self, query: str, event: AstrMessageEvent | None = None
    ) -> str:
        query = (query or "").strip()
        if not query:
            return ""
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
            logger.warning(f"{LOG_PREFIX} 联网搜索超时，回退兜底：{e}")
            return ""
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{LOG_PREFIX} 联网搜索异常，回退兜底：{e}")
            return ""

    @staticmethod
    def _read_anysearch_api_key() -> str:
        candidates = []
        try:
            data_path = Path(get_astrbot_data_path())
            candidates.extend(
                [
                    data_path / "config" / "astrbot_plugin_anysearch_config.json",
                    data_path / "plugin_configs" / "astrbot_plugin_anysearch_config.json",
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

        forward_text, forward_images, forward_note = await self._extract_forward_from_chain(
            event, chain
        )
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
    async def _extract_forward_from_chain(
        self, event: AstrMessageEvent, chain: list
    ) -> tuple[str, list[str], str]:
        texts: list[str] = []
        images: list[str] = []
        note = ""

        for comp in chain:
            if isinstance(comp, Nodes):
                t, imgs = self._parse_nodes(comp)
                if t:
                    texts.append(t)
                images.extend(imgs)
            elif isinstance(comp, Node):
                t, imgs = self._parse_node(comp)
                if t:
                    texts.append(t)
                images.extend(imgs)
            elif isinstance(comp, Forward):
                fid = str(getattr(comp, "id", "") or "").strip()
                if not fid:
                    continue
                t, imgs, n = await self._fetch_forward_by_id(event, fid)
                if t:
                    texts.append(t)
                images.extend(imgs)
                if n:
                    note = n

        return "\n".join(texts).strip(), list(dict.fromkeys(images)), note

    async def _fetch_forward_by_id(
        self, event: AstrMessageEvent, forward_id: str
    ) -> tuple[str, list[str], str]:
        """Best-effort OneBot get_forward_msg for bare Forward components."""
        try:
            client = getattr(event, "bot", None)
            if client is None:
                return "", [], "合并转发未能展开（无可用 OneBot 客户端）。"

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
                return "", [], "合并转发展开失败。"

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
                    if not isinstance(msg, dict):
                        continue
                    sender = msg.get("sender") or {}
                    name = ""
                    if isinstance(sender, dict):
                        name = str(sender.get("nickname") or sender.get("card") or "")
                    content = msg.get("content") or msg.get("message") or []
                    t, imgs = self._parse_onebot_content(content)
                    if t:
                        text_parts.append(f"{name}: {t}" if name else t)
                    images.extend(imgs)
            return "\n".join(text_parts).strip(), list(dict.fromkeys(images)), ""
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{LOG_PREFIX} get_forward_msg 失败：{e}")
            return "", [], f"合并转发展开失败：{e}"

    def _parse_nodes(self, nodes: Nodes) -> tuple[str, list[str]]:
        parts: list[str] = []
        images: list[str] = []
        for node in getattr(nodes, "nodes", []) or []:
            t, imgs = self._parse_node(node)
            if t:
                parts.append(t)
            images.extend(imgs)
        return "\n".join(parts).strip(), images

    def _parse_node(self, node: Node) -> tuple[str, list[str]]:
        name = str(getattr(node, "name", "") or "").strip()
        content = getattr(node, "content", None) or []
        text, images = self._parse_chain(content)
        if text and name:
            text = f"{name}: {text}"
        return text, images

    def _parse_onebot_content(self, content: object) -> tuple[str, list[str]]:
        text_parts: list[str] = []
        images: list[str] = []
        if isinstance(content, str):
            return content.strip(), []
        if not isinstance(content, list):
            return "", []
        for seg in content:
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
                text_parts.append("[嵌套合并转发]")
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

    def _parse_chain(self, chain: list) -> tuple[str, list[str]]:
        text_parts: list[str] = []
        images: list[str] = []
        for comp in chain or []:
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
                t, imgs = self._parse_nodes(comp)
                if t:
                    text_parts.append(t)
                images.extend(imgs)
            elif isinstance(comp, Node):
                t, imgs = self._parse_node(comp)
                if t:
                    text_parts.append(t)
                images.extend(imgs)
        return " ".join(p for p in text_parts if p).strip(), list(dict.fromkeys(images))

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        text = text or ""
        if limit <= 0 or len(text) <= limit:
            return text
        keep = max(0, limit - 12)
        return text[:keep].rstrip() + "\n…(已截断)"
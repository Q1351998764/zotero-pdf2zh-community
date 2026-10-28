"""Process-local compatibility for pdf2zh-next's OpenAI translator.

Some OpenAI-compatible gateways wrap a Chat Completions object in ``data``.
pdf2zh-next 2.x (verified against 2.6.4 and 2.9.0) directly reads
``response.choices[0].message.content`` in both text and glossary requests.
OpenAI's SDK can deserialize such a wrapper without raising, leaving choices
unset and producing an unhelpful NoneType error in the translator.

This shim wraps only each OpenAITranslator instance's completions resource. It
does not change installed packages, the global SDK, or HTTP transports. It is
limited to the synchronous, non-streaming ``create`` contract used by 2.x;
streaming calls pass through. Revisit/remove it when upstream handles wrapped
responses and validates missing, empty, or truncated completions itself. An
upstream client-layout change fails explicitly instead of silently bypassing
the adapter.
"""

import importlib
from collections.abc import Mapping
from functools import wraps
from types import SimpleNamespace


class TranslationResponseError(RuntimeError):
    """A translation response cannot safely be used as a complete translation."""


def _field(value, name, default=None):
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _namespace(value):
    """Support plain test doubles without taking a dependency on OpenAI here."""
    if isinstance(value, Mapping):
        return SimpleNamespace(**{key: _namespace(item) for key, item in value.items()})
    if isinstance(value, list):
        return [_namespace(item) for item in value]
    return value


def normalize_chat_completion(response):
    """Unwrap a gateway envelope, validate the first choice, and retain usage.

    A normal top-level completion is returned unchanged. Only a missing/null
    top-level choices field permits the explicit ``data.choices`` envelope;
    an empty top-level list is an error, not a reason to choose other data.
    Raw responses, prompts and provider errors are deliberately excluded from
    diagnostic strings because they may contain credentials or document text.
    """
    choices = _field(response, "choices")
    envelope = _field(response, "data")
    if choices is None and isinstance(envelope, Mapping) and "choices" in envelope:
        payload = dict(envelope)
        # Some gateways put accounting outside the completion envelope.
        if payload.get("usage") is None and _field(response, "usage") is not None:
            payload["usage"] = _field(response, "usage")
        _validate_completion_text(payload)
        validate = getattr(type(response), "model_validate", None)
        try:
            response = validate(payload) if callable(validate) else _namespace(payload)
        except Exception:
            raise TranslationResponseError(
                "翻译接口的 data 包装不是有效的 Chat Completions 响应；"
                "请检查接口地址、模型和服务商的响应格式。"
            ) from None
    _validate_completion_text(response)
    return response


def _validate_completion_text(response):
    choices = _field(response, "choices")
    if choices is None:
        raise TranslationResponseError(
            "翻译接口未返回 choices（字段缺失或为 null）；"
            "请检查接口地址、模型权限及服务状态。这不是 PDF 文字读取错误。"
        )
    if not isinstance(choices, (list, tuple)) or not choices:
        raise TranslationResponseError(
            "翻译接口返回的 choices 为空或格式错误，没有可用译文；"
            "请检查模型和服务状态。"
        )
    first = choices[0]
    if _field(first, "finish_reason") == "length":
        raise TranslationResponseError(
            "翻译接口返回 finish_reason=length，模型输出已被长度限制截断；"
            "请提高输出 token 上限、缩短输入或更换模型后重试。"
        )
    content = _field(_field(first, "message"), "content")
    if not isinstance(content, str) or not content.strip():
        raise TranslationResponseError(
            "翻译接口未返回有效的 message.content 文本（缺失、null 或空内容）；"
            "请检查模型是否支持文本翻译，以及输出限制和服务状态。"
        )


class _ValidatedCompletions:
    """Delegate the resource unchanged except for non-streaming responses."""

    def __init__(self, resource):
        self._resource = resource

    def __getattr__(self, name):
        return getattr(self._resource, name)

    def create(self, *args, **kwargs):
        response = self._resource.create(*args, **kwargs)
        if kwargs.get("stream"):
            return response
        return normalize_chat_completion(response)


def install_openai_response_compat(translator_class=None):
    """Install once before the engine constructs any translator instances.

    Passing a class permits dependency-free contract tests. Production resolves
    pdf2zh-next's class lazily, after the runner selected its managed Python.
    """
    if translator_class is None:
        module = importlib.import_module("pdf2zh_next.translator.translator_impl.openai")
        translator_class = module.OpenAITranslator

    original_init = translator_class.__init__
    if getattr(original_init, "_zotero_pdf2zh_response_compat", False):
        return False

    @wraps(original_init)
    def initialize(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        chat = getattr(getattr(self, "client", None), "chat", None)
        completions = getattr(chat, "completions", None)
        if not callable(getattr(completions, "create", None)):
            raise RuntimeError(
                "当前 pdf2zh-next 的 OpenAI 客户端结构与兼容层不匹配；"
                "请更新维护版或使用受支持的引擎版本。"
            )
        if not isinstance(completions, _ValidatedCompletions):
            chat.completions = _ValidatedCompletions(completions)

    initialize._zotero_pdf2zh_response_compat = True
    translator_class.__init__ = initialize
    return True

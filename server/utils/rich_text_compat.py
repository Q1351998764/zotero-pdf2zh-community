"""Restore equivalent quotes on injected BabelDOC rich-text placeholders.

BabelDOC 0.6.2 / pdf2zh-next 2.9.0 inject ``<style id='N'>`` but their
parser only accepts single quotes. Some models return the HTML-equivalent
double-quoted tag; its opening tag then becomes visible PDF text. Normalize
only IDs that the current paragraph actually injected, immediately before
the original parser. This is not a general HTML sanitizer or tag remover.
Literal source tags, unknown IDs, and missing/malformed tags are left alone.
Remove this adapter when the supported upstream parser accepts both quotes.
"""

import importlib
import importlib.metadata
import re
from functools import wraps


_DOUBLE_QUOTED_STYLE = re.compile(r'<\s*style\s+id\s*=\s*"([0-9]+)"\s*>', re.I)
_SUPPORTED_BABELDOC = {"0.6.2"}


def restore_injected_style_quotes(input_text, output):
    """Restore known injected opening tags without removing any text.

    The same double-quoted tag may legitimately occur in a document about
    HTML. If that ID is present as a literal in the source, its identity is
    ambiguous and this adapter deliberately leaves it unchanged.
    """
    source = getattr(input_text, "unicode", None)
    if not isinstance(source, str) or not isinstance(output, str):
        return output

    literal_ids = set(_DOUBLE_QUOTED_STYLE.findall(source))
    replacements = {}
    for placeholder in getattr(input_text, "placeholders", ()):
        identifier = getattr(placeholder, "id", None)
        if type(identifier) is not int or identifier < 0:
            continue
        identifier = str(identifier)
        canonical = f"<style id='{identifier}'>"
        if (
            identifier not in literal_ids
            and getattr(placeholder, "left_placeholder", None) == canonical
            and getattr(placeholder, "right_placeholder", None) == "</style>"
            and canonical in source
        ):
            replacements[identifier] = canonical

    if not replacements:
        return output
    return _DOUBLE_QUOTED_STYLE.sub(
        lambda match: replacements.get(match[1], match[0]), output
    )


def install_rich_text_compat(translator_class=None):
    """Install once, scoped to the verified upstream parser contract.

    Explicit classes support dependency-free tests. Unknown or absent engine
    versions keep their original implementation; the runner can load older
    healthy environments without imposing this version-specific adapter.
    """
    if translator_class is None:
        try:
            version = importlib.metadata.version("BabelDOC")
        except importlib.metadata.PackageNotFoundError:
            return False
        if version not in _SUPPORTED_BABELDOC:
            return False
        module = importlib.import_module(
            "babeldoc.format.pdf.document_il.midend.il_translator"
        )
        translator_class = module.ILTranslator

    original = translator_class.parse_translate_output
    if getattr(original, "_zotero_pdf2zh_style_quote_compat", False):
        return False

    @wraps(original)
    def parse(self, input_text, output, *args, **kwargs):
        return original(
            self, input_text, restore_injected_style_quotes(input_text, output),
            *args, **kwargs,
        )

    parse._zotero_pdf2zh_style_quote_compat = True
    translator_class.parse_translate_output = parse
    return True

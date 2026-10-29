import importlib.util
import re
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from utils.rich_text_compat import install_rich_text_compat, restore_injected_style_quotes


def injected(identifier):
    return SimpleNamespace(
        id=identifier, left_placeholder=f"<style id='{identifier}'>",
        right_placeholder="</style>",
    )


def paragraph(*identifiers, suffix=""):
    return SimpleNamespace(
        unicode=" ".join(f"<style id='{identifier}'>Source</style>" for identifier in identifiers) + suffix,
        placeholders=[injected(identifier) for identifier in identifiers],
    )


class RichTextQuoteTest(unittest.TestCase):
    def test_equivalent_double_quotes_restore_only_injected_ids(self):
        source = paragraph(1, 3)
        translated = '<style id="1">译文一</style> <STYLE id = "3">译文三</style>'
        self.assertEqual(
            restore_injected_style_quotes(source, translated),
            "<style id='1'>译文一</style> <style id='3'>译文三</style>",
        )

    def test_unknown_ids_malformed_tags_and_formula_are_unchanged(self):
        translated = '<style id="2">正文</style> <style id="1" extra="x">额外属性</style> {v5}'
        self.assertEqual(restore_injected_style_quotes(paragraph(1), translated), translated)

    def test_literal_source_tag_is_not_changed_even_with_same_injected_id(self):
        source = paragraph(1, 3, suffix=' HTML example: <style id="1">literal</style>')
        translated = '<style id="1">字面内容</style> <style id="3">译文</style>'
        self.assertEqual(
            restore_injected_style_quotes(source, translated),
            '<style id="1">字面内容</style> ' + "<style id='3'>译文</style>",
        )

    def test_no_placeholders_or_missing_source_token_cannot_authorize_rewrite(self):
        translated = '<style id="1">译文</style>'
        for source in (
            SimpleNamespace(unicode="Plain source", placeholders=[]),
            SimpleNamespace(unicode="Plain source", placeholders=[injected(1)]),
            SimpleNamespace(unicode="<style id='1'>Source</style>", placeholders=[injected("1")]),
        ):
            with self.subTest(source=source):
                self.assertEqual(restore_injected_style_quotes(source, translated), translated)

    def test_missing_tags_are_not_added_and_restoration_is_idempotent(self):
        source = paragraph(1)
        for translated in ("译文", "<style id='1'>译文</style>", '<style id="1">译文</style>'):
            with self.subTest(translated=translated):
                normalized = restore_injected_style_quotes(source, translated)
                self.assertEqual(restore_injected_style_quotes(source, normalized), normalized)
        self.assertEqual(restore_injected_style_quotes(source, "译文"), "译文")

    def test_installer_delegates_arguments_return_value_and_is_idempotent(self):
        calls = []

        class Parser:
            def parse_translate_output(self, input_text, output, tracker=None, llm_translate_tracker=None):
                calls.append((input_text, output, tracker, llm_translate_tracker))
                return "parser-result"

        self.assertTrue(install_rich_text_compat(Parser))
        self.assertFalse(install_rich_text_compat(Parser))
        source = paragraph(1)
        result = Parser().parse_translate_output(
            input_text=source, output='<style id="1">译文</style>', tracker="tracker",
            llm_translate_tracker="llm-tracker",
        )
        self.assertEqual(result, "parser-result")
        self.assertEqual(calls, [(source, "<style id='1'>译文</style>", "tracker", "llm-tracker")])

    def test_unverified_upstream_version_does_not_import_or_patch_parser(self):
        with (
            patch("utils.rich_text_compat.importlib.metadata.version", return_value="99.0.0"),
            patch("utils.rich_text_compat.importlib.import_module") as load,
        ):
            self.assertFalse(install_rich_text_compat())
        load.assert_not_called()


@unittest.skipUnless(
    importlib.util.find_spec("babeldoc") and importlib.util.find_spec("pdf2zh_next"),
    "optional actual parser contract requires the full engine environment",
)
class RealRichTextParserTest(unittest.TestCase):
    def test_actual_parser_preserves_styled_text_with_double_quoted_id(self):
        from babeldoc.format.pdf.document_il import PdfStyle, PdfSameStyleCharacters
        from babeldoc.format.pdf.document_il.midend.il_translator import ILTranslator, RichTextPlaceholder
        from pdf2zh_next.translator.base_translator import BaseTranslator

        original = ILTranslator.parse_translate_output
        self.addCleanup(setattr, ILTranslator, "parse_translate_output", original)
        if getattr(original, "_zotero_pdf2zh_style_quote_compat", False):
            ILTranslator.parse_translate_output = original.__wrapped__
        # Avoid fonts, tokenizers, translation clients and asset downloads. Only
        # exercise the actual installed parser and generated placeholder regexes.
        parser = ILTranslator.__new__(ILTranslator)
        parser._formula_placeholder_pattern = re.compile(
            BaseTranslator.get_formular_placeholder(None, r"\d+")[1], re.I
        )
        parser._style_left_placeholder_pattern = re.compile(
            BaseTranslator.get_rich_text_left_placeholder(None, r"\d+")[1], re.I
        )
        parser._style_right_placeholder_pattern = re.compile(
            BaseTranslator.get_rich_text_right_placeholder(None, r"\d+")[1], re.I
        )
        base_style = PdfStyle(font_id="base", font_size=10)
        rich_style = PdfStyle(font_id="rich", font_size=12)
        composition = PdfSameStyleCharacters(pdf_style=rich_style, pdf_character=[])
        left, left_pattern = BaseTranslator.get_rich_text_left_placeholder(None, 1)
        right, right_pattern = BaseTranslator.get_rich_text_right_placeholder(None, 1)
        placeholder = RichTextPlaceholder(1, composition, left, right, left_pattern, right_pattern)
        source = ILTranslator.TranslateInput("<style id='1'>Source</style>", [placeholder], base_style)
        output = '<style id="1">译文</style>'
        before = parser.parse_translate_output(source, output)
        self.assertEqual(before[0].pdf_same_style_unicode_characters.unicode, '<style id="1">译文')

        install_rich_text_compat(ILTranslator)
        after = parser.parse_translate_output(source, output)
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0].pdf_same_style_unicode_characters.unicode, "译文")
        self.assertIs(after[0].pdf_same_style_unicode_characters.pdf_style, rich_style)


if __name__ == "__main__":
    unittest.main()

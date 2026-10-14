"""Check the published guides and the renderer's failure behavior."""

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

from scripts import render_wiki


def headings(text):
    """GitHub heading IDs used by our plain-text Markdown headings."""
    counts = {}
    result = set()
    for line, code in render_wiki.markdown_lines(text):
        match = re.match(r"^#{1,6} (.+?)\s*#*\s*$", line) if not code else None
        if match:
            slug = re.sub(r"[^\w\- ]", "", match[1].lower()).replace(" ", "-")
            count = counts.get(slug, 0)
            counts[slug] = count + 1
            result.add(f"{slug}-{count}" if count else slug)
    return result


class WikiTests(unittest.TestCase):
    def test_current_guides_render_with_working_internal_links(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            count = render_wiki.render(output)
            pages = {path.stem: path.read_text() for path in output.glob("*.md")}
            self.assertEqual(count, len(render_wiki.PAGES) + 1)
            self.assertEqual(set(pages), set(render_wiki.PAGES.values()) | {"_Sidebar"})
            for page, body in pages.items():
                for line, code in render_wiki.markdown_lines(body):
                    if code:
                        continue
                    for match in render_wiki.LINK.finditer(line):
                        target = urlsplit(match[2])
                        with self.subTest(page=page, link=match[2]):
                            self.assertTrue(target.scheme or not target.path,
                                            "Rendered links must be absolute or page anchors")
                            destination = None
                            if target.path.lower().startswith("/icicle-ai/cicd-templates/wiki/"):
                                destination = unquote(target.path.rsplit("/", 1)[1])
                                self.assertIn(destination, pages)
                            elif not target.path:
                                destination = page
                            if destination and target.fragment:
                                self.assertIn(unquote(target.fragment), headings(pages[destination]))

    def test_code_examples_are_preserved_exactly(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            render_wiki.render(output)
            for source, page in render_wiki.PAGES.items():
                before = (render_wiki.ROOT / source).read_text()
                after = (output / f"{page}.md").read_text()
                source_code = "".join(line for line, code in render_wiki.markdown_lines(before) if code)
                wiki_code = "".join(line for line, code in render_wiki.markdown_lines(after) if code)
                with self.subTest(source=source):
                    self.assertEqual(source_code, wiki_code)

    def test_relative_page_and_repository_links_keep_anchors(self):
        actual = render_wiki.rewrite_links(
            "[Setup](quick-start.md#configure) [Script](../scripts/render_wiki.py#L1)",
            "docs/home.md",
        )
        self.assertEqual(actual,
                         f"[Setup]({render_wiki.WIKI}/Quick-start#configure) "
                         f"[Script]({render_wiki.REPOSITORY}/blob/main/scripts/render_wiki.py#L1)")

    def test_fences_do_not_rewrite_code_links(self):
        example = "````markdown\n[Example](missing.md)\n```\n````\n~~~text\n[x](no.md)\n~~~\n"
        self.assertEqual(render_wiki.rewrite_links(example, "docs/home.md"), example)

    def test_invalid_source_does_not_modify_existing_output(self):
        for invalid in ("[Broken](missing.md)\n", "```python\nunclosed\n"):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "good.md").write_text("# Good\n")
                (root / "bad.md").write_text(invalid)
                output = root / "published"
                output.mkdir()
                (output / "Good.md").write_text("Previously published\n")
                with patch.object(render_wiki, "ROOT", root), patch.object(
                    render_wiki, "PAGES", {"good.md": "Good", "bad.md": "Bad"}
                ), self.assertRaises(ValueError):
                    render_wiki.render(output)
                self.assertEqual(list(output.iterdir()), [output / "Good.md"])
                self.assertEqual((output / "Good.md").read_text(), "Previously published\n")


if __name__ == "__main__":
    unittest.main()

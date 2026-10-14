"""Render the GitHub wiki from the repository's authoritative Markdown files."""

import argparse
import posixpath
import re
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "https://github.com/icicle-ai/cicd-templates"
WIKI = f"{REPOSITORY}/wiki"
PAGES = {
    "docs/home.md": "Home",
    "docs/quick-start.md": "Quick-start",
    "docs/configuration.md": "Configuration",
    "docs/deployment.md": "Deployment",
    "docs/versioning.md": "Versioning",
    "python/service-setup.md": "Python",
    "node-backend/service-setup.md": "Node-backend",
    "node-frontend/service-setup.md": "Static-frontends",
    "CHANGELOG.md": "Release-notes",
}
LINK = re.compile(r"(!?\[[^\]\n]+\]\()([^\s)]+)(\))")


def markdown_lines(text):
    """Identify fenced code without modifying its contents."""
    fence = None
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
        if fence:
            yield line, True
            if (
                marker
                and marker[1][0] == fence[0]
                and len(marker[1]) >= len(fence)
                and not marker[2].strip()
            ):
                fence = None
        elif marker:
            fence = marker[1]
            yield line, True
        else:
            yield line, False
    if fence:
        raise ValueError("Unclosed Markdown code fence")


def rewrite_links(text, source):
    def replace(match):
        target = urlsplit(match[2])
        if target.scheme or target.netloc or not target.path:
            return match[0]
        path = posixpath.normpath(
            posixpath.join(posixpath.dirname(source), unquote(target.path))
        )
        fragment = f"#{target.fragment}" if target.fragment else ""
        if path in PAGES:
            url = f"{WIKI}/{PAGES[path]}{fragment}"
        else:
            if path.startswith("../") or not (ROOT / path).is_file():
                raise ValueError(f"Missing repository link in {source}: {match[2]}")
            url = f"{REPOSITORY}/blob/main/{quote(path)}{fragment}"
        if target.query:
            raise ValueError(f"Unsupported relative link query in {source}: {match[2]}")
        return f"{match[1]}{url}{match[3]}"

    return "".join(
        line if code else LINK.sub(replace, line) for line, code in markdown_lines(text)
    )


def render(output):
    rendered = {}
    for source, page in PAGES.items():
        body = (ROOT / source).read_text(encoding="utf-8")
        footer = f"_Source: [{source}]({REPOSITORY}/blob/main/{source}). Edit the repository file; changes on main publish here automatically._"
        rendered[page] = f"{rewrite_links(body, source).rstrip()}\n\n---\n\n{footer}\n"
    navigation = (
        ("Home", "Home"),
        ("Quick-start", "Quick start"),
        ("Python", "Python"),
        ("Node-backend", "Node backend"),
        ("Static-frontends", "Static frontends"),
        ("Configuration", "Configuration"),
        ("Deployment", "Deployment"),
        ("Versioning", "Versioning and maintenance"),
        ("Release-notes", "Release notes"),
    )
    rendered["_Sidebar"] = (
        "\n".join(f"- [{title}]({WIKI}/{page})" for page, title in navigation) + "\n"
    )
    output.mkdir(parents=True, exist_ok=True)
    for page, content in rendered.items():
        (output / f"{page}.md").write_text(content, encoding="utf-8")
    return len(rendered)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "output", type=Path, help="Directory for generated wiki Markdown files"
    )
    args = parser.parse_args()
    try:
        count = render(args.output)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Cannot render wiki: {error}\n")
    print(f"Rendered {count} wiki files in {args.output}")


if __name__ == "__main__":
    main()

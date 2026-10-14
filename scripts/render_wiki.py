"""Render the GitHub wiki from the repository's authoritative Markdown files."""

import argparse
import posixpath
import re
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "https://github.com/icicle-ai/cicd-templates"
WIKI = f"{REPOSITORY}/wiki"
SECTIONS = {
    "Quick start": "Home",
    "Configuration": "Configuration",
    "Deployment": "Deployment",
    "Versioning": "Versioning",
    "Maintaining the templates": "Versioning",
}
GUIDES = {
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


def split_readme(text):
    sections = {"Introduction": []}
    current = "Introduction"
    for line, code in markdown_lines(text):
        if not code and line.startswith("## "):
            current = line[3:].strip()
            if current in sections:
                raise ValueError(f"Duplicate README section: {current}")
            sections[current] = []
        sections[current].append(line)
    if list(sections)[1:] != list(SECTIONS):
        raise ValueError(
            "README sections changed; update SECTIONS and the page map in render_wiki.py. "
            f"Expected: {', '.join(SECTIONS)}"
        )
    return {title: "".join(lines).strip() for title, lines in sections.items()}


def rewrite_links(text, source):
    def replace(match):
        target = urlsplit(match[2])
        if target.scheme or target.netloc or not target.path:
            return match[0]
        path = posixpath.normpath(
            posixpath.join(posixpath.dirname(source), unquote(target.path))
        )
        fragment = f"#{target.fragment}" if target.fragment else ""
        if path == "README.md":
            anchors = {
                title.lower().replace(" ", "-"): page
                for title, page in SECTIONS.items()
            }
            if target.fragment and target.fragment not in anchors:
                raise ValueError(
                    f"Unknown README anchor in {source}: {target.fragment}"
                )
            url = f"{WIKI}/{anchors.get(target.fragment, 'Home')}{fragment}"
        elif path in GUIDES:
            url = f"{WIKI}/{GUIDES[path]}{fragment}"
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
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    sections = split_readme(readme)
    sections["Introduction"] = sections["Introduction"].replace(
        "\n\nRead these guides as separate pages in the [Wiki](https://github.com/ICICLE-ai/cicd-templates/wiki).",
        "",
    )
    support = next(
        (line for line in readme.splitlines() if line.startswith("For help, ")), None
    )
    if not support:
        raise ValueError("README support line is missing; update the Home page map")
    pages = {
        "Home": (
            "README.md",
            "\n\n".join((sections["Introduction"], sections["Quick start"], support)),
        ),
        "Configuration": ("README.md", sections["Configuration"]),
        "Deployment": ("README.md", sections["Deployment"]),
        "Versioning": (
            "README.md",
            "\n\n".join(
                (sections["Versioning"], sections["Maintaining the templates"])
            ),
        ),
    }
    for source, page in GUIDES.items():
        pages[page] = (source, (ROOT / source).read_text(encoding="utf-8"))

    rendered = {}
    for page, (source, body) in pages.items():
        footer = f"_Source: [{source}]({REPOSITORY}/blob/main/{source}). Edit the repository file; changes on main publish here automatically._"
        rendered[page] = f"{rewrite_links(body, source).rstrip()}\n\n---\n\n{footer}\n"
    navigation = (
        ("Home", "Quick start"),
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

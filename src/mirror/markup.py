import re
from collections.abc import Callable
from html import escape
from html.parser import HTMLParser

from markdown_it import MarkdownIt
from markdown_it.token import Token


class Sanitizer(HTMLParser):
    """Rebuilds HTML from an allowlist of tags and attributes, in the spirit of GitHub's sanitization."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._tags = {
            "a", "abbr", "b", "blockquote", "br", "code", "dd", "del", "details", "div", "dl",
            "dt", "em", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "ins", "kbd",
            "li", "ol", "p", "picture", "pre", "q", "s", "samp", "source", "span", "strike",
            "strong", "sub", "summary", "sup", "table", "tbody", "td", "tfoot", "th", "thead",
            "tr", "tt", "u", "ul",
        }
        self._void = {"br", "hr", "img", "source"}
        self._drop = {"script", "style", "iframe", "object", "embed", "noscript", "template", "textarea", "select"}
        self._attributes = {
            "a": {"href", "title", "target", "rel", "class"},
            "img": {"src", "alt", "title", "width", "height", "align"},
            "source": {"srcset", "media", "type"},
            "td": {"align", "colspan", "rowspan"},
            "th": {"align", "colspan", "rowspan"},
            "ol": {"start", "type"},
            "details": {"open"},
            "div": {"align", "class"},
            "p": {"align"},
            "h1": {"align"}, "h2": {"align"}, "h3": {"align"},
            "code": {"class"},
            "pre": {"class"},
            "span": {"class"},
        }
        self._output: list[str] = []
        self._stack: list[str] = []
        self._skip = 0

    def clean(self, html: str) -> str:
        """Return ``html`` reduced to the allowlist with every opened tag closed."""
        self._output, self._stack, self._skip = [], [], 0
        self.feed(html)
        self.close()
        self._output.extend(f"</{tag}>" for tag in reversed(self._stack))
        return "".join(self._output)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._drop:
            self._skip += 1
            return

        if self._skip or tag not in self._tags:
            return

        allowed = self._attributes.get(tag, set())
        parts = [tag]

        for name, value in attrs:
            if name not in allowed:
                continue

            value = value or ""

            if name in ("href", "src", "srcset") and not self._safe(value):
                continue

            parts.append(f'{name}="{escape(value, quote=True)}"')

        if tag == "a" and any(part.startswith('href="http') for part in parts):
            parts.append('rel="noopener noreferrer"')

        self._output.append(f"<{' '.join(parts)}>")

        if tag not in self._void:
            self._stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

        if tag not in self._void and self._stack and self._stack[-1] == tag:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self._drop:
            self._skip = max(0, self._skip - 1)
            return

        if self._skip or tag not in self._stack:
            return

        while self._stack:
            current = self._stack.pop()
            self._output.append(f"</{current}>")

            if current == tag:
                break

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._output.append(escape(data, quote=False))

    def _safe(self, value: str) -> bool:
        lowered = value.strip().lower()
        return lowered.startswith(("http://", "https://", "mailto:", "/", "#", "./", "../")) or not re.match(
            r"^[a-z][a-z0-9+.-]*:", lowered
        )


class Linker:
    """Maps GitHub URLs and references of mirrored repositories onto mirror paths."""

    def __init__(self, repos: list[str], resolve: Callable[[str, str], str | None]) -> None:
        self._repos = {repo.lower(): repo for repo in repos}
        self._resolve = resolve
        self._url = re.compile(
            r"^https?://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?(/[^\s]*)?$",
            re.IGNORECASE,
        )

    def repo(self, name: str) -> str | None:
        """Return the canonical spelling of a mirrored repository name."""
        return self._repos.get(name.lower())

    def href(self, url: str) -> str:
        """Rewrite a github.com URL of a mirrored repository to its mirror path; leave anything else untouched."""
        match = self._url.match(url.strip())

        if not match:
            return url

        repo = self.repo(f"{match.group(1)}/{match.group(2)}")

        if repo is None:
            return url

        rest = match.group(3) or ""
        rest = re.sub(r"^/pulls/(\d+)", r"/pull/\1", rest)
        return f"/{repo}{rest}"

    def commit(self, repo: str, sha: str) -> str | None:
        """Return the full SHA when ``sha`` names a commit of the mirrored ``repo``."""
        return self._resolve(repo, sha)


class Markup:
    """Renders GitHub-flavored issue and pull request text into sanitized HTML with mirror links."""

    def __init__(self, linker: Linker) -> None:
        self._linker = linker
        self._parser = (
            MarkdownIt("commonmark", {"html": True, "breaks": True})
            .enable("table")
            .enable("strikethrough")
        )
        self._parser.core.ruler.push("mirror", self._link)
        self._pattern = re.compile(
            r"(?P<url>https?://[^\s<>\"'`]+)"
            r"|(?P<cross>(?<![\w/])(?P<owner>[\w.-]+)/(?P<name>[\w.-]+)(?:#(?P<cnum>\d+)|@(?P<csha>[0-9a-f]{7,40}))\b)"
            r"|(?P<issue>(?<![\w/&])#(?P<num>\d+)\b)"
            r"|(?P<mention>(?<![\w`])@(?P<login>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))(?![\w-]))"
            r"|(?P<sha>(?<![\w/@.])[0-9a-f]{7,40}(?![\w]))"
        )
        self._repo = ""

    def render(self, text: str | None, repo: str) -> str:
        """Return sanitized HTML of the Markdown ``text`` written in ``repo``."""
        if not text:
            return ""

        self._repo = repo
        return Sanitizer().clean(self._parser.render(text))

    def _link(self, state) -> None:
        for block in state.tokens:
            if block.type != "inline" or not block.children:
                continue

            children: list[Token] = []
            depth = 0

            for index, token in enumerate(block.children):
                if token.type == "link_open":
                    depth += 1
                    href = token.attrGet("href") or ""
                    rewritten = self._linker.href(href)
                    token.attrSet("href", rewritten)

                    if rewritten.startswith("http"):
                        token.attrSet("target", "_blank")
                elif token.type == "link_close":
                    depth -= 1

                if token.type == "text" and depth == 0:
                    text = token.content

                    if index == 0 and self._task(state.tokens, block):
                        text = re.sub(r"^\[ \] ", "☐ ", re.sub(r"^\[[xX]\] ", "☑ ", text))

                    children.extend(self._split(text, token))
                else:
                    children.append(token)

            block.children = children

    def _task(self, tokens: list[Token], block: Token) -> bool:
        position = tokens.index(block)
        return position >= 2 and tokens[position - 2].type == "list_item_open"

    def _split(self, text: str, origin: Token) -> list[Token]:
        tokens: list[Token] = []
        cursor = 0

        for match in self._pattern.finditer(text):
            target, label = self._target(match)

            if target is None:
                continue

            if match.start() > cursor:
                tokens.append(self._text(text[cursor : match.start()], origin))

            opening = Token("link_open", "a", 1)
            opening.attrSet("href", target)

            if target.startswith("http"):
                opening.attrSet("target", "_blank")

            tokens.extend([opening, self._text(label, origin), Token("link_close", "a", -1)])
            cursor = match.end()

            if match.group("url") and len(match.group("url")) > len(label):
                cursor = match.start() + len(label)

        if cursor < len(text):
            tokens.append(self._text(text[cursor:], origin))

        return tokens

    def _target(self, match: re.Match) -> tuple[str | None, str]:
        if match.group("url"):
            url = re.sub(r"[.,;:!?)\]'\"]+$", "", match.group("url"))
            return self._linker.href(url), url

        if match.group("cross"):
            repo = self._linker.repo(f"{match.group('owner')}/{match.group('name')}")
            label = match.group("cross")

            if match.group("cnum"):
                number = match.group("cnum")
                path = f"/{repo}/issues/{number}" if repo else None
                url = f"https://github.com/{match.group('owner')}/{match.group('name')}/issues/{number}"
                return path or url, label

            if repo is None:
                return None, label

            full = self._linker.commit(repo, match.group("csha"))
            return (f"/{repo}/commit/{full}", label) if full else (None, label)

        if match.group("issue"):
            return f"/{self._repo}/issues/{match.group('num')}", match.group("issue")

        if match.group("mention"):
            return f"https://github.com/{match.group('login')}", match.group("mention")

        sha = match.group("sha")
        full = self._linker.commit(self._repo, sha)

        if full is None:
            return None, sha

        return f"/{self._repo}/commit/{full}", sha[:7]

    def _text(self, content: str, origin: Token) -> Token:
        token = Token("text", "", 0)
        token.content = content
        token.level = origin.level
        return token

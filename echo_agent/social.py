"""Social drafts: generate platform-fit posts and queue them for approval."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List

from .config import Config
from .docs import parse_frontmatter
from .llm import LLM
from .util import now, slugify

PLATFORMS = {
    "x": 280,
    "twitter": 280,
    "bluesky": 300,
    "threads": 500,
    "linkedin": 3000,
    "instagram": 2200,
    "facebook": 63206,
    "reddit": 40000,
}

PLATFORM_STYLE = {
    "x": "short, punchy, one idea, no hashtag stuffing",
    "twitter": "short, punchy, one idea, no hashtag stuffing",
    "bluesky": "conversational, thoughtful, no hashtag stuffing",
    "threads": "conversational and friendly, 1-2 hashtags at most",
    "linkedin": "professional but human, a clear hook, short paragraphs, no cringe",
    "instagram": "visual, warm, caption-friendly, line breaks, 3-5 hashtags",
    "facebook": "friendly and direct, community-oriented",
    "reddit": "plain-spoken and non-promotional, value first, no hashtags",
}

SYSTEM = (
    "You are Echo, ghost-writing social posts for the user. Return ONLY the "
    "post text, ready to paste. No preamble, explanation, markdown fences or "
    "quotes. Never invent achievements, numbers or client names that are not "
    "in the notes. Avoid emojis unless the notes explicitly use them. Keep the "
    "user's voice natural and specific."
)


class SocialError(Exception):
    pass


@dataclass
class Draft:
    path: Path
    platform: str
    topic: str
    content: str
    limit: int
    warning: str = ""

    @property
    def chars(self) -> int:
        return len(self.content)


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()


def _post_prompt(platform: str, limit: int, topic: str, notes: str, tone: str, thread: int) -> str:
    lines = [
        "Platform: %s (%d character limit per post)" % (platform, limit),
        "Style: %s" % PLATFORM_STYLE.get(platform, "clear and human"),
        "Tone: %s" % tone,
        "Topic: %s" % topic,
    ]
    if notes:
        lines.append("Raw notes / key points:\n%s" % notes)
    if thread > 1:
        lines.append(
            "Write a thread of exactly %d posts. Number them '1/%d', '2/%d', ... "
            "and separate posts with a line containing only three dashes (---). "
            "Every post must be under %d characters. The first post must earn "
            "the click." % (thread, thread, thread, limit)
        )
    else:
        lines.append(
            "Write one post under %d characters. Put the hook in the first "
            "line; make every sentence earn its place." % limit
        )
    lines.append("Return only the post text.")
    return "\n".join(lines)


def _generate(llm: LLM, platform: str, limit: int, topic: str, notes: str, tone: str, thread: int) -> str:
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": _post_prompt(platform, limit, topic, notes, tone, thread)},
    ]
    content = _strip_fences(llm.chat(messages).get("content") or "")
    if not content:
        raise SocialError("The model returned an empty draft.")
    if _over_limit(content, limit, thread):
        retry = dict(
            platform=platform,
            limit=limit,
            topic=topic,
            notes=notes,
            tone=tone,
            thread=thread,
        )
        shorter = _post_prompt(**retry) + (
            "\nYour previous draft was over the limit. Shorten it; hard maximum "
            "%d characters per post." % limit
        )
        content = _strip_fences(
            llm.chat(
                [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": shorter},
                ]
            ).get("content")
            or content
        )
    return content


def _parts(content: str, thread: int) -> List[str]:
    if thread > 1:
        parts = [p.strip() for p in re.split(r"\n\s*---\s*\n", content) if p.strip()]
        return parts or [content.strip()]
    return [content.strip()]


def _over_limit(content: str, limit: int, thread: int) -> bool:
    return any(len(part) > limit for part in _parts(content, thread))


def draft_post(
    cfg: Config,
    llm: LLM,
    platform: str,
    topic: str,
    notes: str = "",
    tone: str = "professional",
    thread: int = 0,
) -> Draft:
    platform = (platform or "").lower().strip()
    if platform in {"twitter", "tweet"}:
        platform = "x"
    if platform not in PLATFORMS:
        raise SocialError(
            "Unknown platform %r. Choose one of: %s"
            % (platform, ", ".join(sorted(PLATFORMS)))
        )
    topic = (topic or "").strip()
    if not topic:
        raise SocialError("Give me a topic or angle for the post.")
    thread = max(0, int(thread or 0))
    limit = PLATFORMS[platform]
    content = _generate(llm, platform, limit, topic, notes, tone, thread)

    warning = ""
    parts = _parts(content, thread)
    if thread > 1 and len(parts) != thread:
        warning = "Asked for %d posts, got %d." % (thread, len(parts))
    long_parts = [str(len(part)) for part in parts if len(part) > limit]
    if long_parts:
        extra = " (%s chars)" % ", ".join(long_parts)
        warning = (warning + " " if warning else "") + "Over the %d character limit%s." % (limit, extra)

    cfg.ensure_dirs()
    stamp = now()
    filename = "%s-%s.md" % (stamp.replace(":", "").replace("+", "_")[:15], platform)
    path = cfg.outbox_dir / filename
    title = "%s post: %s" % (platform, topic.replace("\n", " ").strip()[:60])
    body = "\n".join(
        [
            "---",
            "title: %s" % title,
            "platform: %s" % platform,
            "status: draft",
            "tone: %s" % tone.replace("\n", " "),
            "tags: social, %s, %s" % (platform, slugify(tone)),
            "created: %s" % stamp,
            "---",
            "",
            content.strip(),
            "",
        ]
    )
    path.write_text(body, encoding="utf-8")
    return Draft(
        path=path,
        platform=platform,
        topic=topic,
        content=content,
        limit=limit,
        warning=warning,
    )


def _path_for(cfg: Config, name: str) -> Path:
    candidate = Path(name)
    if candidate.suffix != ".md":
        candidate = Path(name + ".md")
    if not candidate.is_absolute():
        candidate = cfg.outbox_dir / candidate
    resolved = candidate.resolve()
    if cfg.outbox_dir.resolve() not in resolved.parents and resolved.parent != cfg.outbox_dir.resolve():
        raise SocialError("That draft is outside the outbox.")
    if not resolved.is_file():
        raise SocialError("No draft named %r in %s" % (name, cfg.outbox_dir))
    return resolved


def list_drafts(cfg: Config) -> List[dict]:
    cfg.ensure_dirs()
    drafts = []
    for path in sorted(cfg.outbox_dir.glob("*.md"), reverse=True):
        try:
            meta, body, _tags = parse_frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        drafts.append(
            {
                "name": path.name,
                "platform": meta.get("platform", "?"),
                "status": meta.get("status", "draft"),
                "topic": (meta.get("title") or "").replace(" post: ", ": ", 1),
                "created": meta.get("created", ""),
                "chars": len(body.strip()),
                "body": body.strip(),
            }
        )
    return drafts


def set_status(cfg: Config, name: str, status: str) -> Path:
    path = _path_for(cfg, name)
    text = path.read_text(encoding="utf-8")
    if re.search(r"^status:.*$", text, flags=re.MULTILINE):
        text = re.sub(r"^status:.*$", "status: " + status, text, count=1, flags=re.MULTILINE)
    else:
        text = text.replace("---\n", "---\nstatus: %s\n" % status, 1)
    path.write_text(text, encoding="utf-8")
    return path


def read_draft(cfg: Config, name: str) -> dict:
    path = _path_for(cfg, name)
    meta, body, _tags = parse_frontmatter(path.read_text(encoding="utf-8"))
    return {
        "name": path.name,
        "platform": meta.get("platform", "?"),
        "status": meta.get("status", "draft"),
        "title": meta.get("title", ""),
        "created": meta.get("created", ""),
        "body": body.strip(),
    }

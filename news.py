"""아침 뉴스 브리핑

feeds.toml의 언론사 RSS에서 최근 기사를 모으고, Claude가 같은 사건끼리 묶어
섹션별로 중요한 사건만 골라 요약한다. 결과는 HTML 한 장으로 띄운다.

사용법:
    .venv/bin/python news.py              # 브리핑 생성 → HTML 열기 + 알림
    .venv/bin/python news.py --dry-run    # Claude 없이 섹션별 기사 수만 출력
    .venv/bin/python news.py --out-dir docs --message-file message.json   # GitHub Actions에서 쓰는 방식
"""

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

import anthropic
import feedparser

ROOT = Path(__file__).resolve().parent
FEEDS_FILE = ROOT / "feeds.toml"
OUT_DIR = ROOT / "out"

MODEL = "claude-opus-5-5"
USER_AGENT = "Mozilla/5.0 (Macintosh) news-digest/1.0"
WINDOW_HOURS = 24       # 최근 이 시간 안의 기사만 본다
EXCERPT_CHARS = 160     # 기사가 많아서 본문은 짧게만 넘긴다
MAX_PER_SECTION = 300   # 섹션 하나에서 Claude에게 넘기는 최대 기사 수 (최신순)

SYSTEM_PROMPT = """너는 아침 뉴스 브리핑 편집자야.
한 섹션의 최근 24시간 기사 목록(id, 언론사, 제목, 본문 앞부분)을 받는다.

할 일:
1. 같은 사건을 다룬 기사끼리 묶는다. 언론사가 달라도 같은 사건이면 하나의 이야기다.
2. 오늘 알아야 할 중요도 순으로 이야기를 최대 {pick}개 고른다.
   중요도는 사회적 파급력, 여러 언론사가 다뤘는지, 새로운 사실인지로 판단한다.
   연예 가십, 사건사고 단신, 광고성 기사, 부고, 날씨 단신은 고르지 않는다.
3. 이야기마다:
   - headline: 사건을 중립적으로 요약한 제목, 30자 안쪽. 언론사 제목을 그대로 베끼지 않는다.
   - summary: 사실 위주 한국어 문장 2~3개. 신문 기사체로 "~했다", "~이다"로 끝낸다. 기사에 없는 내용은 추측하지 않는다.
   - why: 왜 중요한지 한 문장.
   - article_ids: 이 이야기에 속한 기사 id 전부.
   논조가 엇갈리는 사안이면 한쪽 입장만 쓰지 말고, 입장이 갈린다는 사실을 summary에 담는다."""

EXCLUDE_PROMPT = """

아래 사건은 이미 1면에 실렸으니 고르지 않는다. 같은 사건의 후속 보도도 마찬가지다.
{headlines}"""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "stories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "headline": {"type": "string"},
                    "summary": {"type": "array", "items": {"type": "string"}},
                    "why": {"type": "string"},
                    "article_ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["headline", "summary", "why", "article_ids"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["stories"],
    "additionalProperties": False,
}

# API 키가 있으면 API로, 없으면 Claude 구독으로 로그인된 claude CLI로 호출한다.
USE_API = bool(os.environ.get("ANTHROPIC_API_KEY"))
CLAUDE_CLI = os.environ.get("CLAUDE_CLI") or shutil.which("claude")


@dataclass
class Article:
    outlet: str
    title: str
    link: str
    published: datetime | None
    excerpt: str


@dataclass
class Story:
    headline: str
    summary: list[str]
    why: str
    articles: list[Article]


@dataclass
class Section:
    name: str
    pick: int
    feeds: list[dict]
    front: bool = False
    focus: str = ""
    articles: list[Article] | None = None
    stories: list[Story] | None = None


def load_sections() -> list[Section]:
    config = tomllib.loads(FEEDS_FILE.read_text(encoding="utf-8"))
    sections = [Section(s["name"], s["pick"], s["feeds"], s.get("front", False), s.get("focus", "")) for s in config.get("sections", [])]
    for k in config.get("keywords", []):
        url = f"https://news.google.com/rss/search?q={quote(k['query'])}+when:1d&hl=ko&gl=KR&ceid=KR:ko"
        sections.append(Section(f"키워드 · {k['query']}", k["pick"], [{"name": "Google 뉴스", "url": url}]))
    return sections


# ── 수집 ──────────────────────────────────────────────

def http_get(url: str, timeout: int = 15) -> bytes:
    with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=timeout) as res:
        return res.read()


def strip_html(s: str) -> str:
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def entry_time(e) -> datetime | None:
    t = e.get("published_parsed") or e.get("updated_parsed")
    return datetime(*t[:6], tzinfo=timezone.utc) if t else None


def fetch_feed(feed: dict) -> list[Article]:
    parsed = feedparser.parse(http_get(feed["url"]))
    if parsed.bozo and not parsed.entries:
        raise ValueError(str(parsed.bozo_exception))
    articles = []
    for e in parsed.entries:
        if not e.get("link"):
            continue
        title = strip_html(e.get("title", ""))
        outlet = feed["name"]
        # Google 뉴스는 여러 언론사 기사를 모아 주므로 실제 언론사 이름을 따로 꺼낸다.
        if src := e.get("source", {}).get("title"):
            outlet = src
            title = title.removesuffix(f" - {src}")
        excerpt = strip_html(e.get("summary", ""))
        if excerpt.startswith(title):  # Google 뉴스 요약은 제목 반복이라 버린다
            excerpt = ""
        articles.append(Article(outlet, title, e.link, entry_time(e), excerpt[:EXCERPT_CHARS]))
    return articles


def safe(fn, *args):
    try:
        return fn(*args)
    except Exception as e:
        return e


def collect(sections: list[Section]) -> list[str]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)
    jobs = [(s, f) for s in sections for f in s.feeds]
    failed = []
    for s in sections:
        s.articles = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for (section, feed), result in zip(jobs, pool.map(lambda j: safe(fetch_feed, j[1]), jobs)):
            if isinstance(result, Exception):
                failed.append(f"{feed['name']} ({type(result).__name__})")
                continue
            section.articles += [a for a in result if a.published is None or a.published >= cutoff]

    for s in sections:
        # 같은 링크나 같은 제목(여러 피드에 중복 게재)은 하나만 남긴다.
        unique: dict[str, Article] = {}
        for a in s.articles:
            unique.setdefault(a.link, a)
        by_title: dict[str, Article] = {}
        for a in unique.values():
            by_title.setdefault(re.sub(r"\W", "", a.title), a)
        s.articles = sorted(by_title.values(), key=lambda a: a.published or cutoff, reverse=True)[:MAX_PER_SECTION]
    return failed


# ── 편집 ──────────────────────────────────────────────

def build_prompt(section: Section) -> str:
    lines = [f"[{i}] ({a.outlet}) {a.title}" + (f" — {a.excerpt}" if a.excerpt else "") for i, a in enumerate(section.articles)]
    return f"섹션: {section.name}\n기사 {len(lines)}건\n\n" + "\n".join(lines)


def call_claude(system: str, prompt: str) -> dict:
    return call_api(system, prompt) if USE_API else call_cli(system, prompt)


def call_cli(system: str, prompt: str) -> dict:
    """Claude 구독 계정으로 로그인된 Claude Code CLI를 헤드리스로 호출한다."""
    proc = subprocess.run(
        [
            CLAUDE_CLI, "-p",
            "--model", MODEL,
            "--effort", "low",
            "--system-prompt", system,
            "--json-schema", json.dumps(OUTPUT_SCHEMA),
            "--output-format", "json",
            # 도구 없이 텍스트만 다루고, 대화 기록은 남기지 않는다.
            "--tools", "",
            "--no-session-persistence",
        ],
        input=prompt, cwd=ROOT, capture_output=True, text=True, timeout=600,
    )
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise CliError(proc.stderr.strip() or proc.stdout.strip() or f"종료 코드 {proc.returncode}")
    if out.get("is_error") or "structured_output" not in out:
        raise CliError(str(out.get("result") or out.get("subtype")))
    return out["structured_output"]


class CliError(Exception):
    pass


def call_api(system: str, prompt: str) -> dict:
    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=system,
        output_config={
            "effort": "low",
            "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
        },
        # 안전 분류기가 요청을 거절하면 서버가 알아서 다른 모델로 재시도한다.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": prompt}],
    )
    if response.stop_reason == "refusal":
        return {"stories": []}
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


def edit_section(section: Section, exclude: list[str]) -> list[Story]:
    if not section.articles:
        return []
    system = SYSTEM_PROMPT.format(pick=section.pick)
    if section.focus:
        system += f"\n\n이 섹션의 편집 방향: {section.focus}"
    if exclude:
        system += EXCLUDE_PROMPT.format(headlines="\n".join(f"- {h}" for h in exclude))
    result = call_claude(system, build_prompt(section))
    stories = []
    for s in result["stories"][: section.pick]:
        articles = [section.articles[i] for i in dict.fromkeys(s["article_ids"]) if 0 <= i < len(section.articles)]
        if articles:
            stories.append(Story(s["headline"], s["summary"], s["why"], articles))
    return stories


# ── 출력 ──────────────────────────────────────────────

def esc(s: str) -> str:
    return html.escape(s, quote=True)


def render(sections: list[Section], failed: list[str], now: datetime) -> str:
    weekday = "월화수목금토일"[now.weekday()]
    total = sum(len(s.articles) for s in sections)

    def story_html(n: int, st: Story) -> str:
        summary = "".join(f"<li>{esc(x)}</li>" for x in st.summary)
        # 언론사마다 대표 기사 하나씩만 링크로 보여준다.
        per_outlet: dict[str, Article] = {}
        for a in st.articles:
            per_outlet.setdefault(a.outlet, a)
        links = "".join(
            f'<a class="outlet" href="{esc(a.link)}" target="_blank" rel="noopener" title="{esc(a.title)}">{esc(a.outlet)}</a>'
            for a in per_outlet.values()
        )
        return f"""
      <article class="story">
        <div class="num">{n}</div>
        <div class="body">
          <h3>{esc(st.headline)}</h3>
          <ul>{summary}</ul>
          <p class="why">{esc(st.why)}</p>
          <div class="outlets"><span class="count">기사 {len(st.articles)}건</span>{links}</div>
        </div>
      </article>"""

    def section_html(s: Section) -> str:
        stories = "".join(story_html(i, st) for i, st in enumerate(s.stories, 1)) or '<p class="empty">고를 만한 소식이 없어요.</p>'
        return f"""
    <section>
      <h2>{esc(s.name)} <small>{len(s.articles)}건 중 {len(s.stories)}개</small></h2>
      {stories}
    </section>"""

    nav = "".join(f'<a href="#s{i}">{esc(s.name)}</a>' for i, s in enumerate(sections))
    body = "".join(section_html(s).replace("<section>", f'<section id="s{i}">', 1) for i, s in enumerate(sections))
    failed_html = f'<p class="failed">가져오지 못한 피드: {esc(", ".join(failed))}</p>' if failed else ""

    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>아침 뉴스 브리핑</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Serif+KR:wght@700;900&family=IBM+Plex+Sans+KR:wght@400;500;600&display=swap">
<style>
  :root {{
    --bg: #f4f5f7; --surface: #ffffff; --ink: #14171c; --muted: #5f6672; --line: #dfe2e7;
    --accent: #1f5fbf; --accent-soft: #e6eefb; --chip: #eef0f3;
    --serif: "Noto Serif KR", "AppleMyungjo", serif;
    --sans: "IBM Plex Sans KR", "Apple SD Gothic Neo", sans-serif;
    color-scheme: light;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #101216; --surface: #181b21; --ink: #e9ecf1; --muted: #9aa2ae; --line: #2a2f37;
      --accent: #7aa7f0; --accent-soft: #1c2940; --chip: #232830;
      color-scheme: dark;
    }}
  }}
  * {{ box-sizing: border-box; }}
  html {{ scroll-behavior: smooth; }}
  body {{ margin: 0; background: var(--bg); color: var(--ink); font: 16px/1.65 var(--sans); padding: 0 16px 64px; }}
  main {{ max-width: 760px; margin: 0 auto; }}

  header {{ padding: 40px 0 16px; }}
  .date {{ font-size: 13px; color: var(--muted); letter-spacing: .06em; }}
  h1 {{ font: 900 clamp(32px, 7vw, 46px)/1.15 var(--serif); margin: 6px 0 4px; letter-spacing: -.02em; }}
  .sub {{ color: var(--muted); font-size: 14px; margin: 0; }}
  nav {{ position: sticky; top: 0; z-index: 1; display: flex; gap: 8px; overflow-x: auto; padding: 12px 0; background: var(--bg); border-bottom: 1px solid var(--line); }}
  nav a {{ flex: none; text-decoration: none; color: var(--ink); background: var(--chip); border-radius: 99px; padding: 4px 14px; font-size: 14px; }}
  nav a:hover {{ background: var(--accent-soft); color: var(--accent); }}

  section {{ scroll-margin-top: 60px; }}
  h2 {{ font: 700 22px var(--serif); margin: 36px 0 12px; display: flex; align-items: baseline; gap: 10px; }}
  h2 small {{ font: 400 13px var(--sans); color: var(--muted); }}

  .story {{ display: grid; grid-template-columns: 34px 1fr; gap: 12px; background: var(--surface); border: 1px solid var(--line); border-radius: 8px; padding: 18px 20px 16px 14px; margin-bottom: 12px; }}
  .num {{ font: 900 22px/1.3 var(--serif); color: var(--accent); text-align: center; }}
  .story h3 {{ font: 700 19px/1.4 var(--serif); margin: 0 0 8px; }}
  .story ul {{ margin: 0; padding-left: 1.1em; }}
  .story li {{ margin: 3px 0; }}
  .why {{ margin: 10px 0 0; font-size: 14px; color: var(--muted); }}
  .why::before {{ content: "왜 중요? "; font-weight: 600; color: var(--accent); }}
  .outlets {{ display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin-top: 12px; }}
  .count {{ font-size: 12px; color: var(--muted); margin-right: 4px; }}
  .outlet {{ font-size: 12px; text-decoration: none; color: var(--ink); background: var(--chip); border-radius: 4px; padding: 2px 8px; }}
  .outlet:hover {{ background: var(--accent-soft); color: var(--accent); }}
  @media (max-width: 480px) {{
    .story {{ grid-template-columns: 1fr; padding-left: 18px; }}
    .num {{ text-align: left; }}
  }}

  .empty {{ color: var(--muted); }}
  footer {{ margin-top: 40px; font-size: 13px; color: var(--muted); text-align: center; }}
  .failed {{ color: var(--accent); }}
</style>
</head>
<body>
<main>
  <header>
    <div class="date">{now:%Y년 %-m월 %-d일} {weekday}요일 · {now:%H:%M}</div>
    <h1>아침 뉴스 브리핑</h1>
    <p class="sub">최근 {WINDOW_HOURS}시간 기사 {total}건을 사건별로 묶어 골랐어요.</p>
  </header>
  <nav>{nav}</nav>
  {body}
  <footer>
    <p>요약은 기사 제목과 앞부분만 보고 만든 것이라 원문과 다를 수 있어요.</p>
    {failed_html}
  </footer>
</main>
</body>
</html>
"""


# ── 실행 ──────────────────────────────────────────────

def applescript_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(message: str) -> None:
    if sys.platform == "darwin":
        subprocess.run(["osascript", "-e", f"display notification {applescript_str(message)} with title \"아침 뉴스 브리핑\""])


def kakao_message(sections: list[Section], now: datetime) -> str:
    """섹션마다 첫 번째 사건 제목 한 줄씩. 카톡 텍스트 메시지는 200자 제한이 있다."""
    weekday = "월화수목금토일"[now.weekday()]
    lines = [f"📰 {now:%-m/%-d}({weekday}) 아침 브리핑", ""]
    for s in sections:
        if s.stories:
            h = s.stories[0].headline
            lines.append(f"[{s.name}] {h if len(h) <= 26 else h[:25] + '…'}")
    return "\n".join(lines)


def run(args) -> None:
    sections = load_sections()
    started = time.monotonic()
    failed = collect(sections)
    print(f"수집 완료 ({time.monotonic() - started:.1f}초)" + (f", 실패: {', '.join(failed)}" if failed else ""))
    for s in sections:
        print(f"  {s.name}: {len(s.articles)}건")
    if args.dry_run:
        return

    started = time.monotonic()
    # 1면을 먼저 편집하고, 나머지 섹션은 1면 사건을 빼고 동시에 편집한다.
    front = [s for s in sections if s.front]
    rest = [s for s in sections if not s.front]
    with ThreadPoolExecutor(max_workers=len(sections)) as pool:
        for s, stories in zip(front, pool.map(lambda s: edit_section(s, []), front)):
            s.stories = stories
        exclude = [st.headline for s in front for st in s.stories]
        for s, stories in zip(rest, pool.map(lambda s: edit_section(s, exclude), rest)):
            s.stories = stories
    print(f"편집 완료 ({time.monotonic() - started:.1f}초)")

    now = datetime.now().astimezone()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    page = render(sections, failed, now)
    out = out_dir / f"{now:%Y-%m-%d}.html"
    out.write_text(page, encoding="utf-8")
    (out_dir / "index.html").write_text(page, encoding="utf-8")  # 항상 최신 브리핑
    print(f"저장: {out}")

    if args.message_file:
        # 카톡 전송은 페이지를 배포한 뒤 kakao.py가 따로 한다.
        Path(args.message_file).write_text(json.dumps(
            {"text": kakao_message(sections, now), "page": out.name}, ensure_ascii=False), encoding="utf-8")
    else:
        top = next((s.stories[0].headline for s in sections if s.stories), "")
        notify(f"오늘의 첫 소식: {top}" if top else "브리핑을 만들었어요.")
        if not args.no_open:
            subprocess.run(["open", str(out)])


def main() -> None:
    parser = argparse.ArgumentParser(description="아침 뉴스 브리핑")
    parser.add_argument("--dry-run", action="store_true", help="Claude 없이 섹션별 기사 수만 출력")
    parser.add_argument("--no-open", action="store_true", help="HTML을 브라우저로 열지 않음")
    parser.add_argument("--out-dir", default=str(OUT_DIR), help="HTML 저장 위치 (기본: out/)")
    parser.add_argument("--message-file", help="알림 대신 카톡 메시지 내용을 이 파일(JSON)에 저장")
    args = parser.parse_args()

    if not args.dry_run and not USE_API and not CLAUDE_CLI:
        sys.exit("ANTHROPIC_API_KEY도 없고 claude CLI도 찾지 못했어요. 둘 중 하나가 필요해요.")
    run(args)


if __name__ == "__main__":
    main()

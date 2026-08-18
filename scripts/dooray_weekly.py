#!/usr/bin/env python3
"""Dooray 주간보고 조회 CLI.

환경변수:
  DOORAY_API_KEY   (필수) Dooray 개인 API 토큰
  DOORAY_API_HOST  (선택) 기본 api.gov-dooray.com
  DOORAY_WEEKLY_PROJECT (선택) 기본 주간보고

사용법:
  dooray_weekly.py projects [--query 주간보고]
  dooray_weekly.py list [--project 주간보고] [--limit 20]
  dooray_weekly.py show [<post-id | URL | latest | #번호>] [--project 주간보고]
                        [--format md|raw|json]
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

DEFAULT_HOST = os.environ.get("DOORAY_API_HOST", "api.gov-dooray.com")
FILE_HOST = os.environ.get("DOORAY_FILE_HOST", DEFAULT_HOST.replace("api.", "file-api.", 1))
DEFAULT_PROJECT = os.environ.get("DOORAY_WEEKLY_PROJECT", "주간보고")


# --------------------------------------------------------------------------- api


class DoorayError(RuntimeError):
    pass


def _auth() -> str:
    key = os.environ.get("DOORAY_API_KEY")
    if not key:
        raise DoorayError("DOORAY_API_KEY 환경변수가 없다.")
    return f"dooray-api {key}"


def _send(req: urllib.request.Request) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise DoorayError(f"HTTP {exc.code} {req.full_url}\n{detail}") from exc
    if not payload.get("header", {}).get("isSuccessful", False):
        raise DoorayError(f"{req.full_url}\n{payload.get('header')}")
    return payload


def api(path: str, *, method: str = "GET", body: dict | None = None, **params) -> dict:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"https://{DEFAULT_HOST}{path}" + (f"?{query}" if query else "")
    headers = {"Authorization": _auth()}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    return _send(urllib.request.Request(url, data=data, headers=headers, method=method))


def upload_file(project_id: str, post_id: str, path: str | os.PathLike) -> str:
    """업무에 파일을 업로드하고 fileId를 돌려준다. 업로드는 file-api 호스트를 쓴다."""
    path = os.fspath(path)
    name = os.path.basename(path)
    with open(path, "rb") as fh:
        payload = fh.read()
    boundary = "----dooray" + os.urandom(12).hex()
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8")
    data = head + payload + f"\r\n--{boundary}--\r\n".encode("utf-8")
    url = f"https://{FILE_HOST}/uploads/project/v1/projects/{project_id}/posts/{post_id}/files"
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": _auth(),
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    return _send(req)["result"]["id"]


def create_comment(project_id: str, post_id: str, content: str, file_ids: list[str] | None = None) -> str:
    body = {"body": {"mimeType": "text/x-markdown", "content": content}}
    if file_ids:
        body["fileIdList"] = list(file_ids)
    result = api(
        f"/project/v1/projects/{project_id}/posts/{post_id}/logs", method="POST", body=body
    )
    return result["result"]["id"]


def list_projects(query: str | None = None) -> list[dict]:
    projects = api("/project/v1/projects", member="me", state="active", size=200)["result"]
    if query:
        projects = [p for p in projects if query in p["code"]]
    return projects


def resolve_project_id(name_or_id: str) -> tuple[str, str]:
    if name_or_id.isdigit():
        return name_or_id, name_or_id
    matches = list_projects(name_or_id)
    if not matches:
        raise DoorayError(f"'{name_or_id}' 프로젝트를 찾을 수 없다.")
    exact = [p for p in matches if p["code"] == name_or_id]
    picked = (exact or matches)[0]
    return picked["id"], picked["code"]


def list_posts(project_id: str, limit: int = 20) -> list[dict]:
    return api(
        f"/project/v1/projects/{project_id}/posts",
        size=limit,
        order="-postUpdatedAt",
        postWorkflowClasses="registered,working,closed",
    )["result"]


def get_post(project_id: str, post_id: str) -> dict:
    return api(f"/project/v1/projects/{project_id}/posts/{post_id}")["result"]


def get_comments(project_id: str, post_id: str) -> list[dict]:
    return api(f"/project/v1/projects/{project_id}/posts/{post_id}/logs", size=100)["result"]


POST_URL_RE = re.compile(r"/(?:project/)?tasks/(\d+)")


def resolve_post_id(project_id: str, target: str) -> str:
    target = (target or "latest").strip()
    if match := POST_URL_RE.search(target):
        return match.group(1)
    if target.isdigit() and len(target) > 6:
        return target
    posts = list_posts(project_id, limit=100)
    if not posts:
        raise DoorayError("게시글이 없다.")
    if target in ("latest", "last", "최신"):
        return posts[0]["id"]
    wanted = target.lstrip("#")
    for post in posts:
        if str(post.get("number")) == wanted or post.get("subject", "").strip() == target:
            return post["id"]
    raise DoorayError(f"'{target}'에 해당하는 게시글이 없다.")


# ----------------------------------------------------------------- body rendering


class _TableParser(HTMLParser):
    """Dooray 본문의 <table> 블록을 행/셀 텍스트로 분해한다."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[object] = []  # str(텍스트) | list[list[str]](테이블)
        self._text: list[str] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def _flush_text(self) -> None:
        text = "".join(self._text).strip("\n")
        if text.strip():
            self.blocks.append(text)
        self._text = []

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._flush_text()
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag == "br":
            (self._cell if self._cell is not None else self._text).append("\n")
        elif self._cell is not None:
            self._cell.append(self.get_starttag_text() or "")

    def handle_endtag(self, tag):
        if tag == "table" and self._table is not None:
            self.blocks.append(self._table)
            self._table = None
        elif tag == "tr" and self._row is not None:
            if self._table is not None:
                self._table.append(self._row)
            self._row = None
        elif tag in ("td", "th") and self._cell is not None:
            if self._row is not None:
                self._row.append("".join(self._cell).strip())
            self._cell = None
        elif self._cell is not None and tag not in ("colgroup", "col", "br"):
            self._cell.append(f"</{tag}>")

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)
        elif self._table is None:
            self._text.append(data)

    def close(self):
        super().close()
        self._flush_text()


DOORAY_LINK_RE = re.compile(r"\[([^\]]+)\]\(dooray://[^)]*/tasks/(\d+)[^)]*\)")


def clean_cell(text: str) -> str:
    text = html.unescape(text)
    text = DOORAY_LINK_RE.sub(r"\1 (task \2)", text)
    text = re.sub(r"\\([\\`*_{}\[\]()#+\-.!~])", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[ \t]*\u00a0[ \t]*", " ", text)
    lines = [line.rstrip() for line in text.split("\n")]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def normalize_rows(rows: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    """셀을 정리하고 `..` 연속행의 연번/업무를 직전 값으로 승계한다."""
    rows = [[clean_cell(c) for c in row] for row in rows]
    rows = [row for row in rows if any(c for c in row)]
    if not rows:
        return [], []
    headers, body = rows[0], rows[1:]
    carried: list[list[str]] = []
    last: dict[int, str] = {}
    for row in body:
        row = list(row)
        for col in (0, 1):
            if col >= len(row):
                continue
            value = row[col]
            if value and set(value) <= {".", " "}:
                row[col] = last.get(col, "")
            elif value:
                last[col] = value
        carried.append(row)
    return headers, carried


def parse_document(content: str) -> list[dict]:
    """본문을 `[{title, tables: [{headers, rows}]}]` 구조로 분해한다."""
    parser = _TableParser()
    parser.feed(content)
    parser.close()
    sections: list[dict] = []
    current = {"title": "", "tables": []}
    for block in parser.blocks:
        if isinstance(block, str):
            for line in clean_cell(block).split("\n"):
                heading = re.match(r"^#{1,3}\s*(.+?)\s*$", line)
                if not heading:
                    continue
                if current["title"] or current["tables"]:
                    sections.append(current)
                title = re.sub(r"^\d+\.\s*", "", heading.group(1)).strip()
                current = {"title": title, "tables": []}
        else:
            headers, rows = normalize_rows(block)
            if rows:
                current["tables"].append({"headers": headers, "rows": rows})
    if current["title"] or current["tables"]:
        sections.append(current)
    return sections


def render_table(rows: list[list[str]]) -> str:
    headers, body = normalize_rows(rows)
    if not body:
        return ""
    out: list[str] = []
    for row in body:
        index = row[0] if row else ""
        label = row[1] if len(row) > 1 else ""
        title = ". ".join(part for part in (index, label) if part)
        out.append(f"### {title or '(무제)'}")
        for header, cell in zip(headers[2:], row[2:]):
            if not cell:
                continue
            head = header or "-"
            if "\n" in cell:
                body_text = "\n".join(("  " + line) if line else "" for line in cell.split("\n"))
                out.append(f"- **{head}**\n{body_text}")
            else:
                out.append(f"- **{head}**: {cell}")
        out.append("")
    return "\n".join(out)


def render_body(content: str) -> str:
    parser = _TableParser()
    parser.feed(content)
    parser.close()
    chunks: list[str] = []
    for block in parser.blocks:
        if isinstance(block, str):
            text = clean_cell(block)
            if text:
                chunks.append(text)
        else:
            table = render_table(block)
            if table:
                chunks.append(table)
    return re.sub(r"\n{3,}", "\n\n", "\n\n".join(chunks)).strip()


def format_post(post: dict, comments: list[dict], fmt: str) -> str:
    if fmt == "json":
        return json.dumps({"post": post, "comments": comments}, ensure_ascii=False, indent=2)

    content = post.get("body", {}).get("content", "")
    author = post.get("users", {}).get("from", {}).get("member", {}).get("name", "?")
    header = (
        f"# {post.get('subject', '(제목 없음)')}\n"
        f"- 업무번호: {post.get('taskNumber')} (id {post.get('id')})\n"
        f"- 작성자: {author}\n"
        f"- 등록: {post.get('createdAt')} / 수정: {post.get('updatedAt')}"
    )
    body = content if fmt == "raw" else render_body(content)
    out = f"{header}\n\n{body}"
    if comments:
        out += "\n\n## 댓글\n"
        for c in comments:
            who = (c.get("createdAt") or "")
            text = (c.get("body") or {}).get("content", "")
            out += f"\n- [{who}] {clean_cell(text)}"
    return out


# --------------------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dooray 주간보고 조회")
    sub = parser.add_subparsers(dest="cmd")

    p_proj = sub.add_parser("projects", help="접근 가능한 프로젝트 목록")
    p_proj.add_argument("--query", default=None)

    p_list = sub.add_parser("list", help="주간보고 게시글 목록")
    p_list.add_argument("--project", default=DEFAULT_PROJECT)
    p_list.add_argument("--limit", type=int, default=20)

    p_show = sub.add_parser("show", help="주간보고 본문 조회")
    p_show.add_argument("target", nargs="?", default="latest", help="post id, URL, latest, #번호")
    p_show.add_argument("--project", default=DEFAULT_PROJECT)
    p_show.add_argument("--format", choices=("md", "raw", "json"), default="md")
    p_show.add_argument("--no-comments", action="store_true")

    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 1

    try:
        if args.cmd == "projects":
            for p in list_projects(args.query):
                print(f"{p['id']}\t{p['code']}")
            return 0

        project_id, code = resolve_project_id(args.project)

        if args.cmd == "list":
            for post in list_posts(project_id, args.limit):
                print(f"{post['id']}\t{post['taskNumber']}\t{post['subject']}\t{post['createdAt']}")
            return 0

        post_id = resolve_post_id(project_id, args.target)
        post = get_post(project_id, post_id)
        comments = [] if args.no_comments else get_comments(project_id, post_id)
        if args.format == "md":
            print(f"<!-- project: {code} ({project_id}) -->")
        print(format_post(post, comments, args.format))
        return 0
    except DoorayError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

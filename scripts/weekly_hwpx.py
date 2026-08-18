#!/usr/bin/env python3
"""Dooray 주간보고 → AIX전략실 주요 업무 보고 HWPX 생성기.

템플릿 hwpx의 문단/표 노드를 그대로 복제해 텍스트만 바꾸므로 서식(글꼴, 표
테두리, 배너)이 100% 보존된다. 구조는 다음과 같다.

    [제목 배너 표]  AIX전략실 주요 업무 보고
    (`26.8.18.(화), AIX전략실)
    [섹션 바 표] 1 | AI 연구개발
      1. {업무}
      □ {세부업무} (*담당자)
      [2열 표] 주요 경과 | 향후 계획

사용법:
  weekly_hwpx.py [--template <template.hwpx>] [--output out.hwpx]
                 [--post latest|<id>|<URL>] [--project 주간보고]
                 [--from-json parsed.json] [--dump-json parsed.json]
                 [--title ...] [--org AIX전략실] [--date 2026-08-18]
                 [--exclude-section 기타] [--keep-empty] [--no-attach]
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import struct
import sys
import tempfile
import zipfile
import zlib
from pathlib import Path

from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dooray_weekly as dw  # noqa: E402

HP = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"
WEEKDAYS = "월화수목금토일"
DEFAULT_TITLE = "AIX전략실 주요 업무 보고"
DEFAULT_ORG = "AIX전략실"
HWPX_SKILL = Path(os.environ.get("HWPX_SKILL_DIR", Path.home() / ".gjc/agent/skills/hwpx"))

# 표 셀 레이아웃 상수 (템플릿 실측값)
LINE_PITCH = 1600
CELL_PAD = 248
MIN_ROW_HEIGHT = 1665
CJK_WIDTH = 610
ASCII_WIDTH = 320


# ------------------------------------------------------------------ 본문 → 데이터


def strip_md(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    return text.strip()


def cell_lines(text: str) -> list[str]:
    """마크다운 셀을 HWPX 문단 줄 목록으로 변환한다."""
    out: list[str] = []
    for raw in text.split("\n"):
        if not raw.strip():
            continue
        indent = len(raw) - len(raw.lstrip(" \t"))
        body = raw.strip()
        bullet = re.match(r"^[*\-+]\s+(.*)$", body)
        if bullet:
            body = bullet.group(1)
            checkbox = re.match(r"^\[([ xX])\]\s*(.*)$", body)
            done = False
            if checkbox:
                done = checkbox.group(1).lower() == "x"
                body = checkbox.group(2)
            body = strip_md(body)
            if not body:
                continue
            if done:
                body += " (완료)"
            out.append(("ㅇ " if indent < 4 else " - ") + body)
        else:
            body = strip_md(body)
            if not body:
                continue
            out.append(f" {body}" if body[0] in "※1234567890" else body)
    if len(out) == 1:
        out[0] = re.sub(r"^(ㅇ |\s*- )", "", out[0])
    return out


def pick(headers: list[str], *keywords: str) -> int | None:
    for idx, header in enumerate(headers):
        if any(k in header for k in keywords):
            return idx
    return None


def build_document(sections: list[dict], *, keep_empty: bool, exclude: list[str]) -> list[dict]:
    """Dooray 표 구조를 보고서 섹션/업무/항목 트리로 변환한다."""
    result: list[dict] = []
    for section in sections:
        title = section["title"]
        if any(token and token in title for token in exclude):
            continue
        tasks: list[dict] = []
        for table in section["tables"]:
            headers = table["headers"]
            col_task = pick(headers, "업무") if pick(headers, "세부") is None else 1
            col_task = 1 if col_task is None else col_task
            col_sub = pick(headers, "세부")
            col_owner = pick(headers, "담당")
            col_progress = pick(headers, "경과")
            if col_progress is None:
                col_progress = pick(headers, "진행")
            col_plan = pick(headers, "향후", "계획")
            if col_plan is None:
                col_plan = pick(headers, "비고")

            def cell(row: list[str], idx: int | None) -> str:
                return row[idx] if idx is not None and idx < len(row) else ""

            for row in table["rows"]:
                name = cell(row, col_task).strip()
                subject = strip_md(cell(row, col_sub)) if col_sub is not None else ""
                owners = cell(row, col_owner).replace("\n", " ").strip()
                progress = cell_lines(cell(row, col_progress))
                plan = cell_lines(cell(row, col_plan))
                if not keep_empty and not progress and not plan:
                    continue
                if not name and not subject:
                    continue
                if tasks and tasks[-1]["name"] == name:
                    tasks[-1]["items"].append(
                        {"subject": subject, "owners": owners, "progress": progress, "plan": plan}
                    )
                else:
                    tasks.append(
                        {
                            "name": name,
                            "items": [
                                {
                                    "subject": subject,
                                    "owners": owners,
                                    "progress": progress,
                                    "plan": plan,
                                }
                            ],
                        }
                    )
        if tasks:
            result.append({"title": title, "tasks": tasks})
    return result


# ------------------------------------------------------------------ HWPX 조립


def blank_png(width: int = 744, height: int = 1052) -> bytes:
    """A4 비율의 흰 PNG. 생성물이 서식 원본 썸네일을 물고 가지 않게 한다."""
    raw = b"".join(b"\x00" + b"\xff" * (width * 3) for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        payload = tag + data
        return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def text_width(line: str) -> int:
    return sum(CJK_WIDTH if ord(ch) > 0x2000 else ASCII_WIDTH for ch in line)


def wrapped_lines(lines: list[str], cell_width: int) -> int:
    usable = max(cell_width - 1020, 1000)
    total = 0
    for line in lines or [""]:
        total += max(1, -(-text_width(line) // usable))
    return max(total, 1)


class TemplateBuilder:
    def __init__(self, template: Path) -> None:
        self.workdir = Path(tempfile.mkdtemp(prefix="weekly-hwpx-"))
        with zipfile.ZipFile(template) as zf:
            self.names = zf.namelist()
            zf.extractall(self.workdir)
        self.section_path = self.workdir / "Contents/section0.xml"
        self.tree = etree.parse(str(self.section_path))
        self.root = self.tree.getroot()
        self.paras = list(self.root)
        self._collect_prototypes()

    # -- prototype 탐색 -----------------------------------------------------

    @staticmethod
    def _tbl(p) -> etree._Element | None:
        return p.find(f".//{HP}tbl")

    @staticmethod
    def _cell_texts(tbl) -> list[str]:
        return ["".join(tc.itertext()).strip() for tc in tbl.iter(f"{HP}tc")]

    def _collect_prototypes(self) -> None:
        self.p_title = self.paras[0]
        self.p_date = self.paras[1]
        self.p_gap = next(
            p for p in self.paras[1:] if not "".join(p.itertext()).strip() and p.get("paraPrIDRef") != "0"
        )
        self.p_blank = next(
            p for p in self.paras if not "".join(p.itertext()).strip() and p.get("paraPrIDRef") == "0"
        )
        self.p_secbar = next(
            p for p in self.paras if (t := self._tbl(p)) is not None and t.get("colCnt") == "3"
        )
        self.p_table = next(
            p
            for p in self.paras
            if (t := self._tbl(p)) is not None
            and t.get("colCnt") == "2"
            and any("경과" in c for c in self._cell_texts(t))
        )
        self.p_task = next(
            p
            for p in self.paras
            if self._tbl(p) is None and re.match(r"^\d+\.", "".join(p.itertext()).strip())
        )
        self.p_sub = next(
            p
            for p in self.paras
            if self._tbl(p) is None
            and "".join(p.itertext()).strip().startswith("□")
            and any(r.get("charPrIDRef") for r in p.findall(f"{HP}run"))
            and len(p.findall(f"{HP}run")) >= 3
        )

    # -- 텍스트 주입 --------------------------------------------------------

    @staticmethod
    def _runs(p) -> list:
        return p.findall(f"{HP}run")

    @staticmethod
    def _set_run_text(run, text: str) -> None:
        for t in run.findall(f"{HP}t"):
            run.remove(t)
        el = etree.SubElement(run, f"{HP}t")
        el.text = text

    @classmethod
    def _set_para_text(cls, p, text: str) -> None:
        runs = cls._runs(p)
        keep = runs[0]
        for run in runs[1:]:
            p.remove(run)
        cls._set_run_text(keep, text)

    @classmethod
    def _set_cell(cls, tc, lines: list[str]) -> None:
        sub = tc.find(f"{HP}subList")
        proto = sub.find(f"{HP}p")
        template = copy.deepcopy(proto)
        for child in sub.findall(f"{HP}p"):
            sub.remove(child)
        for line in lines or [""]:
            new = copy.deepcopy(template)
            cls._set_para_text(new, line)
            sub.append(new)

    @staticmethod
    def _cells(tbl) -> list:
        return list(tbl.iter(f"{HP}tc"))

    # -- 블록 생성 ----------------------------------------------------------

    def make_secbar(self, number: int, title: str):
        p = copy.deepcopy(self.p_secbar)
        cells = self._cells(self._tbl(p))
        self._set_cell(cells[0], [str(number)])
        self._set_cell(cells[-1], [title])
        return p

    def make_task(self, number: int, name: str):
        p = copy.deepcopy(self.p_task)
        self._set_para_text(p, f"{number}. {name}")
        return p

    def make_sub(self, subject: str, owners: str):
        p = copy.deepcopy(self.p_sub)
        runs = self._runs(p)
        head, gap, tail = runs[0], runs[-2], runs[-1]
        for run in runs[1:-2]:
            p.remove(run)
        self._set_run_text(head, f"□ {subject}")
        if owners:
            self._set_run_text(gap, " ")
            self._set_run_text(tail, owners if owners.startswith("(") else f"({owners})")
        else:
            p.remove(gap)
            p.remove(tail)
        return p

    def make_table(self, progress: list[str], plan: list[str]):
        p = copy.deepcopy(self.p_table)
        tbl = self._tbl(p)
        rows = tbl.findall(f"{HP}tr")
        body_cells = rows[1].findall(f"{HP}tc")
        self._set_cell(body_cells[0], progress)
        self._set_cell(body_cells[1], plan)

        widths = [int(tc.find(f"{HP}cellSz").get("width")) for tc in body_cells]
        needed = max(
            wrapped_lines(progress, widths[0]),
            wrapped_lines(plan, widths[1]),
        )
        body_height = max(MIN_ROW_HEIGHT, needed * LINE_PITCH + CELL_PAD)
        for tc in body_cells:
            tc.find(f"{HP}cellSz").set("height", str(body_height))
        head_height = int(rows[0].findall(f"{HP}tc")[0].find(f"{HP}cellSz").get("height"))
        tbl.find(f"{HP}sz").set("height", str(head_height + body_height))
        return p

    # -- 문서 조립 ----------------------------------------------------------

    def build(self, doc: dict) -> None:
        title_cells = self._cells(self._tbl(self.p_title))
        for tc in title_cells:
            if "".join(tc.itertext()).strip():
                self._set_cell(tc, [doc["title"]])
        self._set_para_text(self.p_date, doc["date_line"])

        body = [self.p_title, self.p_date, copy.deepcopy(self.p_gap)]
        sections = doc["sections"]
        for s_idx, section in enumerate(sections, 1):
            body.append(self.make_secbar(s_idx, section["title"]))
            body.append(copy.deepcopy(self.p_blank))
            for t_idx, task in enumerate(section["tasks"], 1):
                body.append(self.make_task(t_idx, task["name"]))
                body.append(copy.deepcopy(self.p_blank))
                for item in task["items"]:
                    if item["subject"]:
                        body.append(self.make_sub(item["subject"], item["owners"]))
                    body.append(self.make_table(item["progress"], item["plan"]))
                    body.append(copy.deepcopy(self.p_blank))
            body.pop()
            if s_idx != len(sections):
                body.append(copy.deepcopy(self.p_gap))

        for child in list(self.root):
            self.root.remove(child)
        for para in body:
            self.root.append(para)

        for cache in self.root.iter(f"{HP}linesegarray"):
            cache.getparent().remove(cache)
        for idx, tbl in enumerate(self.root.iter(f"{HP}tbl")):
            tbl.set("id", str(1170793480 + idx))
            tbl.set("zOrder", str(idx))

    def _write_preview(self) -> None:
        """미리보기를 새 본문으로 갱신한다. 썸네일은 서식 원본 내용이 남지 않도록 비운다."""
        text_path = self.workdir / "Preview/PrvText.txt"
        if text_path.exists():
            lines: list[str] = []
            for para in self.root:
                text = "\n".join(
                    t.strip() for t in ("".join(para.itertext())).split("\n") if t.strip()
                )
                lines.append(text)
            text_path.write_text("\n".join(lines)[:2000], encoding="utf-8")
        image_path = self.workdir / "Preview/PrvImage.png"
        if image_path.exists():
            image_path.write_bytes(blank_png())

    def save(self, output: Path) -> None:
        self.tree.write(
            str(self.section_path), xml_declaration=True, encoding="UTF-8", standalone=True
        )
        self._write_preview()
        output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output, "w") as zf:
            zf.write(self.workdir / "mimetype", "mimetype", compress_type=zipfile.ZIP_STORED)
            for name in self.names:
                if name == "mimetype":
                    continue
                zf.write(self.workdir / name, name, compress_type=zipfile.ZIP_DEFLATED)

    def cleanup(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)


def postprocess(output: Path) -> None:
    """hwpx-skill의 네임스페이스 보정 · 레이아웃 검증을 순서대로 실행한다."""
    steps = [
        ["scripts/fix_namespaces.py", str(output)],
        ["scripts/finalize_hwpx.py", str(output), "--strip-linesegarray", "--layout"],
        ["scripts/validate.py", str(output)],
    ]
    for step in steps:
        script = HWPX_SKILL / step[0]
        if not script.exists():
            print(f"warn: {script} 없음 — 후처리 생략", file=sys.stderr)
            return
        proc = subprocess.run(
            [sys.executable, str(script), *step[1:]], capture_output=True, text=True
        )
        tag = Path(step[0]).stem
        if proc.returncode != 0:
            print(f"warn: {tag} rc={proc.returncode}\n{proc.stdout}{proc.stderr}", file=sys.stderr)
        else:
            print(f"ok: {tag}", file=sys.stderr)


# --------------------------------------------------------------------------- cli


def date_line(date: dt.date, org: str) -> str:
    return f"(`{date.year % 100}.{date.month}.{date.day}.({WEEKDAYS[date.weekday()]}), {org})"


def resolve_template(explicit: Path | None) -> Path:
    """--template → $WEEKLY_HWPX_TEMPLATE → ./template/*.hwpx → 스킬 assets 순으로 찾는다."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(explicit)
    if env := os.environ.get("WEEKLY_HWPX_TEMPLATE"):
        candidates.append(Path(env))
    candidates += sorted(Path("template").glob("*.hwpx"), reverse=True)
    candidates.append(Path(__file__).resolve().parent.parent / "assets/weekly-template.hwpx")
    for path in candidates:
        if path.is_file():
            return path
    raise SystemExit("error: 서식 hwpx를 찾을 수 없다. --template 으로 지정한다.")


def parse_date(text: str) -> dt.date:
    digits = re.findall(r"\d+", text)
    if len(digits) >= 3:
        year, month, day = (int(d) for d in digits[:3])
        if year < 100:
            year += 2000
        return dt.date(year, month, day)
    raise ValueError(f"날짜를 해석할 수 없다: {text!r}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Dooray 주간보고 → HWPX 보고서")
    ap.add_argument("--template", type=Path, default=None, help="서식 기준 hwpx")
    ap.add_argument("--output", type=Path, default=None)
    ap.add_argument("--post", default="latest", help="post id / URL / latest / #번호")
    ap.add_argument("--project", default=dw.DEFAULT_PROJECT)
    ap.add_argument("--from-json", type=Path, default=None, help="Dooray 호출 대신 JSON 입력")
    ap.add_argument("--dump-json", type=Path, default=None, help="변환된 구조를 JSON으로 저장")
    ap.add_argument("--title", default=DEFAULT_TITLE)
    ap.add_argument("--org", default=DEFAULT_ORG)
    ap.add_argument("--date", default=None, help="보고일 (기본: 게시글 제목의 날짜)")
    ap.add_argument("--exclude-section", action="append", default=[], help="제외할 섹션 키워드")
    ap.add_argument("--keep-empty", action="store_true", help="경과·계획이 빈 항목도 유지")
    ap.add_argument(
        "--attach",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="원본 업무에 hwpx를 첨부한 댓글 등록 (기본 동작, 끄려면 --no-attach)",
    )
    ap.add_argument("--comment", default=None, help="첨부 댓글 본문")
    ap.add_argument("--no-postprocess", action="store_true")
    args = ap.parse_args(argv)
    explicit_attach = "--attach" in (argv if argv is not None else sys.argv[1:])

    project_id = post_id = None
    if args.from_json:
        doc = json.loads(args.from_json.read_text(encoding="utf-8"))
        source = doc.get("source") or {}
        project_id, post_id = source.get("projectId"), source.get("id")
    else:
        project_id, _ = dw.resolve_project_id(args.project)
        post_id = dw.resolve_post_id(project_id, args.post)
        post = dw.get_post(project_id, post_id)
        raw = dw.parse_document(post["body"]["content"])
        report_date = parse_date(args.date or post["subject"])
        doc = {
            "title": args.title,
            "date_line": date_line(report_date, args.org),
            "date": report_date.isoformat(),
            "source": {
                "projectId": project_id,
                "id": post_id,
                "taskNumber": post["taskNumber"],
                "subject": post["subject"],
            },
            "sections": build_document(
                raw, keep_empty=args.keep_empty, exclude=args.exclude_section
            ),
        }

    if args.dump_json:
        args.dump_json.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

    if not doc["sections"]:
        print("error: 출력할 섹션이 없다.", file=sys.stderr)
        return 2

    report_date = parse_date(doc.get("date") or args.date or "")
    output = args.output or Path(f"{report_date:%y%m%d}_{doc['title']}.hwpx")

    builder = TemplateBuilder(resolve_template(args.template))
    try:
        builder.build(doc)
        builder.save(output)
    finally:
        builder.cleanup()

    if not args.no_postprocess:
        postprocess(output)
    print(output)

    if args.attach:
        if not (project_id and post_id):
            message = "source.projectId / source.id 가 없어 첨부를 건너뛴다."
            if explicit_attach:
                print(f"error: {message}", file=sys.stderr)
                return 2
            print(f"warn: {message}", file=sys.stderr)
            return 0
        file_id = dw.upload_file(project_id, post_id, output)
        text = args.comment or f"본문 기준으로 자동 생성한 「{doc['title']}」 초안을 첨부합니다."
        log_id = dw.create_comment(project_id, post_id, text, [file_id])
        print(f"attached: file={file_id} comment={log_id}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

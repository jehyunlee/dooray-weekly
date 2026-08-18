---
name: dooray-weekly
description: 두레이(Dooray) 주간보고를 읽어 정리하고, 주요 업무 보고 HWPX 문서를 작성해 원본 업무에 첨부 댓글로 등록한다. 주간보고 작성·정리·한글파일(hwpx) 생성·업로드 요청에 쓴다.
argument-hint: "[read|hwpx] [latest | <post-id> | <Dooray task URL>] [--attach]"
use_when:
  - 두레이 주간보고 작성
  - 두레이 주간보고 작성해줘
  - 주간보고 작성해줘
  - 주간보고 만들어줘
  - 주간보고 써줘
  - 주간보고 정리해줘
  - 주간보고 읽어줘
  - 주간보고 보고서 한글파일 hwpx 생성
  - 주간보고 두레이 댓글 첨부 업로드
  - dooray weekly report write generate hwpx attach
---

# Dooray 주간보고 읽기 · HWPX 보고서 생성

Dooray(정부용 테넌트 `*.gov-dooray.com` 포함)의 **주간보고** 프로젝트 게시글을 REST API로 가져와,
(1) 읽기 좋은 마크다운으로 정리하거나 (2) `AIX전략실 주요 업무 보고` 서식의 HWPX 문서로 만든다.

## 전제

- `DOORAY_API_KEY` 환경변수에 개인 API 토큰이 있어야 한다. (Dooray → 설정 → API)
- API 호스트는 `api.gov-dooray.com` (정부용 테넌트). 일반 테넌트는 `DOORAY_API_HOST=api.dooray.com`.
- 인증 헤더: `Authorization: dooray-api <KEY>`
- HWPX 생성에는 `lxml`이 필요하고, 후처리에 [hwpx-skill](https://github.com/jkf87/hwpx-skill)을 쓴다.
  기본 경로 `~/.gjc/agent/skills/hwpx` (변경: `HWPX_SKILL_DIR`).

## 프로젝트 식별

프로젝트 id는 하드코딩하지 않는다. `dooray_weekly.py projects --query 주간` 으로 찾거나,
`--project` 에 프로젝트 code(기본 `주간보고`)를 넘기면 스크립트가 알아서 id로 해석한다.

## 1. 읽기 — `scripts/dooray_weekly.py`

stdlib만 사용. 의존성 없음.

```bash
S=~/.gjc/agent/skills/dooray-weekly/scripts

python3 $S/dooray_weekly.py show                       # 최신 주간보고 (정리된 마크다운)
python3 $S/dooray_weekly.py show <URL|post-id|'#2'>     # 특정 게시글
python3 $S/dooray_weekly.py show --format raw           # 원본 본문
python3 $S/dooray_weekly.py show --format json          # 전체 JSON
python3 $S/dooray_weekly.py list --limit 20             # 게시글 목록
python3 $S/dooray_weekly.py projects --query 주간       # 프로젝트 탐색
```

옵션: `--project`(code 또는 id, 기본 `주간보고`, env `DOORAY_WEEKLY_PROJECT`), `--format`, `--no-comments`.

### 사용하는 API 엔드포인트

| 목적 | 메서드 · 경로 |
| --- | --- |
| 내 프로젝트 목록 | `GET /project/v1/projects?member=me&state=active` |
| 게시글 목록 | `GET /project/v1/projects/{projectId}/posts?order=-postUpdatedAt&postWorkflowClasses=registered,working,closed` |
| 게시글 단건 | `GET /project/v1/projects/{projectId}/posts/{postId}` |
| 댓글 목록·단건 | `GET /project/v1/projects/{projectId}/posts/{postId}/logs[/{logId}]` |
| 댓글 등록 | `POST .../logs` — `{"body":{"mimeType":"text/x-markdown","content":"…"},"fileIdList":["<fileId>"]}` |
| 댓글 수정·삭제 | `PUT` / `DELETE .../logs/{logId}` |
| 첨부 목록 | `GET .../posts/{postId}/files` |
| 첨부 업로드 | `POST https://file-api.gov-dooray.com/uploads/project/v1/projects/{projectId}/posts/{postId}/files` (multipart, 필드명 `file`) |
| 첨부 삭제 | `DELETE .../posts/{postId}/files/{fileId}` |

응답은 `{"header": {"isSuccessful": bool, ...}, "result": ...}` 형태다. `isSuccessful`이 false면 실패로 처리한다.

업로드는 함정이 둘 있다. `api.gov-dooray.com`으로 POST하면 `file-api` 호스트로 **307 리다이렉트**되는데
curl은 교차 호스트 리다이렉트에서 `Authorization` 헤더를 버리므로 401이 난다. `file-api` 호스트로 직접
POST해야 한다. 그리고 `fileIdList`는 **문자열 배열**이다. `[{"id": …}]`로 보내면 `Failed to read HTTP message` 400이 난다.

### 본문 파싱

Dooray 본문은 `mimeType: text/x-markdown`이지만 표는 raw `<table><tr><td>`이고,
셀 안에 다시 마크다운(불릿, 체크박스, `\.` 이스케이프, `<br>`)이 섞인다.

- `parse_document(content)` → `[{title, tables: [{headers, rows}]}]` 구조로 분해 (HWPX 생성기가 소비)
- `render_body(content)` → 사람이 읽는 마크다운
- 연번/업무 칸이 `..` 인 연속 행은 직전 값을 승계한다.
- `dooray://.../tasks/{id}` 링크는 `제목 (task {id})`로 치환한다.

## 2. HWPX 보고서 — `scripts/weekly_hwpx.py`

서식 hwpx의 문단·표 노드를 그대로 **복제**하고 텍스트만 교체한다. XML을 새로 쓰지 않으므로
글꼴·표 테두리·색 배너가 100% 보존된다.

```bash
# 최신 주간보고 → 260818_AIX전략실 주요 업무 보고.hwpx
python3 $S/weekly_hwpx.py

# 특정 게시글 / 출력 경로 / 서식 지정
python3 $S/weekly_hwpx.py --post <URL|id> --output out.hwpx --template 서식.hwpx

# 변환 구조를 JSON으로 뽑아 검토하거나, 손본 JSON으로 다시 생성
python3 $S/weekly_hwpx.py --dump-json parsed.json
python3 $S/weekly_hwpx.py --from-json parsed.json

# 특정 섹션 제외 / 빈 항목 유지
python3 $S/weekly_hwpx.py --exclude-section 기타 --keep-empty

# 생성 + 원본 업무에 hwpx 첨부 댓글 등록 (URL in → hwpx out → Dooray 댓글)
python3 $S/weekly_hwpx.py --post <URL> --attach --comment "초안 첨부합니다."
```

서식 탐색 순서: `--template` → `$WEEKLY_HWPX_TEMPLATE` → `./template/*.hwpx` → `assets/weekly-template.hwpx`.
출력 파일명 기본값은 `{YYMMDD}_{제목}.hwpx`.
스크립트는 cwd 기준으로 서식을 찾으므로 `template/` 디렉토리가 있는 곳에서 실행하거나 `--template`을 준다.

### 문서 구조

```
[제목 배너 표]  AIX전략실 주요 업무 보고
(`26.8.18.(화), AIX전략실)
[섹션 바 표] 1 | AI 연구개발
  1. {업무}
  □ {세부업무} (*담당자)
  [2열 표] 주요 경과 | 향후 계획
```

### 매핑 규칙

| Dooray | HWPX |
| --- | --- |
| `## N. {제목}` 헤딩 | 섹션 바 (번호는 1부터 재부여) |
| `업무` 열 | `N. {업무}` 문단 (연속 행은 하나로 묶음) |
| `세부업무` 열 | `□ {세부업무}` 문단 — 열이 없으면 `□` 줄 생략 |
| `담당자` 열 | `□` 줄 끝의 `(*이름)` |
| `주요 경과` 열 (없으면 `진행사항`) | 2열 표 좌측 |
| `향후 계획` 열 (없으면 `비고`) | 2열 표 우측 |

- 셀 마크다운: 1단계 불릿 → `ㅇ `, 2단계 이상 → ` - `, `[x]` → 문장 끝 ` (완료)`, `※`/번호 줄은 앞에 공백 한 칸. 단일 줄이면 마커 없이 그대로.
- 주요 경과와 향후 계획이 모두 빈 항목은 기본 제외(`--keep-empty`로 유지). 항목이 전부 빠진 업무·섹션도 사라진다.
- 표 행 높이는 셀 폭 기준으로 줄 수를 추정해 다시 계산하고, `hp:linesegarray` 캐시는 제거한다.
- 표 `id`/`zOrder`를 문서 순서대로 재부여하고 `Preview/PrvText.txt`를 새 본문으로 갱신한다.

### 후처리

생성 직후 hwpx-skill의 `fix_namespaces.py` → `finalize_hwpx.py --strip-linesegarray --layout` →
`validate.py`를 차례로 실행한다(`--no-postprocess`로 생략). `VALID` + 구조 검사 통과를 확인한다.
`body_paragraph_without_visible_indent` 경고는 원본 서식에서도 동일하게 나오는 휴리스틱 경고다.

## 3. Dooray 업로드 — `--attach`

`--attach`를 주면 생성한 hwpx를 원본 업무에 업로드하고, 그 파일을 첨부한 댓글을 등록한다.
`--comment`로 본문을 지정하지 않으면 `본문 기준으로 자동 생성한 「{제목}」 초안을 첨부합니다.`가 들어간다.
`--from-json`으로 생성할 때도 JSON의 `source.projectId` / `source.id`가 있으면 첨부된다.

파이썬 API는 `dooray_weekly.upload_file(project_id, post_id, path) -> fileId`와
`dooray_weekly.create_comment(project_id, post_id, content, file_ids) -> logId`다.
잘못 올렸으면 `DELETE .../logs/{logId}`, `DELETE .../posts/{postId}/files/{fileId}`로 되돌린다.

## 작업 지침

- "주간보고 읽어줘/정리해줘" → `dooray_weekly.py show`
- "주간보고 문서/한글파일 만들어줘" → `weekly_hwpx.py`
- "주간보고에 올려줘/첨부해줘" → `weekly_hwpx.py --attach`
- 내용을 손봐야 하면 `--dump-json`으로 뽑아 수정한 뒤 `--from-json`으로 다시 생성한다.
- 조회는 자유롭게 하되, **쓰기(`--attach`, 댓글·첨부 등록·삭제)는 사용자가 명시적으로 요청했을 때만** 실행한다.
- 업무 본문 자체를 수정하는 API는 이 스킬에서 호출하지 않는다.

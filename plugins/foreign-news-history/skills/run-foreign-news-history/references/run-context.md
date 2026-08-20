# 실행 컨텍스트

`run_context.json`은 설정 파일이 아니다. 현재 입력 파일과 `동향스케줄.json`의 해시를 기준으로 매 실행마다 새로 만드는 판단 기록이다. 이전 실행 파일을 복사하거나 사람·작업조 매핑을 재사용하지 않는다.

## 생성 순서

1. 최종보고서에서 작업일을 추출한다.
2. 작업일 전날 보도분과, 작업일 전날에 작업됐지만 보도일은 정확히 하루 더 이전인 좁은 재반영 후보를 Google Sheets에서 가져온다.
3. `[VT] 2026년 24시간 외신 모니터링 및 요약 보고`의 `0. 근무 일정` 탭 메타데이터와 현재 그리드를 매번 다시 읽고 `동향 스케줄` 제목 및 헤더 위치를 동적으로 확인한 뒤, 작업일의 요일 열을 선택한 스키마 버전 2 `동향스케줄.json`을 만든다. 과거 조회 범위나 셀 주소는 재사용하지 않는다.
4. 일본언론동향은 선택 입력으로 받는다. 제공된 경우에만 사용자가 명시한 원본 경로를 입력에 포함하고 주변 폴더에서 경로를 추측하지 않는다. 제공되지 않으면 `not_provided`로 기록한다.
5. `scripts/discover_context.py`로 현재 파일명, 경로, 문서 미리보기, 기사 수, 스케줄, 선택적으로 제공된 일본언론동향, SHA-256과 입력 지문을 수집한다.
   - 필수 입력 문서와 제공된 일본동향은 `read_error`가 비어 있고 기사 제목이 1건 이상이어야 한다. 실패한 문서가 있으면 `run_context`를 만들지 않는다.
6. 스크립트가 프롬프트의 파일 단계별 역할표를 적용하고, Codex가 현재 파일과 작업일 동향스케줄을 함께 읽어 충돌과 예외를 검수한다.
7. 다섯 역할 열의 판단 근거를 각 항목의 `evidence`와 `schedule_refs`에 기록한다. 근거가 없으면 값을 비우고 `confidence: unresolved`를 유지한다.

## 구조

```json
{
  "generated_at": "현재 실행 시각",
  "input_fingerprint": "현재 입력 전체의 지문",
  "job_date": {
    "value": "YYYY-MM-DD",
    "confidence": "document-derived|confirmed|unresolved",
    "evidence": ["최종보고서 본문 또는 파일명 근거"]
  },
  "schedule": {
    "schema_version": 2,
    "job_date": "YYYY-MM-DD",
    "weekday": "작업일 요일",
    "heading": "동향 스케줄",
    "heading_cell": "현재 표 제목 셀",
    "header_cell": "현재 보고서 헤더 셀",
    "source": {"spreadsheet_id": "", "sheet_name": "0. 근무 일정", "range": "현재 실행에서 조회한 실제 A1 범위"},
    "assignments": [
      {"ref": "0. 근무 일정!4", "sheet_row": 4, "report": "시트 원문", "report_cell": "A4", "division": "시트 원문", "division_cell": "B4", "worker": "시트 원문", "worker_cell": "G4"}
    ],
    "sha256": "동향스케줄.json 해시"
  },
  "japan_input": {
    "status": "present_checked|not_provided|unresolved",
    "path": "현재 실행에서 찾은 일본언론동향 절대 경로",
    "files": ["해시·기사 수·미리보기 신호"]
  },
  "final": {
    "workgroup": "현재 근거로 판단한 값 또는 빈 문자열",
    "owner": "현재 근거로 판단한 값 또는 빈 문자열",
    "worker": "현재 근거로 판단한 값 또는 빈 문자열",
    "confidence": "confirmed|inferred|unresolved",
    "evidence": ["파일명, 문서, 스케줄의 구체적 근거"],
    "schedule_refs": ["0. 근무 일정!행번호"]
  },
  "final_disposition": {
    "not_representative_owner": "현재 업무 기준에서 확인한 표기",
    "confidence": "confirmed|inferred|unresolved",
    "evidence": ["유사보도·완전 미포함 기사 처리 기준"]
  },
  "sources": {
    "regular": {"workgroup": "", "owner": "", "worker": "", "priority": 0, "confidence": "", "evidence": [], "schedule_refs": []},
    "japan": {"status": "present_checked|not_provided|unresolved", "workgroup": "", "owner": "", "worker": "", "priority": 0, "confidence": "", "evidence": [], "schedule_refs": []}
  },
  "comparison_order": ["regular_and_japan", "afternoon", "morning"],
  "files": [
    {
      "path": "절대 경로",
      "sha256": "파일 해시",
      "comparison_stage": "morning|afternoon",
      "source_kind": "현재 문서 역할",
      "workgroup": "현재 작업조 또는 빈 문자열",
      "owner": "현재 담당 또는 빈 문자열",
      "worker": "현재 작업자 또는 빈 문자열",
      "priority": 0,
      "include_unmatched": false,
      "confidence": "confirmed|inferred|unresolved",
      "evidence": ["현재 실행에서 확인한 근거"],
      "schedule_refs": ["0. 근무 일정!행번호"]
    }
  ],
  "article_overrides": [
    {"order": 1, "article_title": "현재 결과 제목", "article_media": "현재 결과 매체", "reference_title": "기준표 제목", "reference_media": "기준표 매체", "field": "category|media|canonical_title", "value": "문서 원문", "evidence": ["근거"]}
  ],
  "article_origin_confirmations": [
    {"order": 1, "article_title": "현재 결과 제목", "article_media": "현재 결과 매체", "reference_title": "기준표 제목", "reference_media": "기준표 매체", "source_type": "regular|worker|japan", "source_file": "선택 입력", "source_title": "현재 원본 제목", "evidence": ["낮은 점수나 경합을 직접 확인한 근거"]}
  ],
  "article_role_confirmations": [
    {
      "order": 1,
      "article_title": "현재 결과 제목",
      "article_media": "현재 결과 매체",
      "reference_title": "기준표 제목",
      "reference_media": "기준표 매체",
      "workgroup": "현재 기준표와 당일 근거로 확인한 값",
      "owner": "현재 기준표와 당일 근거로 확인한 값",
      "worker": "현재 당일 작업자",
      "schedule_refs": ["0. 근무 일정!행번호"],
      "reference_file": "사용자가 현재 실행에 제공한 기준 파일 절대 경로",
      "reference_sha256": "현재 기준 파일 SHA-256",
      "evidence": ["현재 원본만으로 복원되지 않는 역할을 기준 파일 해당 행과 대조한 근거"]
    }
  ],
  "article_additions": [
    {
      "kind": "similar|omitted",
      "after_order": 1,
      "source_type": "regular|worker|japan",
      "source_file": "후보가 중복될 때 현재 원본 절대 경로",
      "source_title": "현재 원본의 정확한 제목",
      "category": "같은 작업일 기준표에서 확인한 카테고리",
      "media": "선택: 기준표 표기",
      "date": "선택: 기준표 표기",
      "canonical_title": "선택: 기준표 표기",
      "reference_file": "사용자가 제공한 같은 작업일 기준 파일 절대 경로",
      "reference_sha256": "기준 파일 SHA-256",
      "evidence": ["자동 결과에 없던 행과 현재 원본을 기준 파일 해당 행으로 대조한 근거"]
    }
  ],
  "result_order": {
    "orders": [1, 3, 2],
    "reference_file": "사용자가 제공한 같은 작업일 기준 파일 절대 경로",
    "reference_sha256": "기준 파일 SHA-256",
    "evidence": ["기준표의 기사 행 순서와 현재 최종 기사를 제목·매체로 대조한 근거"]
  },
  "article_japan_confirmations": [
    {"order": 1, "article_title": "현재 결과 제목", "article_media": "현재 결과 매체", "reference_title": "기준표 제목", "reference_media": "기준표 매체", "included": true, "source_file": "현재 일본언론동향 원본", "source_title": "원본의 정확한 제목", "reference_file": "같은 작업일 기준 파일", "reference_sha256": "기준 파일 SHA-256", "evidence": ["제목이 크게 달라진 동일 기사를 현재 원문과 기준표로 대조한 근거"]},
    {"order": 2, "article_title": "현재 결과 제목", "article_media": "현재 결과 매체", "reference_title": "기준표 제목", "reference_media": "기준표 매체", "included": false, "reference_file": "같은 작업일 기준 파일", "reference_sha256": "기준 파일 SHA-256", "evidence": ["기준표의 일일일본동향 공란과 현재 원본을 대조한 근거"]}
  ]
}
```

## 판단 규칙

- 파일 단계별 기본 역할은 고정된 사람 매핑이 아니라 업무 프롬프트의 열 판정 규칙이다: 오후 국내 초안 `1조/국내`, 오후 글로벌 작업본 `1조/글로벌`, 오후 취합본 `오후/오후/총괄`, 오전 보조·초안 `2조/보조`, 오전 n차·최종 총괄본 `2조/오전/총괄`.
- 정기 유입은 `정기/오후/총괄/<당일 오후 취합 작업자>`, 일본언론동향은 작업조 `일본문화원`, 순방 파일은 작업조 `순방`을 사용한다. 일본·순방의 담당과 작업자는 실제 당일 편집 작업본으로 판정한다.
- 정기·당일 외신동향 개별 초안이 자동 기준 이상으로 중복되면 실제 동향 초안을 우선한다. 정기 전일 보도분이 오후와 오전 개별·보조 초안에는 없고 오전 총괄본에서 처음 재반영된 좁은 예외만 `정기/오전/총괄/<오전 총괄 작업자>`를 허용한다.
- 대표기사의 최종 담당은 `오전/총괄`, 유사보도·완전 미포함 기사의 최종 담당은 `최종 보고서 미포함`이며, 두 경우 모두 최종 작업자는 당일 오전 총괄 작업자다.

- 파일명에 사람이 명시돼 있으면 작업자 후보로 사용할 수 있지만, 작업조나 담당까지 과거 사례로 확장하지 않는다.
- 폴더명은 보관·취합 단계 근거로 사용할 수 있지만 작업조 값이나 사람의 역할을 단독으로 확정하는 근거로 사용하지 않는다.
- 당일 스케줄이 있으면 작업일과 작업자·담당을 연결하는 우선 근거로 사용한다.
- 작업조·초벌 담당·초벌 작업자·최종 담당·최종 작업자의 완전 판정에는 현재 `schedule.assignments[].ref`에 실제로 존재하는 `schedule_refs`가 필요하다.
- 스케줄 행은 작업일 요일의 시트 값을 그대로 보존한다. 사람별 역할 매핑을 코드나 설정으로 복사하지 않는다.
- 스케줄의 `division`은 결과의 작업조·담당 값이 아니다. 그날 역할별 작업자를 찾기 위한 원문 근거다. 예를 들어 스케줄에 `오후`가 있어도 개별 한국 관련 초안이면 결과는 현재 업무 표기에 따라 `1조/국내/<당일 작업자>`가 될 수 있다.
- 역할 표기와 사람 이름을 분리한다. `workgroup`·`owner`는 현재 파일의 유입 단계와 업무 의미에서 판단하고, `worker`는 파일명과 같은 역할의 당일 스케줄 행을 대조해 넣는다.
- `final.owner`에는 최종 담당 단계 표기를 넣고 `final.worker`에만 당일 최종 총괄 작업자 이름을 넣는다. 스케줄 작업자 이름을 두 칸 모두에 복사하지 않는다.
- `final.worker`는 파일명상 가장 늦어 보이는 작업자가 아니라 최종 대표본을 실제로 편집한 총괄 작업자다. 최종보고서와 가장 완전한 취합본, 당일 스케줄을 함께 대조한다.
- 정기 유입의 `workgroup`·`owner`는 정기 유입 처리 방식에서 판단하고, `worker`는 그날 정기 기사를 수정한 오후 총괄 파일과 스케줄 행으로 찾는다.
- 일본언론동향이 작업조의 특수 유입 근거가 되더라도 초벌 담당·작업자는 실제로 기사가 처음 편집된 현재 작업본과 당일 스케줄에서 별도로 판단할 수 있다.
- 스케줄의 보고서·구분 표기와 현재 파일 역할을 Codex가 함께 해석한다. 둘이 충돌하면 해당 프로필을 `unresolved`로 둔다.
- 최종보고서 첫 장의 카테고리와 매체 표기를 그대로 사용한다. 고정 별칭 사전을 만들지 않는다.
- 숫자를 포함한 카테고리도 문서 구조로 판정하고, 첫 장·본문 카테고리는 정규화한 실제 이름으로 대응한다. 두 목록의 개수·이름·순서가 다르면 순번으로 연결하거나 결과를 계속 만들지 않는다.
- 문서 표기가 불명확하면 `article_overrides`에 임의 표기를 만들지 않는다.
- 자동 매칭 점수가 낮거나 후보가 경합해 Codex가 현재 원문을 직접 대조한 경우에만 `article_origin_confirmations`를 기록한다. 과거 실행의 확인값은 재사용하지 않는다.
- 현재 입력 파일만으로 역할을 복원할 수 없지만 사용자가 같은 작업일의 권위 있는 기준표를 제공한 경우에만 `article_role_confirmations`를 사용한다. 기준 파일 절대 경로와 SHA-256, 당일 `schedule_refs`, 구체적 행 근거가 모두 필요하다. 이 확인값은 다른 날짜나 기준 파일에 승계하지 않는다.
- 같은 `order`를 공유하는 대표·유사 기사에는 `article_title`·`article_media`와 기준표의 `reference_title`·`reference_media`를 함께 기록한다. 기사별 확인값과 덮어쓰기는 순번만으로 합치거나 적용하지 않는다.
- 자동 추출 결과에 없는 유사보도나 완전 미포함 행을 사용자가 같은 작업일의 권위 있는 기준표로 확인한 경우에만 `article_additions`를 사용한다. 현재 후보의 유입 경로·정확한 제목, 기준 파일 절대 경로·SHA-256, 분류와 삽입 위치, 구체적 행 근거가 모두 필요하다. 해시가 바뀌거나 후보를 하나로 특정하지 못하면 적용하지 않는다.
- 자동 추출된 미포함 행들의 순서가 기준표의 대응 행 순서와 모두 같을 때만 기존 행을 그대로 소비한다. 순서가 다르거나 일부를 특정할 수 없으면 현재 원본 후보에서 `article_additions`를 다시 만들고 기준표 순서로 배치한다.
- 최종보고서 추출 순서와 같은 작업일 기준표의 기사 행 순서가 다른 경우에만 `result_order`를 사용한다. 현재 최종 기사 `order`의 완전한 순열이어야 하며, 기준 파일 해시가 달라지면 적용하지 않는다.
- 최종보고서 기사 유입 경로는 최초 유입의 시간 순서인 ① 정기 작업내역·일본언론동향 ② 전일 밤 오후폴더 ③ 당일 새벽 오전폴더 순서로 비교한다. 이 순서는 실행별 판단값이 아니라 휴먼 업무 절차다.
- 정기와 일본언론동향 양쪽에 모두 있으면 정기 경로를 선택하고 `일일일본동향`은 O로 유지한다.
- `comparison_stage`는 사용자가 지정한 실제 입력 폴더에 따라 정하며 파일명, 사람 이름, `source_kind`, `priority`로 바꾸지 않는다.
- 실제 개별 국내·글로벌 동향 초안이 정기와 자동 기준 이상으로 중복된 경우에는 동향 초안을 우선한다. 제목수정으로 직접 점수가 낮으면 같은 매체·날짜의 초안이 하나이고 의미 있는 제목 관계가 남으며 후속 취합본에서 최종 제목으로 강하게 이어진 경우에만 동일 중복 계보로 인정한다. 이 예외가 아니면 현재 단계에서 기준 점수 이상의 후보가 확인됐다는 이유만으로 뒤 단계의 취합·총괄본이 더 높은 점수나 `priority`를 가져도 선택을 바꾸지 않는다.
- `priority`는 같은 비교 단계 안에서 현재 파일들의 생성·취합 관계를 비교해 정한다. 사람 이름별 고정 숫자를 사용하지 않는다.
- 실제 초벌 파일에서 최종보고서에 빠진 기사까지 추적해야 하는 파일만 `include_unmatched: true`로 둔다. 취합본·중간 총괄본에는 설정하지 않는다.
- 파일 프로필의 `workgroup`·`owner`는 `source_kind`의 업무 표기와 일치해야 한다. 값과 근무표 참조가 모두 있어도 `global_draft`가 `오후/총괄`처럼 의미가 어긋나면 완전한 근거로 인정하지 않는다.
- 일본동향은 선택 입력이며, 제공된 경우에만 사용자가 명시한 정확한 경로를 사용한다. 입력을 생략하면 `not_provided` 근거를 남기고 일본동향 비교 없이 계속 진행한다. 경로를 명시했는데 파일이 없으면 실행을 시작하지 않는다.
- 일본동향의 제목이 최종보고서에서 크게 수정돼 자동 임계치를 넘지 못한 경우, 현재 원본의 정확한 제목과 원문 대조 근거가 있는 `article_japan_confirmations`만 적용한다. 같은 작업일의 권위 기준표가 있으면 O와 공란을 모두 기준 파일 경로·SHA-256에 묶어 기록하며, 공란 확인값은 자동 오탐을 억제한다. 과거 확인값은 재사용하지 않는다.
- 같은 비교 단계 안에서 유입 후보가 경합하면 현재 자료와 업무 원칙으로 판단하고, 근거가 없으면 결과를 `확인 필요`로 남긴다. 현재 원문을 직접 대조한 실행별 `article_origin_confirmations`만 자동 선택을 교정할 수 있다.
- 프로필의 세 필드(`workgroup`, `owner`, `worker`) 중 하나라도 근거 있게 확인되지 않으면 해당 결과 행을 `확인 필요`로 유지한다.

## 허용되는 고정값

고정 가능한 것은 외부 시스템 주소, 입출력 열 스키마, 파일 형식, 안전 검증 절차, 열의 업무 의미와 사용자가 확인한 휴먼 비교 절차뿐이다. `오전/총괄`처럼 결과 양식이 요구하는 단계 표기는 열 의미이며 사람별 매핑이 아니다. 사람 이름, 사람별 작업조·담당, 카테고리 목록, 매체 별칭, 날짜별 배정은 고정하지 않는다.

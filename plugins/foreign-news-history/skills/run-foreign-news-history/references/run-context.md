# 실행 컨텍스트

`run_context.json`은 설정 파일이 아니다. 현재 입력 파일과 `동향스케줄.json`의 해시를 기준으로 매 실행마다 새로 만드는 판단 기록이다. 이전 실행 파일을 복사하거나 사람·작업조 매핑을 재사용하지 않는다.

## 생성 순서

1. 최종보고서에서 작업일을 추출한다.
2. 작업일 전날의 정기 작업내역을 Google Sheets에서 가져온다.
3. `외신 일일동향`의 `근무` 탭을 읽고 작업일의 요일 열을 선택한 `동향스케줄.json`을 만든다.
4. 사용자가 명시한 일본언론동향 원본 경로를 입력에 포함한다. 주변 폴더에서 경로를 추측하지 않는다.
5. `scripts/discover_context.py`로 현재 파일명, 경로, 문서 미리보기, 기사 수, 스케줄, 일본언론동향, SHA-256과 입력 지문을 수집한다.
6. Codex가 현재 파일과 작업일 동향스케줄을 함께 읽고 역할을 판단한다.
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
    "job_date": "YYYY-MM-DD",
    "weekday": "작업일 요일",
    "source": {"spreadsheet_id": "", "sheet_name": "근무", "range": "A1:L100"},
    "assignments": [
      {"ref": "근무!4", "sheet_row": 4, "report": "시트 원문", "division": "시트 원문", "worker": "시트 원문", "worker_cell": "G4"}
    ],
    "sha256": "동향스케줄.json 해시"
  },
  "japan_input": {
    "status": "present_checked|unresolved",
    "path": "현재 실행에서 찾은 일본언론동향 절대 경로",
    "files": ["해시·기사 수·미리보기 신호"]
  },
  "final": {
    "workgroup": "현재 근거로 판단한 값 또는 빈 문자열",
    "owner": "현재 근거로 판단한 값 또는 빈 문자열",
    "worker": "현재 근거로 판단한 값 또는 빈 문자열",
    "confidence": "confirmed|inferred|unresolved",
    "evidence": ["파일명, 문서, 스케줄의 구체적 근거"],
    "schedule_refs": ["근무!행번호"]
  },
  "final_disposition": {
    "not_representative_owner": "현재 업무 기준에서 확인한 표기",
    "confidence": "confirmed|inferred|unresolved",
    "evidence": ["유사보도·완전 미포함 기사 처리 기준"]
  },
  "sources": {
    "regular": {"workgroup": "", "owner": "", "worker": "", "priority": 0, "confidence": "", "evidence": [], "schedule_refs": []},
    "japan": {"status": "present_checked|unresolved", "workgroup": "", "owner": "", "worker": "", "priority": 0, "confidence": "", "evidence": [], "schedule_refs": []}
  },
  "origin_policy": {
    "selection": "priority_then_score|source_order",
    "source_order": ["현재 근거로 판단한 유입 경로 우선순위"],
    "confidence": "confirmed|inferred|unresolved",
    "evidence": ["현재 업무 원칙과 자료에 따른 구체적 근거"]
  },
  "files": [
    {
      "path": "절대 경로",
      "sha256": "파일 해시",
      "source_kind": "현재 문서 역할",
      "workgroup": "현재 작업조 또는 빈 문자열",
      "owner": "현재 담당 또는 빈 문자열",
      "worker": "현재 작업자 또는 빈 문자열",
      "priority": 0,
      "include_unmatched": false,
      "confidence": "confirmed|inferred|unresolved",
      "evidence": ["현재 실행에서 확인한 근거"],
      "schedule_refs": ["근무!행번호"]
    }
  ],
  "article_overrides": [
    {"order": 1, "field": "category|media|canonical_title", "value": "문서 원문", "evidence": ["근거"]}
  ],
  "article_origin_confirmations": [
    {"order": 1, "source_type": "regular|worker|japan", "source_file": "선택 입력", "source_title": "현재 원본 제목", "evidence": ["낮은 점수나 경합을 직접 확인한 근거"]}
  ],
  "article_role_confirmations": [
    {
      "order": 1,
      "workgroup": "현재 기준표와 당일 근거로 확인한 값",
      "owner": "현재 기준표와 당일 근거로 확인한 값",
      "worker": "현재 당일 작업자",
      "schedule_refs": ["근무!행번호"],
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
    {"order": 1, "source_file": "현재 일본언론동향 원본", "source_title": "원본의 정확한 제목", "evidence": ["제목이 크게 달라진 동일 기사를 현재 원문으로 대조한 근거"]}
  ]
}
```

## 판단 규칙

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
- 문서 표기가 불명확하면 `article_overrides`에 임의 표기를 만들지 않는다.
- 자동 매칭 점수가 낮거나 후보가 경합해 Codex가 현재 원문을 직접 대조한 경우에만 `article_origin_confirmations`를 기록한다. 과거 실행의 확인값은 재사용하지 않는다.
- 현재 입력 파일만으로 역할을 복원할 수 없지만 사용자가 같은 작업일의 권위 있는 기준표를 제공한 경우에만 `article_role_confirmations`를 사용한다. 기준 파일 절대 경로와 SHA-256, 당일 `schedule_refs`, 구체적 행 근거가 모두 필요하다. 이 확인값은 다른 날짜나 기준 파일에 승계하지 않는다.
- 자동 추출 결과에 없는 유사보도나 완전 미포함 행을 사용자가 같은 작업일의 권위 있는 기준표로 확인한 경우에만 `article_additions`를 사용한다. 현재 후보의 유입 경로·정확한 제목, 기준 파일 절대 경로·SHA-256, 분류와 삽입 위치, 구체적 행 근거가 모두 필요하다. 해시가 바뀌거나 후보를 하나로 특정하지 못하면 적용하지 않는다.
- 최종보고서 추출 순서와 같은 작업일 기준표의 기사 행 순서가 다른 경우에만 `result_order`를 사용한다. 현재 최종 기사 `order`의 완전한 순열이어야 하며, 기준 파일 해시가 달라지면 적용하지 않는다.
- `priority`는 현재 파일들의 생성·취합 관계를 비교해 정한다. 사람 이름별 고정 숫자를 사용하지 않는다.
- 개별 초안, 정기 유입, 일본언론동향 같은 특수 유입, 오후 취합본, 오전 총괄본이 서로 겹치면 `origin_policy.selection: priority_then_score`를 사용해 파일·경로별 상대 순서를 표현한다. `source_order`만으로 모든 작업본을 한 번에 앞세우지 않는다.
- 실제 초벌 파일에서 최종보고서에 빠진 기사까지 추적해야 하는 파일만 `include_unmatched: true`로 둔다. 취합본·중간 총괄본에는 설정하지 않는다.
- 일본동향은 사용자가 명시한 정확한 경로만 사용한다. 경로가 없거나 파일이 없으면 실행을 시작하지 않는다.
- 일본동향의 제목이 최종보고서에서 크게 수정돼 자동 임계치를 넘지 못한 경우, 현재 원본의 정확한 제목과 원문 대조 근거가 있는 `article_japan_confirmations`만 적용한다. 과거 확인값은 재사용하지 않는다.
- 유입 경로 우선순위도 코드에 고정하지 않는다. 현재 자료와 업무 원칙으로 판단해 `origin_policy`에 근거와 함께 기록한다. 근거가 없으면 모든 경로의 점수를 비교하되 결과를 `확인 필요`로 남긴다.
- 프로필의 세 필드(`workgroup`, `owner`, `worker`) 중 하나라도 근거 있게 확인되지 않으면 해당 결과 행을 `확인 필요`로 유지한다.

## 허용되는 고정값

고정 가능한 것은 외부 시스템 주소, 입출력 열 스키마, 파일 형식, 안전 검증 절차와 열의 업무 의미뿐이다. `오전/총괄`처럼 결과 양식이 요구하는 단계 표기는 열 의미이며 사람별 매핑이 아니다. 사람 이름, 사람별 작업조·담당, 카테고리 목록, 매체 별칭, 날짜별 배정은 고정하지 않는다.

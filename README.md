# 외신동향 작업이력 Codex 플러그인

일일외신보도동향 작업본과 최종보고서를 비교해 기사별 작업이력을 만들고, 중간 엑셀 검수 후 Google Sheets에 반영하는 Codex 플러그인입니다.

## 설치

이 저장소는 공개 Codex 플러그인 마켓플레이스입니다. GitHub 협업자 초대나 저장소 접근 권한 없이 추가할 수 있습니다.

ChatGPT 데스크톱 앱에서 플러그인 디렉터리를 열고 **플러그인 마켓플레이스 추가**를 선택합니다.

- 출처: `sanso62/foreign-news-history-plugin`
- Git ref: `main`
- Sparse 경로: 비워 둡니다.

마켓플레이스를 추가한 뒤 **외신동향 작업이력**에서 `foreign-news-history` 플러그인을 설치하고 새 작업을 시작합니다.

## 업데이트

제작자가 새 버전을 게시한 뒤 설치자는 마켓플레이스를 업그레이드하고 새 작업을 시작합니다.

```powershell
codex plugin marketplace upgrade foreign-news-history
```

## 데이터와 설정

플러그인에는 개인 이름, 작업 결과물, Google Sheets 문서 ID 또는 인증 정보가 포함되지 않습니다. 실행 시 연결된 Google 계정에서 `외신 일일동향` 문서를 찾으며, 같은 이름의 문서가 여러 개이면 사용자가 대상을 선택해야 합니다.

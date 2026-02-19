## PR 생성 빠른 가이드

오류 예시
- `src refspec wk does not match any`

원인
- 존재하지 않는 브랜치명(`wk`)으로 push 시도.
- 현재 브랜치는 보통 `work`.

해결
1. 현재 브랜치 확인
   - `git branch --show-current`
2. 현재 브랜치 그대로 push
   - `git push -u origin <현재브랜치>`
3. PR 생성
   - GitHub 웹에서 `Compare & pull request`
   - 또는 `gh pr create --base main --head <현재브랜치> --fill`

자동화
- `cash-burner/v2/pr_helper.cmd` 실행 시 현재 브랜치를 자동 감지해 push를 시도합니다.

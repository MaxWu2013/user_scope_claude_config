# Skill ownership

Only skills created by me may be renamed. The audit script reads this file.

## Me

Git author names, handles and emails that count as me. The global and
per-repo `git config user.name` / `user.email` are added automatically.
Matching ignores case, spaces, dots, hyphens and underscores, and also
checks the part of an email before `@` (so `Feilong`, `fei.long` and
`feilong@company.com` all match `feilong`).

- 飞龙
- feilong
- wujincheng
- wu jincheng
- jinchengwu
- jinchengwu-ola
- jonathan@gumdrop.work
- 89000073+jinchengwu-ola@users.noreply.github.com

## Mine

Skills confirmed as mine that the script cannot prove (e.g. not in git).
One absolute path or glob pattern per line, `~` allowed
(e.g. `*/.claude/skills/pz-*` covers every clone of a shared repo).

- ~/.claude/skills/wf-refactor-skill-names
- ~/.claude/skills/wf-apply-dropbox-config
- ~/.claude/skills/wf-convert-skills-to-workflows
- ~/.claude/skills/wf-sync-claude-config-repos
- ~/.claude/skills/wf-sync-dropbox-config
- ~/.claude/skills/wf-update-cursor-model
- ~/.claude/skills/wf-refactor-skill-composition
- ~/.claude/skills/tl-route-run-state
- ~/.claude/skills/kn-skill-subagent-composition
- ~/workplace/OlaChat/InfoTank/.claude/skills/wf-sweep-pending-actions
- ~/workplace/OlaChat/InfoTank/.claude/skills/wf-sync-slack-channels
- ~/workplace/OlaChat/PRD/pt-prd/.claude/skills/wf-open-prd-review-room

## External

Skills confirmed as made by someone else. Never rename these.
One absolute path or glob pattern per line, `~` allowed.

- ~/.claude/skills/ralph2ralph

<!-- 2026-10-23: copied in bulk (claude_project_config 54924fa "add flutter skills", pt-zh-web-page_claude 5b2e9b0) -->
- */.claude/skills/assets-update
- */.claude/skills/clean-code-principles
- */.claude/skills/component-creation
- */.claude/skills/composition-patterns
- */.claude/skills/dependabot-check
- */.claude/skills/dependabot-fix
- */.claude/skills/flutter-adding-home-screen-widgets
- */.claude/skills/flutter-animating-apps
- */.claude/skills/flutter-architecting-apps
- */.claude/skills/flutter-building-forms
- */.claude/skills/flutter-building-layouts
- */.claude/skills/flutter-building-plugins
- */.claude/skills/flutter-caching-data
- */.claude/skills/flutter-embedding-native-views
- */.claude/skills/flutter-handling-concurrency
- */.claude/skills/flutter-handling-http-and-json
- */.claude/skills/flutter-implementing-navigation-and-routing
- */.claude/skills/flutter-improving-accessibility
- */.claude/skills/flutter-interoperating-with-native-apis
- */.claude/skills/flutter-localizing-apps
- */.claude/skills/flutter-managing-state
- */.claude/skills/flutter-reducing-app-size
- */.claude/skills/flutter-setting-up-on-linux
- */.claude/skills/flutter-setting-up-on-macos
- */.claude/skills/flutter-setting-up-on-windows
- */.claude/skills/flutter-testing-apps
- */.claude/skills/flutter-theming-apps
- */.claude/skills/flutter-working-with-databases
- */.claude/skills/proto-api-creation
- */.claude/skills/pt-zh-web-page
- */.claude/skills/react-best-practices
- */.claude/skills/react-vite-best-practices
- */.claude/skills/recharge-capsule-machine
- */.claude/skills/recharge-v4
- */.claude/skills/riverpod-3-0-migration
- */.claude/skills/riverpod-auto-dispose
- */.claude/skills/riverpod-cancel
- */.claude/skills/riverpod-codegen-and-hooks
- */.claude/skills/riverpod-consumers
- */.claude/skills/riverpod-containers
- */.claude/skills/riverpod-eager-initialization
- */.claude/skills/riverpod-family
- */.claude/skills/riverpod-faq-and-practices
- */.claude/skills/riverpod-from-provider
- */.claude/skills/riverpod-getting-started
- */.claude/skills/riverpod-migration
- */.claude/skills/riverpod-mutations
- */.claude/skills/riverpod-observers
- */.claude/skills/riverpod-offline
- */.claude/skills/riverpod-overrides
- */.claude/skills/riverpod-providers
- */.claude/skills/riverpod-pull-to-refresh
- */.claude/skills/riverpod-refs
- */.claude/skills/riverpod-retry
- */.claude/skills/riverpod-scoping
- */.claude/skills/riverpod-select
- */.claude/skills/riverpod-testing
- */.claude/skills/state-management
- */.claude/skills/tailwind-best-practices
- */.claude/skills/testing-best-practices
- */.claude/skills/typescript-react-patterns

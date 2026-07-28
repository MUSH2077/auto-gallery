# 为 auto-gallery 贡献

[English](CONTRIBUTING.md)

感谢你帮助改进 auto-gallery。贡献应优先保障数据安全、可观测的失败状态、界面可访问性，
并尊重来源平台规则。

## 开始之前

- 先搜索已有 Issue 和 Pull Request，避免重复。
- Bug、部署问题、Provider 请求和较大功能建议请使用对应 Issue 表单。
- 安全漏洞按 [SECURITY.md](SECURITY.md) 的流程私密报告。
- 大型架构变更请先开 Issue，在投入大量实现前确认边界和迁移路径。

小型文档修复、范围明确的 Bug 修复和测试改进可以直接提交 PR。

## 开发环境

1. Fork 仓库，从默认分支创建范围明确的功能分支。
2. 生成本地密钥并启动服务：

   ```bash
   bash scripts/generate-env.sh
   docker compose up -d --build
   ```

3. 确认后端健康：

   ```bash
   curl http://localhost:8818/api/v1/system/health
   ```

本地迭代、迁移、Provider 开发和前端结构详见
[开发指南](docs/development.zh.md)。

## 分支与提交

使用简短、可描述意图的分支名，例如：

- `fix/x-metadata-import`
- `feat/repository-tag-view`
- `docs/public-installation`

提交信息使用祈使语气，并把无关变更拆开。推荐但不强制使用 `feat:`、`fix:`、
`docs:`、`test:`、`refactor:` 等前缀。维护者可能使用 squash merge，因此 PR
标题应可直接作为最终变更记录。

## 必要检查

提交 PR 前运行与你的变更相关的检查。

后端：

```bash
docker compose run --rm -T --volume "$PWD/backend:/app" \
  -e PYTHONDONTWRITEBYTECODE=1 backend \
  python -m pytest
docker compose run --rm -T --volume "$PWD/backend:/app" \
  -e PYTHONDONTWRITEBYTECODE=1 backend \
  ruff check app tests
```

管理前端：

```bash
npm --prefix admin-web ci
npm --prefix admin-web run typecheck
npm --prefix admin-web run build
npm --prefix admin-web run test:e2e
```

仓库与部署：

```bash
docker compose --env-file .env.ci config --quiet
bash scripts/privacy-scan.sh
bash scripts/package-release.sh ci
```

端到端测试需要 Playwright 浏览器。如果某项检查无法在你的环境中运行，请在 PR
中明确写出跳过了哪项检查以及原因。

## 变更要求

### 后端与数据库

- 业务逻辑放在 service，数据库访问放在 repository。
- API 输入输出使用 Pydantic schema。
- 导入和维护操作必须幂等。
- 为 API 契约、Provider 解析、任务状态变更和回归场景增加测试。
- 审查 Alembic 自动生成结果；schema 变更同时验证 upgrade 和 downgrade，并披露
  数据丢失风险。
- 不要增加临时的下载任务状态跳转；状态机与测试必须同步更新。

### Provider

- 记录 URL 校验、规范化、认证、gallery-dl 配置、元数据映射和脱敏 fixture。
- 绝不提交真实 Cookie 或私有内容 URL。
- Provider 模块不得直接访问数据库。
- 必要时同步更新 worker 队列配置。

### 管理前端

- 复用共享导航、页面容器、反馈、列表和文本溢出组件。
- 同时支持鼠标、键盘和触屏；关键操作不能只在 hover 时出现。
- 检查窄屏、长文本、中英文、浅色/深色主题和降低动效模式。
- 所有面向用户的文字必须同时提供中英文。
- 工作流发生实质变化时同步更新截图或文档。

### 文档

- 同时维护已有的中英文版本。
- 示例必须是虚构数据或完整脱敏数据。
- 命令应能从文档标注的工作目录直接运行。

## PR 要求

一个便于审查的 PR 应：

- 说明用户问题和所选择的实现边界；
- 在存在相关 Issue 时进行关联；
- 明确数据库迁移、配置变化和兼容性影响；
- 提供回归测试，或说明测试不适用的理由；
- UI 变更提供可访问且脱敏的截图或录屏；
- 不包含密钥、私有创作者数据、本机绝对路径或运行时媒体。

请保持 PR 聚焦。范围过大时维护者可能要求拆分。合并前请解决审查对话，或解释仍有
分歧的原因。

欢迎使用 AI 辅助完成贡献；如辅助程度较高，请在 PR 中披露，并逐行审查生成内容。
提交者仍需对正确性、许可、安全性和测试负责。

## 许可

提交贡献即表示你同意按仓库的 [AGPL-3.0-only 许可证](LICENSE)发布该贡献。
请只提交你有权许可的内容。本项目目前不要求另行签署 CLA。

## 合法与平台边界

贡献不得鼓励绕过付费墙、DRM、访问控制、速率限制或平台限制。auto-gallery
仅应帮助用户归档其有权访问和下载的内容。

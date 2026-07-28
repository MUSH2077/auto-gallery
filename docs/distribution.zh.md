# 分发与隐私

auto-gallery 面向局域网优先的个人媒体归档场景。发布远端仓库、提交 issue 或构建 release 包之前，都应默认把仓库视为公开内容来检查。

## 远端仓库

公开分发建议使用中性的组织或项目 namespace：

```bash
git remote set-url origin git@github.com:<neutral-org>/auto-gallery.git
git remote -v
```

不要在 tracked 文件中硬编码个人 GitHub 用户名、本地主机名或本机 home 路径。如果开发阶段使用过个人远端，只在本地 Git config 中修改，不要把个人 URL 写入文档。

## 隐私 Checklist

push 或发布 release 前运行：

```bash
scripts/privacy-scan.sh
scripts/package-release.sh
```

不要提交或附带：

- `.env`、cookies、refresh tokens、API keys、密码、SSH keys 或数据库 dump。
- `data/`、`downloads/`、`library/`、`app-config/`、`gallerydl-config/`、日志、备份、Meilisearch/PostgreSQL/Redis 数据卷。
- 私有创作者 URL、已下载媒体、包含真实创作者的截图或 Playwright 调试快照。
- `/home/<user>/...` 这类本机路径。

`.env.example` 只保留占位值。公开文档截图使用 `docs/assets/` 下的脱敏 SVG。

## Release 包

release 打包脚本基于 tracked files，并会移除本地协作上下文和运行时路径：

```bash
scripts/package-release.sh v0.1.0
tar -tzf dist/auto-gallery-v0.1.0.tar.gz | less
```

打包结果会排除 `.claude/`、`.playwright-mcp/`、`.env*`、运行时数据目录、缓存和日志。v1 仍以 Docker Compose 为分发目标；Helm、托管 installer 和云部署模板暂不纳入范围。

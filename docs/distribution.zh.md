# 分发与隐私

auto-gallery 面向个人媒体归档并采用局域网优先设计。在修改仓库可见性前，应把每个已跟踪
文件、提交、PR、工作流日志、构建产物和发布压缩包都视为即将公开的内容。

官方仓库：

```text
https://github.com/MUSH2077/auto-gallery
```

官方仓库所有者和 URL 属于公开项目元数据，不是隐私标识；本地用户名、主机名、HOME
路径、无关个人远端、私有来源账号和运行时媒体仍然禁止进入仓库。

## 隐私检查

每次公开推送或发布前运行：

```bash
bash scripts/privacy-scan.sh
bash scripts/package-release.sh
```

绝不要提交或附加：

- `.env`、Cookie、refresh token、API key、密码、SSH key 或数据库备份
- `data/`、`downloads/`、`library/`、应用配置、gallery-dl 配置、日志、备份或
  服务数据卷
- 私有创作者 URL、已下载媒体、私有账号名或浏览器调试快照
- `/home/<user>/...`、`/Users/<user>/...` 等本机路径

占位配置使用 `.env.example`；文档图片使用 `docs/assets/` 下的虚构或完整脱敏素材。

## 历史与可见性检查

当前工作树干净并不代表可以公开：Git 历史和 GitHub Actions 历史仍可能保留已删除数据。
把私有仓库转为公开前：

1. 检查全部 Git 对象，查找曾被跟踪的环境文件、凭据、备份、密钥、大型媒体和本机账号数据。
2. 检查 Actions 日志、Artifacts、缓存、Release 资源和旧 PR。
3. 任何可能曾被提交或输出的凭据都必须轮换；删除文件或重写历史不能让泄露密钥重新安全。
4. 如果必须重写历史，要求协作者清理旧 clone。
5. 可见性变化后用未登录浏览器验证仓库。

重写历史具有破坏性，只能在确认准确路径和协作方案后执行。

## 发布压缩包

发布脚本从已跟踪文件开始，移除协作上下文、环境文件、运行时路径、缓存和日志：

```bash
bash scripts/package-release.sh v0.1.0-beta.1
tar -tzf dist/auto-gallery-v0.1.0-beta.1.tar.gz | less
```

当前分发目标为 Docker Compose；托管安装器和云部署模板不在 Beta 范围内。

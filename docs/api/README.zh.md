# 局域网 API 契约

本目录保存由运行中的 FastAPI 应用生成、可纳入版本控制的机器可读契约：

- `openapi.json` 描述 `/api/v1` 下的全部公开 HTTP 操作。
- `asyncapi.yaml` 描述公开的 `/api/v1/ws` WebSocket 协议。
- `capability-matrix.md` 对照产品行为与公开 API 能力。

不要手工编辑 OpenAPI 文件。修改路由、请求/响应模型、权限依赖或 WebSocket
消息后运行：

```bash
cd backend
python scripts/export_api_contracts.py
cd ../admin-web
npm run generate:api-types
```

CI 会以 check 模式重新生成契约，并比较生成的 TypeScript 类型；运行时 schema
与仓库文件不一致时不能合并。

## 在线文档

管理员可在 `/api/docs` 使用 Swagger UI，或在 `/api/redoc` 查看只读参考。
界面所需 JavaScript、CSS 和字体均由本机提供，断网也可使用。对应契约端点同样
受管理员鉴权保护：

- `/api/openapi.json`
- `/api/asyncapi.yaml`

有效的管理员会话 Cookie 可以打开文档；从 Swagger 执行业务操作时，仍需在
**Authorize** 中填写 `POST /api/v1/auth/login` 返回的显式 JWT Bearer。
业务 API 不接受 Cookie 鉴权。

旧 `/docs`、`/redoc` 与 `/openapi.json` 会永久重定向到受保护的 `/api/*`
地址。

## 局域网来源

CORS 始终使用精确白名单。每个可信客户端都要把协议和端口写入
`CORS_ORIGINS`，例如：

```dotenv
CORS_ORIGINS=http://gallery-client.example.test:3000
```

携带凭据的局域网部署不要使用 `*`。

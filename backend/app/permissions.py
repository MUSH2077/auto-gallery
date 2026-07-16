"""Permission module registry for non-admin users.

Each key is a stored value inside ``User.permissions`` (a JSONB list of
strings); the value is the Chinese display label shown in the admin UI.
``UserService`` validates that any permissions assigned to a user are a
subset of these keys.
"""

PERMISSION_MODULES: dict[str, str] = {
    "library": "图库浏览",
    "curation": "策展操作",
    "upload": "手动上传",
    "subscriptions": "订阅与来源",
    "tasks": "任务中心",
    "system": "系统与设置",
}

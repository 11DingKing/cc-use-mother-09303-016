"""服务端统一错误类型。"""
from __future__ import annotations


class ApiError(Exception):
    """携带 HTTP 状态码、业务错误码与可选详情的异常。"""

    def __init__(self, status: int, code: str, message: str, details: object = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def bad_request(message: str) -> ApiError:
    return ApiError(400, "BAD_REQUEST", message)


def unauthorized(message: str = "缺少或无效的访问令牌") -> ApiError:
    return ApiError(401, "UNAUTHORIZED", message)


def forbidden(message: str = "当前角色无权执行该操作") -> ApiError:
    return ApiError(403, "FORBIDDEN", message)


def not_found(message: str) -> ApiError:
    return ApiError(404, "NOT_FOUND", message)


def conflict(code: str, message: str) -> ApiError:
    return ApiError(409, code, message)


def unprocessable(code: str, message: str, details: object = None) -> ApiError:
    return ApiError(422, code, message, details)

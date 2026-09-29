"""统一业务异常。"""


class ApiError(Exception):
    """携带 HTTP 状态码与业务错误码的异常。"""

    def __init__(self, status: int, code: str, message: str, details=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details

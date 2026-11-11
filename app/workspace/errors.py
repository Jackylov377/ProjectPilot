"""Stable business failures shared by workspace services."""


class Problem(ValueError):
    def __init__(self, code, message, status=409, *, headers=None):
        self.code, self.message, self.status = code, message, status
        self.headers = headers
        super().__init__(message)

"""Stable business failures shared by workspace services."""


class Problem(ValueError):
    def __init__(self, code, message, status=409):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)

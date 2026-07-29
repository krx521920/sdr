"""Stable failure types understood by the durable job runner."""


class AutomationJobError(RuntimeError):
    def __init__(self, message: str, *, code: str, retryable: bool):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class RetryableJobError(AutomationJobError):
    def __init__(self, message: str, *, code: str = "temporary_failure"):
        super().__init__(message, code=code, retryable=True)


class PermanentJobError(AutomationJobError):
    def __init__(self, message: str, *, code: str = "permanent_failure"):
        super().__init__(message, code=code, retryable=False)

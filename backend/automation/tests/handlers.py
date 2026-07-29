from automation.errors import PermanentJobError, RetryableJobError


def echo_job(payload):
    return {"echo": payload.get("value")}


def retryable_failure(payload):
    raise RetryableJobError("Temporary test failure", code="test_temporary")


def permanent_failure(payload):
    raise PermanentJobError("Permanent test failure", code="test_permanent")

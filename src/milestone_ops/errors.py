"""转化里程碑与拨付联动服务向 API 和 CLI 暴露的稳定错误。"""


class MilestoneOpsError(RuntimeError):
    code = "milestone_error"
    status = 400


class NotFound(MilestoneOpsError):
    code = "not_found"
    status = 404


class Conflict(MilestoneOpsError):
    code = "conflict"
    status = 409


class Forbidden(MilestoneOpsError):
    code = "forbidden"
    status = 403


class InvalidState(MilestoneOpsError):
    code = "invalid_state"
    status = 409


class ValidationFailed(MilestoneOpsError):
    code = "validation_failed"
    status = 422

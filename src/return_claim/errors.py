"""退货索赔协同的领域错误类型。"""


class DomainError(Exception):
    """业务规则冲突（状态机、数量、库存等）。"""


class NotFoundError(DomainError):
    """目标单据不存在。"""


class ValidationError(DomainError):
    """入参不合法。"""


class PermissionDenied(DomainError):
    """角色无权执行该操作。"""

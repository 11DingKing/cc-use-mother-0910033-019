"""领域错误。命令校验失败时抛出，服务层映射为 4xx，不落任何事件。"""
from __future__ import annotations


class DomainError(Exception):
    """所有领域规则冲突的基类。"""


class CaseNotFound(DomainError):
    """退货索赔单不存在。"""


class LineNotFound(DomainError):
    """退货行不存在。"""


class IllegalState(DomainError):
    """当前状态不允许该操作。"""


class QuantityConflict(DomainError):
    """数量口径冲突（超过申请/批准/实物/责任/库存余量等）。"""


class LiabilityMissing(DomainError):
    """批准退货前缺少责任认定。"""


class EvidenceMissing(DomainError):
    """需要实物签退结论时缺少物流证据。"""


class RevokedConflict(DomainError):
    """退货行已撤销，不能再调整。"""

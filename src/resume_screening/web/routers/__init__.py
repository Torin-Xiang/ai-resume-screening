"""HTTP 路由层。只做参数校验与调用 services。"""

from . import events, jobs, ranking, resumes

__all__ = ["events", "jobs", "ranking", "resumes"]

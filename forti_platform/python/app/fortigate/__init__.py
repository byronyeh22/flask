from .policy import forti_policy_bp
from .device import forti_device_bp
from .drafts import forti_drafts_bp
from .tasks import forti_tasks_bp
from .pipeline import forti_pipeline_bp
from .task_list import forti_task_list_bp
from . import routes

__all__ = [
    "forti_policy_bp",
    "forti_device_bp",
    "forti_drafts_bp",
    "forti_tasks_bp",
    "forti_pipeline_bp",
    "forti_task_list_bp",
]

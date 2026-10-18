from django.apps import AppConfig


class SrcConfig(AppConfig):
    name = 'src'

    def ready(self):
        from src import audit  # noqa: F401  (connects the audit signal handlers)

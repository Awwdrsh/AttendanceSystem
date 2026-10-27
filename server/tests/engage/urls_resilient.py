"""config/urls.py, minus any app whose urls module does not import right now (other slices are mid-edit in the same
tree). Used by these tests only when the real URLconf fails to load."""
import logging
import re
from pathlib import Path

from django.urls import include, path

urlpatterns = []
for _app in re.findall(r"include\(\('hajiri\.(\w+)\.urls'", (Path(__file__).resolve().parents[2] / 'config' / 'urls.py').read_text()):
    try:
        urlpatterns.append(path('', include((f'hajiri.{_app}.urls', _app))))
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning('skipping %s urls', _app)

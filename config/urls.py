from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.static import serve


def serve_local_media(request, path):
    # Resolve MEDIA_ROOT at request time so deployment overrides are respected.
    return serve(request, path, document_root=settings.MEDIA_ROOT, show_indexes=False)


urlpatterns = [path("admin/", admin.site.urls), path("", include("core.urls"))]

# AutoZapaz is a local desktop/server application. Waitress is bound to loopback
# by default; serve uploaded shop media explicitly even when DEBUG=False.
if getattr(settings, "AUTOZAPAZ_SERVE_MEDIA", True):
    urlpatterns.append(re_path(r"^media/(?P<path>.*)$", serve_local_media))

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

admin.site.site_header = f"{settings.EWCE_PRODUCT_NAME} — Django admin (fallback)"
admin.site.site_title = settings.EWCE_PRODUCT_NAME

urlpatterns = [
    path("accounts/login/", auth_views.LoginView.as_view(template_name="registration/login.html"), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("django-admin/", admin.site.urls),
    path("", include("apps.ui.urls")),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

from django.conf import settings

from apps.core.roles import is_admin, is_operator


def ewce(request):
    user = getattr(request, "user", None)
    return {"is_ewce_admin": is_admin(user), "is_ewce_operator": is_operator(user),
            "product_name": settings.EWCE_PRODUCT_NAME, "product_tagline": settings.EWCE_PRODUCT_TAGLINE}

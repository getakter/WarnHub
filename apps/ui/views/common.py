"""Small helpers shared by UI views."""
import json

from django.contrib import messages
from django.db import transaction
from django.db.models import ProtectedError
from django.http import HttpResponse
from django.shortcuts import redirect, render

from apps.core.audit import audit


def render_form(request, form, *, title, back_url, submit="Save", extra=None, template="ui/form.html", **ctx):
    return render(request, template, {"form": form, "title": title, "back_url": back_url, "submit": submit,
                                      "extra_forms": extra or [], **ctx})


def delete_with_confirm(request, obj, *, back_url, success_url, warning=""):
    if request.method == "POST":
        label = str(obj)
        try:
            with transaction.atomic():
                audit("delete", obj)
                obj.delete()
        except ProtectedError as exc:
            blockers = {str(type(o)._meta.verbose_name_plural) for o in exc.protected_objects}
            messages.error(
                request,
                f"Cannot delete {label}: it is still referenced by {', '.join(sorted(blockers))}. "
                "Disable it instead (nothing is ever silently deleted).",
            )
            return redirect(back_url)
        messages.success(request, f"Deleted {label}. Its last configuration is kept in version history.")
        return redirect(success_url)
    return render(request, "ui/confirm_delete.html", {"object": obj, "back_url": back_url, "warning": warning})


def json_download(data, filename):
    response = HttpResponse(
        json.dumps(data, indent=2, ensure_ascii=False, default=str), content_type="application/json; charset=utf-8"
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def form_errors_to_messages(request, *forms):
    for form in forms:
        for error in form.non_field_errors():
            messages.error(request, error)

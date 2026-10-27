"""Mailbox accounts, routing rules, connection test, inbox preview, rule tester, poll now."""
from django.contrib import messages as flash
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.collection.pipeline import RunContext
from apps.core.audit import audit
from apps.core.roles import admin_required, operator_required
from apps.core.versioning import version_context, versions_for
from apps.mailbox import service
from apps.mailbox.models import MailboxAccount, MailboxRule, MailboxSeenMessage
from apps.plugins.registry import PluginNotFound, registry
from apps.plugins.schema_forms import SchemaForm
from apps.sources.services import unique_value

from ..forms import MailboxForm, MailboxRuleForm
from .common import delete_with_confirm, render_form


@operator_required
def mailbox_list(request):
    mailboxes = MailboxAccount.objects.annotate(rule_count=Count("rules", distinct=True))
    return render(request, "ui/mailboxes/list.html", {"mailboxes": mailboxes})


@operator_required
def mailbox_detail(request, pk):
    mailbox = get_object_or_404(MailboxAccount, pk=pk)
    return render(request, "ui/mailboxes/detail.html", {
        "mailbox": mailbox,
        "rules": mailbox.rules.select_related("target_feed__source"),
        "seen": MailboxSeenMessage.objects.filter(mailbox=mailbox).select_related("rule")[:30],
        "runs": mailbox.runs.all()[:8],
        "versions": versions_for(mailbox)[:8],
        "unrouted": mailbox.messages.filter(status="unrouted").count(),
    })


@admin_required
def mailbox_edit(request, pk=None):
    mailbox = get_object_or_404(MailboxAccount, pk=pk) if pk else MailboxAccount(folders=["INBOX"])
    protocol = request.POST.get("protocol") or request.GET.get("protocol") or mailbox.protocol
    form = MailboxForm(request.POST or None, instance=mailbox, initial={"protocol": protocol})
    try:
        plugin = registry.get("mailbox_protocol", protocol)
    except PluginNotFound:
        plugin = registry.get("mailbox_protocol", "imap")
    oform = SchemaForm(plugin.schema(), mailbox.options if protocol == mailbox.protocol else {},
                       data=request.POST if request.method == "POST" else None, prefix="o")
    if request.method == "POST":
        if oform.is_valid():
            mailbox.options = oform.config
            if form.is_valid():
                form.save()
                audit("update" if pk else "create", mailbox)
                flash.success(request, f"Saved mailbox {mailbox}. Use 'Test connection' to check the login.")
                return redirect(mailbox.get_absolute_url())
        flash.error(request, "Please fix the errors below.")
    schema_forms = [(f"{plugin.label} options", oform)] if oform.schema.get("properties") else []
    return render_form(request, form, title=f"Edit mailbox {mailbox}" if pk else "New mailbox",
                       back_url=mailbox.get_absolute_url() if pk else reverse("ui:mailbox_list"),
                       schema_forms=schema_forms,
                       method_help="The password is never stored here: put it in .env (e.g. EWCE_MAILBOX_ALERTS_PASSWORD=...) "
                                   "and enter only the variable name. For Gmail/Outlook use an app password.")


@admin_required
def mailbox_delete(request, pk):
    mailbox = get_object_or_404(MailboxAccount, pk=pk)
    return delete_with_confirm(request, mailbox, back_url=mailbox.get_absolute_url(),
                               success_url=reverse("ui:mailbox_list"),
                               warning="Collected messages and archived emails are kept.")


@admin_required
@require_POST
def mailbox_clone(request, pk):
    original = get_object_or_404(MailboxAccount, pk=pk)
    rules = list(original.rules.all())
    with version_context(change_type="clone", comment=f"Cloned from mailbox #{pk}"):
        copy = MailboxAccount.objects.get(pk=pk)
        copy.pk = None
        copy._state.adding = True
        copy.label = unique_value(MailboxAccount, "label", f"{original.label} (copy)", 200)
        copy.is_active = False
        copy.is_example = False
        for attr in MailboxAccount.version_exclude_fields:
            setattr(copy, attr, MailboxAccount._meta.get_field(attr).get_default())
        copy.save()
        for rule in rules:
            rule.pk = None
            rule._state.adding = True
            rule.mailbox = copy
            rule.save()
    audit("clone", copy, cloned_from=pk)
    flash.success(request, f"Cloned as {copy} (disabled).")
    return redirect(reverse("ui:mailbox_edit", args=[copy.pk]))


@admin_required
@require_POST
def mailbox_toggle(request, pk):
    mailbox = get_object_or_404(MailboxAccount, pk=pk)
    mailbox.is_active = not mailbox.is_active
    mailbox.save()
    audit("enable" if mailbox.is_active else "disable", mailbox)
    return redirect(mailbox.get_absolute_url())


@admin_required
@require_POST
def mailbox_test(request, pk):
    mailbox = get_object_or_404(MailboxAccount, pk=pk)
    ok, message, details = service.test_connection(mailbox)
    (flash.success if ok else flash.error)(request, message + "".join(f" · {f}: {d}" for f, d in details))
    return redirect(mailbox.get_absolute_url())


@operator_required
@require_POST
def mailbox_poll(request, pk):
    mailbox = get_object_or_404(MailboxAccount, pk=pk)
    run = service.poll_mailbox(mailbox, trigger="manual", user=request.user)
    audit("poll_mailbox", mailbox, run=run.pk, status=run.status)
    level = {"success": flash.success, "partial": flash.warning}.get(run.status, flash.error)
    level(request, f"Poll finished: {run.status}. " + ", ".join(f"{k} {v}" for k, v in sorted(run.stats.items())))
    return redirect(run.get_absolute_url())


@operator_required
def mailbox_preview(request, pk):
    mailbox = get_object_or_404(MailboxAccount, pk=pk)
    rows, error = service.preview_inbox(mailbox, limit=int(request.GET.get("limit") or 20))
    return render(request, "ui/mailboxes/preview.html", {"mailbox": mailbox, "rows": rows, "error": error})


@operator_required
def mailbox_tester(request, pk):
    mailbox = get_object_or_404(MailboxAccount, pk=pk)
    context = {"mailbox": mailbox}
    raw = None
    if request.method == "POST":
        upload = request.FILES.get("eml")
        if upload:
            raw = upload.read()
        elif request.POST.get("source", "").strip():
            raw = request.POST["source"].replace("\r\n", "\n").encode("utf-8")
        elif request.POST.get("seen"):
            seen = get_object_or_404(MailboxSeenMessage, pk=request.POST["seen"], mailbox=mailbox)
            from apps.collection.archive import read_artifact

            raw = read_artifact(seen.raw_artifact) if seen.raw_artifact else None
        if not raw:
            flash.error(request, "Upload an .eml file or paste the full email source.")
        else:
            info, verdicts, plans = service.explain(mailbox, raw)
            context.update({"info": info, "verdicts": verdicts, "plans": plans})
            if request.POST.get("dry_run") and plans:
                results = []
                for plan in plans:
                    ctx = RunContext(plan["rule"].target_feed, trigger="test", dry_run=True, mailbox=mailbox,
                                     honour_limits=False)
                    service.deliver(plan["rule"], info, raw, None, ctx)
                    results.extend(ctx.results)
                context["results"] = results
    context["recent"] = MailboxSeenMessage.objects.filter(mailbox=mailbox, raw_artifact__isnull=False)[:15]
    return render(request, "ui/mailboxes/tester.html", context)


# --- Rules ---------------------------------------------------------------------------
@admin_required
def mailbox_rule_edit(request, mailbox_pk, pk=None):
    mailbox = get_object_or_404(MailboxAccount, pk=mailbox_pk)
    if pk:
        rule = get_object_or_404(MailboxRule, pk=pk, mailbox=mailbox)
    else:
        last = mailbox.rules.order_by("-position").first()
        rule = MailboxRule(mailbox=mailbox, position=(last.position + 10) if last else 10)
    form = MailboxRuleForm(request.POST or None, instance=rule)
    if request.method == "POST" and form.is_valid():
        form.save()
        audit("update" if pk else "create", rule)
        flash.success(request, f"Saved rule {rule.name}. Try it with the Rule tester.")
        return redirect(mailbox.get_absolute_url() + "#rules")
    return render_form(request, form, title=f"{'Edit' if pk else 'New'} routing rule — {mailbox}",
                       back_url=mailbox.get_absolute_url(),
                       method_help="Conditions you leave empty are ignored. With 'All conditions must match', every filled "
                                   "condition must be true. Rules are checked top to bottom.")


@admin_required
def mailbox_rule_delete(request, mailbox_pk, pk):
    rule = get_object_or_404(MailboxRule, pk=pk, mailbox_id=mailbox_pk)
    url = rule.mailbox.get_absolute_url() + "#rules"
    return delete_with_confirm(request, rule, back_url=url, success_url=url)


@admin_required
@require_POST
def mailbox_rule_action(request, mailbox_pk, pk, action):
    from apps.sources.services import reorder

    rule = get_object_or_404(MailboxRule, pk=pk, mailbox_id=mailbox_pk)
    mailbox = rule.mailbox
    if action == "toggle":
        rule.enabled = not rule.enabled
        rule.save()
    elif action == "clone":
        rule.pk = None
        rule._state.adding = True
        rule.name = f"{rule.name} (copy)"[:200]
        rule.enabled = False
        rule.position += 1
        rule.save()
    elif action in {"up", "down"}:
        ordered = list(mailbox.rules.all())
        i = next(index for index, r in enumerate(ordered) if r.pk == rule.pk)
        j = i - 1 if action == "up" else i + 1
        if 0 <= j < len(ordered):
            ordered[i], ordered[j] = ordered[j], ordered[i]
            reorder(mailbox.rules.all(), [r.pk for r in ordered])
    return redirect(mailbox.get_absolute_url() + "#rules")

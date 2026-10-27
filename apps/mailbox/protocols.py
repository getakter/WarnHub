"""Mailbox protocol plugins.

A protocol's ``connect(account)`` returns a connection with this small interface, so POP3, Microsoft
Graph or the Gmail API can be added later as plugins without touching the routing/collection code:

    folders() -> [name]                 uidvalidity(folder) -> str
    search_uids(folder, since=date|None, unseen_only=bool) -> [uid]  (oldest first)
    fetch_raw(folder, uids) -> {uid: bytes}   (untouched RFC 822 source; must not mark as read)
    counts(folder) -> {"messages": n, "unseen": n}
    mark_read(folder, uid) · move(folder, uid, target) · label(folder, uid, label) · close()
"""
import os

from apps.plugins.registry import Plugin, register


class MailboxError(Exception):
    """Shown to operators (connection, login, configuration problems)."""


def password_for(account):
    password = os.environ.get(account.password_env_var or "")
    if password is None:
        raise MailboxError(f"Environment variable {account.password_env_var} is not set. Add it to the .env file "
                           "and restart the server and worker.")
    return password


class MailboxProtocol(Plugin):
    kind = "mailbox_protocol"

    def connect(self, account):
        raise NotImplementedError


class ImapConnection:
    def __init__(self, account, timeout=60):
        from imap_tools import MailBox, MailBoxStartTls, MailBoxUnencrypted

        password = password_for(account)
        cls = {"ssl": MailBox, "starttls": MailBoxStartTls, "none": MailBoxUnencrypted}[account.security]
        try:
            self.mb = cls(account.host, account.port, timeout=timeout)
            self.mb.login(account.username, password, initial_folder=None)
        except Exception as exc:  # noqa: BLE001 - imaplib raises many types
            raise MailboxError(f"Could not log in to {account.host}:{account.port} as {account.username}: {exc}") from exc
        self.current = None

    def _select(self, folder):
        if self.current != folder:
            self.mb.folder.set(folder)
            self.current = folder

    def folders(self):
        return [f.name for f in self.mb.folder.list()]

    def uidvalidity(self, folder):
        return str(self.mb.folder.status(folder, ["UIDVALIDITY"])["UIDVALIDITY"])

    def counts(self, folder):
        status = self.mb.folder.status(folder, ["MESSAGES", "UNSEEN"])
        return {"messages": status.get("MESSAGES", 0), "unseen": status.get("UNSEEN", 0)}

    def search_uids(self, folder, since=None, unseen_only=False):
        from imap_tools import AND

        self._select(folder)
        criteria = {}
        if since:
            criteria["date_gte"] = since
        if unseen_only:
            criteria["seen"] = False
        uids = self.mb.uids(AND(**criteria) if criteria else "ALL")
        return sorted(uids, key=int)

    def fetch_raw(self, folder, uids):
        self._select(folder)
        out = {}
        for uid in uids:
            status, data = self.mb.client.uid("FETCH", str(uid), "(BODY.PEEK[])")
            if status != "OK":
                continue
            for part in data:
                if isinstance(part, tuple) and len(part) > 1:
                    out[str(uid)] = part[1]
                    break
        return out

    def mark_read(self, folder, uid):
        from imap_tools import MailMessageFlags

        self._select(folder)
        self.mb.flag(str(uid), MailMessageFlags.SEEN, True)

    def move(self, folder, uid, target):
        self._select(folder)
        self.mb.move(str(uid), target)

    def label(self, folder, uid, label):
        """Gmail: X-GM-LABELS; other servers: a custom IMAP keyword."""
        self._select(folder)
        if "X-GM-EXT-1" in getattr(self.mb.client, "capabilities", ()):
            self.mb.client.uid("STORE", str(uid), "+X-GM-LABELS", f'("{label}")')
        else:
            self.mb.flag(str(uid), label, True)

    def close(self):
        try:
            self.mb.logout()
        except Exception:  # noqa: BLE001 - already disconnected
            pass


@register
class ImapProtocol(MailboxProtocol):
    name = "imap"
    label = "IMAP"
    description = "Any IMAP server (Gmail, Outlook/Office 365 with IMAP enabled, cPanel, government mail)."
    config_schema = {
        "type": "object",
        "properties": {
            "timeout_seconds": {"type": "integer", "minimum": 5, "maximum": 300, "default": 60},
        },
    }

    def connect(self, account):
        return ImapConnection(account, timeout=self.config.get("timeout_seconds", 60))

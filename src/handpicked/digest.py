"""Render the admin digest and send it by email."""

import smtplib
import ssl
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from functools import cache

from jinja2 import Environment, PackageLoader, StrictUndefined

from handpicked.config import Digest
from handpicked.ingest import IngestStats
from handpicked.picks import Candidate
from handpicked.pipeline import UserPicks
from handpicked.suggestions import Requested
from handpicked.users import User

PORTS = {"ssl": 465, "starttls": 587, "none": 25}
TIMEOUT = 30


class DigestError(Exception):
    pass


@dataclass
class Entry:
    result: UserPicks
    requested: list[Requested] | None  # None when nothing was requested


@dataclass
class Report:
    dry_run: bool
    stopped: str | None = None  # why the run stopped early
    changes: Counter[str] | None = None  # None on dry runs
    ingest: IngestStats | None = None
    included: list[User] = field(default_factory=list)
    excluded: list[User] = field(default_factory=list)
    entries: list[Entry] = field(default_factory=list)

    @property
    def pick_count(self) -> int:
        return sum(len(e.result.picks.picks) for e in self.entries if e.result.picks)

    @property
    def checked(self) -> int:
        return sum(e.result.picks.checked for e in self.entries if e.result.picks)

    @property
    def rated(self) -> int:
        return sum(e.result.picks.rated for e in self.entries if e.result.picks)


def because(candidate: Candidate) -> str:
    finished = sorted(
        (s for s in candidate.seeds if s.weight > 0), key=lambda s: -s.weight
    )
    abandoned = [s for s in candidate.seeds if s.weight < 0]
    reason = f"because you finished {_titles([s.title for s in finished])}"
    if abandoned:
        reason += f", despite abandoning {_titles([s.title for s in abandoned])}"
    return reason


def _titles(titles: list[str], limit: int = 3) -> str:
    if len(titles) > limit:
        titles = titles[:limit] + [f"{len(titles) - limit} more"]
    if len(titles) == 1:
        return titles[0]
    return f"{', '.join(titles[:-1])} and {titles[-1]}"


@cache
def _environment(autoescape: bool) -> Environment:
    env = Environment(
        loader=PackageLoader("handpicked"),
        autoescape=autoescape,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.globals["because"] = because
    return env


def subject(report: Report) -> str:
    picks, people = report.pick_count, len(report.entries)
    made = (
        f"{picks} {'pick' if picks == 1 else 'picks'}"
        f" for {people} {'person' if people == 1 else 'people'}"
    )
    if not report.stopped:
        text = f"Handpicked: {made}"
    elif picks:
        text = f"Handpicked: the run stopped after {made}"
    else:
        text = "Handpicked: the run stopped"
    return f"[DRY RUN] {text}" if report.dry_run else text


def render(report: Report, settings: Digest) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject(report)
    message["From"] = settings.sender
    message["To"] = ", ".join(settings.recipients)
    message["Date"] = formatdate(localtime=True)
    # Without a sender domain, leave it to the mail server: make_msgid's
    # fallback looks up this host's name, which can stall in a container.
    _, at, domain = parseaddr(settings.sender)[1].rpartition("@")
    if at and domain:
        message["Message-ID"] = make_msgid(domain=domain)
    text = _environment(autoescape=False).get_template("digest.txt.j2")
    html = _environment(autoescape=True).get_template("digest.html.j2")
    message.set_content(text.render(r=report))
    message.add_alternative(html.render(r=report), subtype="html")
    return message


def send(settings: Digest, message: EmailMessage) -> None:
    context = ssl.create_default_context()
    port = settings.port or PORTS[settings.security]
    try:
        if settings.security == "ssl":
            smtp = smtplib.SMTP_SSL(
                settings.host, port, timeout=TIMEOUT, context=context
            )
        else:
            smtp = smtplib.SMTP(settings.host, port, timeout=TIMEOUT)
        try:
            if settings.security == "starttls":
                smtp.starttls(context=context)
            if settings.username:
                _login(smtp, settings)
            smtp.send_message(message)
            # The message is sent, so an error reply to QUIT is not a failure.
            with suppress(smtplib.SMTPException, OSError):
                smtp.quit()
        finally:
            smtp.close()
    except (smtplib.SMTPException, OSError) as e:
        raise DigestError(f"{settings.host}:{port}: {e}") from e


def _login(smtp: smtplib.SMTP, settings: Digest) -> None:
    try:
        smtp.login(settings.username or "", settings.password or "")
    except UnicodeEncodeError:
        # The error quotes part of the password, so leave it out.
        raise smtplib.SMTPException(
            "SMTP login needs an ASCII username and password"
        ) from None

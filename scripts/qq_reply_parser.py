"""Deterministic, offline acceptance and text extraction for QQ task replies.

The receiving layer must authenticate the account and bind its evidence to the
exact raw bytes. Email headers alone never establish that evidence. The caller
must persist accepted incoming Message-IDs before scheduling a Codex turn.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from email import policy
from email.errors import InvalidHeaderDefect
from email.message import EmailMessage
from email.parser import BytesParser
from html.parser import HTMLParser
from typing import Collection, Mapping


from qq_mail_config import ACCOUNT
MAX_MESSAGE_BYTES = 5_000_000
MAX_BODY_CHARS = 20_000
_ATOM = r"[A-Za-z0-9!#$%&'*+\-/=?^_`{|}~]+"
_DOT_ATOM = rf"{_ATOM}(?:\.{_ATOM})*"
_MESSAGE_ID = re.compile(rf"<({_DOT_ATOM})@({_DOT_ATOM})>", re.ASCII)
_REPLY_SEPARATOR = re.compile(
    r"^\s*[-_]{2,}\s*(?:原始邮件|原邮件|Original Message|Forwarded Message)\s*[-_]{2,}\s*$",
    re.IGNORECASE,
)
_WRITES = re.compile(r"^\s*(?:On\s+.{1,1200}\bwrote\s*[:：]|在.{1,1200}(?:写道|寫道)\s*[:：])\s*$", re.IGNORECASE)
_SIGNATURE = re.compile(
    r"^\s*(?:发自我的\s*(?:iPhone|iPad|Android)|发送自\s*(?:手机)?QQ邮箱|"
    r"发自\s*(?:手机)?QQ邮箱|Sent from my (?:iPhone|iPad|Android)).*$",
    re.IGNORECASE,
)
_FROM_LINE = re.compile(r"^\s*(?:发件人|寄件者|From)\s*[:：]", re.IGNORECASE)
_QUOTE_HEADER = re.compile(r"^\s*(?:发送时间|寄件日期|日期|收件人|收件者|主题|主旨|Sent|Date|To|Subject)\s*[:：]", re.IGNORECASE)
_AUTO_SUBJECT = re.compile(r"^\s*(?:自动回复|自动答复|Auto(?:matic)?[ -]?reply|Out of office)\s*[:：]", re.IGNORECASE)
_QQ_DISPLAY_NAME_DEFECTS = {"encoded word inside quoted string", "missing trailing whitespace after encoded-word"}


class ReplyRejected(ValueError):
    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class OutboundTarget:
    codex_thread_id: str
    mail_thread_id: str


@dataclass(frozen=True)
class VerifiedIdentity:
    source: str
    account: str
    verified: bool
    message_sha256: str


@dataclass(frozen=True)
class ParsedReply:
    message_id: str
    matched_outbound_message_id: str
    codex_thread_id: str
    mail_thread_id: str
    body: str
    identity_source: str


def normalize_message_id(value: str) -> str:
    """Accept the unambiguous modern dot-atom Message-ID form used by QQ."""
    if not isinstance(value, str):
        raise ReplyRejected("invalid_message_id", "Message-ID must be text")
    value = value.strip()
    match = _MESSAGE_ID.fullmatch(value)
    if match is None or len(value) > 998:
        raise ReplyRejected("invalid_message_id", "Message-ID is missing or malformed")
    return f"<{match.group(1)}@{match.group(2).lower()}>"


def _qq_display_name_only(message: EmailMessage, name: str, header) -> bool:
    """QQ quotes encoded display names; tolerate only that spelling defect.

    Encoded/ambiguous addr-specs remain ineligible even if the email library
    happens to decode them into the expected account address.
    """
    if name.lower() not in {"from", "to"}:
        return False
    defects = getattr(header, "defects", ())
    if not defects or any(type(defect) is not InvalidHeaderDefect or str(defect) not in _QQ_DISPLAY_NAME_DEFECTS for defect in defects):
        return False
    addresses = getattr(header, "addresses", ())
    raw = next((value for key, value in message.raw_items() if key.lower() == name.lower()), "")
    raw_addresses = re.findall(r"<([^<>]*)>", raw)
    return bool(addresses) and len(raw_addresses) == len(addresses) and all(
        value.strip() == address.addr_spec and "=?" not in value
        for value, address in zip(raw_addresses, addresses)
    )


def _one_header(message: EmailMessage, name: str, *, required: bool = False) -> str:
    values = message.get_all(name, [])
    if len(values) > 1 or (required and len(values) != 1):
        raise ReplyRejected("ambiguous_headers", f"Expected a single {name} header")
    if not values:
        return ""
    if getattr(values[0], "defects", ()) and not _qq_display_name_only(message, name, values[0]):
        raise ReplyRejected("malformed_headers", f"Invalid {name} header")
    return str(values[0])


def _addresses(message: EmailMessage, name: str) -> list[str]:
    _one_header(message, name, required=True)
    header = message[name]
    addresses = getattr(header, "addresses", ())
    if not addresses or (getattr(header, "defects", ()) and not _qq_display_name_only(message, name, header)):
        raise ReplyRejected("invalid_address", f"Invalid {name} address")
    result = []
    for address in addresses:
        if not address.username or not address.domain:
            raise ReplyRejected("invalid_address", f"Invalid {name} address")
        result.append(f"{address.username}@{address.domain.lower()}")
    return result


def _message_ids(value: str) -> list[str]:
    if not value.strip():
        return []
    ids = []
    offset = 0
    for match in _MESSAGE_ID.finditer(value):
        if value[offset:match.start()].strip():
            raise ReplyRejected("invalid_reply_headers", "Reply headers contain malformed message identifiers")
        ids.append(normalize_message_id(match.group()))
        offset = match.end()
    if value[offset:].strip() or not ids or len(ids) > 1000:
        raise ReplyRejected("invalid_reply_headers", "Reply headers contain malformed message identifiers")
    return ids


def _select_body(message: EmailMessage) -> tuple[str, str, str | None] | None:
    if message.get_content_disposition() == "attachment" or message.get_filename():
        return None
    content_type = message.get_content_type()
    if content_type in {"text/plain", "text/html"}:
        payload = message.get_payload(decode=True)
        if not isinstance(payload, bytes) or message.defects:
            raise ReplyRejected("invalid_body", "Cannot decode the email body")
        try:
            text = payload.decode(message.get_content_charset() or "utf-8", errors="strict")
        except (LookupError, UnicodeError):
            raise ReplyRejected("invalid_encoding", "Email body encoding cannot be decoded reliably") from None
        return content_type, text, None
    if message.get_content_maintype() != "multipart":
        return None  # This also excludes forwarded message/rfc822 attachments.
    choices = [choice for part in message.iter_parts() if (choice := _select_body(part)) is not None]
    if not choices:
        return None
    if content_type == "multipart/alternative":
        preferred = [choice for choice in choices if choice[0] == "text/plain"]
        if len(preferred) > 1 or (not preferred and len(choices) > 1):
            raise ReplyRejected("ambiguous_body", "The message contains multiple possible instruction bodies")
        if preferred:
            html = [choice for choice in choices if choice[0] == "text/html"]
            return preferred[0][0], preferred[0][1], html[0][1] if len(html) == 1 else None
        return choices[0]
    if len(choices) != 1:
        raise ReplyRejected("ambiguous_body", "The message contains multiple possible instruction bodies")
    return choices[0]


class _ReplyHTMLParser(HTMLParser):
    """Render simple message HTML, stopping at known quotation containers."""

    _BLOCKS = {"div", "p", "br", "li", "ul", "ol", "pre", "hr"}
    _SAFE_TAGS = _BLOCKS | {"html", "body", "span", "b", "strong", "i", "em", "u", "s", "a", "code", "font", "wbr"}
    _IGNORED = {"head", "style", "script", "template", "svg"}
    _QUOTE_NAMES = {"qqmail_quote", "gmail_quote", "yahoo_quoted", "moz-cite-prefix", "divrplyfwdmsg"}
    _SAFE_STYLES = {"font-family", "font-weight", "font-style", "text-align", "text-decoration", "white-space", "word-break", "overflow-wrap", "word-wrap"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self.ignored_depth = 0
        self.stop = False
        self.ambiguous = False
        self.links: list[str | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.stop:
            return
        attributes = {key.lower(): value or "" for key, value in attrs}
        if tag == "style" or (tag == "link" and "stylesheet" in attributes.get("rel", "").lower().split()):
            # Computing stylesheet selectors/cascades is outside this parser.
            self.ambiguous = True
        if self.ignored_depth:
            if tag not in {"br", "hr", "img", "meta", "link", "input", "wbr"}:
                self.ignored_depth += 1
            return
        names = set((attributes.get("class", "") + " " + attributes.get("id", "")).lower().split())
        if tag in {"blockquote", "xm-signature"} or names & self._QUOTE_NAMES:
            self.stop = True
            return
        if any(re.search(r"quote|reply|forward|original", name) for name in names):
            self.ambiguous = True
        style = re.sub(r"\s+", "", attributes.get("style", "").lower())
        declarations = [item.split(":", 1) for item in style.split(";") if item]
        hidden_style = any(
            len(item) == 2
            and (
                (item[0] == "display" and item[1] in {"none", "none!important"})
                or (item[0] == "visibility" and item[1] in {"hidden", "hidden!important", "collapse", "collapse!important"})
            )
            for item in declarations
        )
        if tag in self._IGNORED or "hidden" in attributes or attributes.get("aria-hidden") == "true" or hidden_style:
            if tag not in {"br", "hr", "img", "meta", "link", "input", "wbr"}:
                self.ignored_depth = 1
            return
        for declaration in declarations:
            if len(declaration) != 2:
                self.ambiguous = True
                continue
            name, value = declaration
            if name == "font-size":
                size = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(?:px|pt|em|rem|%)", value)
                if size is None or float(size.group(1)) <= 0:
                    self.ambiguous = True
            elif name not in self._SAFE_STYLES or any(character in value for character in "\\{}") or "var(" in value:
                self.ambiguous = True
        if any(attribute in attributes for attribute in {"color", "bgcolor", "background", "size"}):
            self.ambiguous = True
        if tag == "img":
            # A small reply cannot safely communicate image-only instructions.
            if attributes.get("alt", "").strip():
                self.ambiguous = True
            return
        if tag not in self._SAFE_TAGS:
            self.ambiguous = True
        if tag in self._BLOCKS:
            self.chunks.append("\n")
        if tag == "a":
            href = attributes.get("href", "")
            if href and not re.match(r"^(?:https?://|mailto:)", href, re.IGNORECASE):
                self.ambiguous = True
            self.links.append(href or None)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in {"br", "hr", "img", "meta", "link", "input", "wbr"}:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self.stop:
            return
        if self.ignored_depth:
            self.ignored_depth -= 1
            return
        if tag == "a" and self.links:
            href = self.links.pop()
            if href:
                self.chunks.append(f" ({href})")
        if tag in self._BLOCKS:
            self.chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.stop and not self.ignored_depth:
            self.chunks.append(data)


def _html_to_text(body: str) -> str:
    parser = _ReplyHTMLParser()
    try:
        parser.feed(body)
        parser.close()
    except (ValueError, AssertionError):
        raise ReplyRejected("ambiguous_html", "HTML cannot be separated reliably") from None
    if parser.ambiguous or parser.ignored_depth or parser.links:
        raise ReplyRejected("ambiguous_html", "HTML contains unsupported or ambiguous reply structure")
    return "".join(parser.chunks)


def _reply_boundary(lines: list[str]) -> int:
    """Use the same quote/signature boundary for plain text and HTML evidence."""
    for index, line in enumerate(lines):
        if line == "-- " or _SIGNATURE.match(line) or _REPLY_SEPARATOR.match(line) or line.strip().startswith(">"):
            return index
        if any(_WRITES.match(" ".join(lines[index:index + size])) for size in (1, 2, 3)):
            return index
        if _FROM_LINE.match(line) and sum(bool(_QUOTE_HEADER.match(next_line)) for next_line in lines[index + 1:index + 9]) >= 2:
            return index
    return len(lines)


class _QQSignatureParser(HTMLParser):
    """Collect an explicit QQ signature only before any quoted old message."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.signatures: list[str] = []
        self.current: list[str] | None = None
        self.prefix: list[str] = []
        self.stopped = False
        self.ambiguous = False

    def handle_starttag(self, tag, attrs):
        if self.stopped:
            return
        attributes = dict(attrs)
        names = set(((attributes.get("class") or "") + " " + (attributes.get("id") or "")).lower().split())
        if tag == "blockquote" or names & _ReplyHTMLParser._QUOTE_NAMES:
            self.stopped = True
            return
        if tag == "xm-signature":
            prefix_lines = "".join(self.prefix).replace("\r\n", "\n").replace("\r", "\n").split("\n")
            if _reply_boundary(prefix_lines) < len(prefix_lines):
                self.stopped = True
                return
            if self.current is not None:
                self.ambiguous = True
            self.current = []
        elif self.current is not None and tag in {"style", "script", "template"}:
            self.ambiguous = True
        elif self.current is None and tag in _ReplyHTMLParser._BLOCKS | {"table", "tbody", "tr", "td"}:
            self.prefix.append("\n")

    def handle_endtag(self, tag):
        if not self.stopped and tag == "xm-signature" and self.current is not None:
            self.signatures.append("".join(self.current))
            self.current = None
        elif not self.stopped and self.current is None and tag in _ReplyHTMLParser._BLOCKS | {"table", "tbody", "tr", "td"}:
            self.prefix.append("\n")

    def handle_data(self, data):
        if self.stopped:
            return
        if self.current is not None:
            self.current.append(data)
        else:
            self.prefix.append(data)


def _qq_signature(html: str | None) -> str | None:
    if html is None:
        return None
    parser = _QQSignatureParser()
    try:
        parser.feed(html)
        parser.close()
    except (ValueError, AssertionError):
        raise ReplyRejected("ambiguous_signature", "QQ signature structure cannot be parsed reliably") from None
    if parser.ambiguous or parser.current is not None or len(parser.signatures) > 1:
        raise ReplyRejected("ambiguous_signature", "QQ signature structure is ambiguous")
    return parser.signatures[0] if parser.signatures else None


def _strip_matching_signature(text: str, signature: str | None) -> str:
    if signature is None:
        return text
    signature_chars = [character for character in signature if not character.isspace()]
    if not signature_chars:
        return text
    end = len(text)
    for character in reversed(signature_chars):
        while end and text[end - 1].isspace():
            end -= 1
        if not end or text[end - 1] != character:
            raise ReplyRejected("ambiguous_signature", "HTML signature does not match the plain-text suffix")
        end -= 1
    return text[:end].rstrip()


def _new_text(body: str, signature: str | None = None) -> str:
    body = body.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    if "\x00" in body:
        raise ReplyRejected("invalid_body", "Email contains a NUL character")
    lines = body.split("\n")
    end = _reply_boundary(lines)
    text = "\n".join(lines[:end]).strip()
    text = _strip_matching_signature(text, signature)
    if not text:
        raise ReplyRejected("empty_reply", "No new text appears before the quoted email or signature")
    if len(text) > MAX_BODY_CHARS:
        raise ReplyRejected("body_too_long", "New reply text exceeds 20000 characters")
    return text


def parse_reply(
    raw_message: bytes,
    known_outbound: Mapping[str, OutboundTarget],
    *,
    identity: VerifiedIdentity | None,
    seen_message_ids: Collection[str] = (),
    sender_aliases: Collection[str] = (),
) -> ParsedReply:
    if not isinstance(raw_message, bytes) or not raw_message or len(raw_message) > MAX_MESSAGE_BYTES:
        raise ReplyRejected("invalid_message", "Email is empty or exceeds the supported size")
    if (
        not isinstance(identity, VerifiedIdentity)
        or identity.verified is not True
        or identity.account != ACCOUNT
        or identity.source not in {"sent_folder", "trusted_server_auth"}
        or identity.message_sha256 != hashlib.sha256(raw_message).hexdigest()
    ):
        raise ReplyRejected("identity_not_verified", "No trusted evidence binds this email to the user's QQ account")
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw_message)
    except (ValueError, TypeError):
        raise ReplyRejected("invalid_message", "Email cannot be parsed") from None
    if message.defects:
        raise ReplyRejected("invalid_message", "Email has malformed MIME structure")
    senders = _addresses(message, "From")
    if len(senders) != 1 or senders[0] not in {ACCOUNT, *sender_aliases} or ACCOUNT not in _addresses(message, "To"):
        raise ReplyRejected("wrong_account", "Reply must be from and addressed to the user's QQ mailbox")
    message_id = normalize_message_id(_one_header(message, "Message-ID", required=True))
    outbound = {normalize_message_id(key): value for key, value in known_outbound.items()}
    if message_id in outbound:
        raise ReplyRejected("outbound_message", "This is an existing service notification, not a user reply")
    if message_id in {normalize_message_id(value) for value in seen_message_ids}:
        raise ReplyRejected("duplicate_message", "This incoming Message-ID has already been processed")
    auto_submitted = _one_header(message, "Auto-Submitted").split(";", 1)[0].strip().lower()
    precedence = _one_header(message, "Precedence").strip().lower()
    if (
        auto_submitted not in {"", "no"}
        or precedence in {"bulk", "list", "junk"}
        or _one_header(message, "X-Autoreply")
        or _one_header(message, "X-Autorespond")
        or _one_header(message, "Return-Path").strip() == "<>"
        or message.get_content_type() == "multipart/report"
        or _AUTO_SUBJECT.match(_one_header(message, "Subject"))
    ):
        raise ReplyRejected("automatic_message", "Automatic replies and delivery reports cannot execute tasks")
    direct_ids = _message_ids(_one_header(message, "In-Reply-To"))
    reference_ids = _message_ids(_one_header(message, "References"))
    matched = [item for item in reference_ids + direct_ids if item in outbound]
    if not matched:
        raise ReplyRejected("not_a_known_reply", "Reply headers do not reference any registered notification")
    targets = {(outbound[item].codex_thread_id, outbound[item].mail_thread_id) for item in matched}
    if len(targets) != 1:
        raise ReplyRejected("ambiguous_task", "Reply headers refer to more than one Codex task")
    selected = _select_body(message)
    if selected is None:
        raise ReplyRejected("missing_body", "No supported text body was found; attachments are not instructions")
    content_type, body, html_alternative = selected
    text = _new_text(_html_to_text(body) if content_type == "text/html" else body, _qq_signature(html_alternative))
    target = outbound[matched[-1]]
    return ParsedReply(message_id, matched[-1], target.codex_thread_id, target.mail_thread_id, text, identity.source)

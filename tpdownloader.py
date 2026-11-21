#!/usr/bin/env python3
"""Download a file from a website, email it, wait for a reply, open the link in the reply, trash the reply.

Credentials (TPDOWNLOADER_SITE_USERNAME, TPDOWNLOADER_SITE_PASSWORD, TPDOWNLOADER_GMAIL_ADDRESS,
TPDOWNLOADER_GMAIL_APP_PASSWORD) and the recipient (TPDOWNLOADER_SEND_TO) are looked up in .env next to this script, then in environment
variables, then in the macOS Keychain:
    .venv/bin/python -m keyring set tpdownloader TPDOWNLOADER_SITE_PASSWORD
Everything else is in config.toml.
"""

import argparse
import email
import email.policy
import html
import http.cookiejar
import imaplib
import logging
import mimetypes
import os
import re
import smtplib
import sys
import time
import tomllib
from datetime import datetime
from email.message import EmailMessage, Message
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import keyring
import requests
from bs4 import BeautifulSoup
from dotenv import dotenv_values

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.toml"
ENV_PATH = BASE_DIR / ".env"
STATE_DIR = BASE_DIR / "state"
COOKIES = STATE_DIR / "cookies.txt"
LAST_PAGE = STATE_DIR / "last_page.html"
LAST_CONFIRMATION = STATE_DIR / "last_confirmation.html"
# One line per emailed file, tab-separated: file name, sent at, inbox UIDNEXT just before sending,
# confirmed at ("-" = unknown / not yet). A file listed here isn't emailed again; if it isn't
# confirmed yet, the next run only looks for its confirmation reply.
SENT_LOG = STATE_DIR / "sent.txt"
DOWNLOAD_DIR = BASE_DIR / "downloads"
# Secrets and private settings, kept out of config.toml because the repository is public.
CREDENTIALS = ("TPDOWNLOADER_SITE_USERNAME", "TPDOWNLOADER_SITE_PASSWORD", "TPDOWNLOADER_GMAIL_ADDRESS",
               "TPDOWNLOADER_GMAIL_APP_PASSWORD", "TPDOWNLOADER_SEND_TO")
KEYCHAIN_SERVICE = "tpdownloader"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")

# Page structure — adjust to the site's and the reply email's HTML.
# Page with the download link. When logged out, it should show (or redirect to) the login form.
PAGE_URL = "https://www.tygodnikpowszechny.pl/moje-konto"
# Regex matched against each link's href and visible text; the first match is downloaded.
# The account page lists issues newest first, each as pdf/epub/mobi links like
# /sites/default/files/publication/2026-11/tp47-2026-f.epub, so the first EPUB is the latest issue.
DOWNLOAD_LINK_PATTERN = r"/publication/.+\.epub$"
# The site's login form (Drupal). When logged out, PAGE_URL redirects to /user/login, which shows it.
# Matched by id rather than "any password input", so a password-change form on the logged-in
# account page isn't mistaken for a login form.
LOGIN_FORM_SELECTOR = "form#user-login-form"
# Name of the username input in the login form; None = first text/email input in that form.
USERNAME_FIELD = "name"
# Regex picking the link to open out of the reply email. Amazon's "Verify Request" button is a
# tracking link (https://www.amazon.com/gp/f.html?...&U=<encoded target>...) whose target is
# https://www.amazon.com/sendtokindle/verification/confirm/...; match that target, encoded or not.
REPLY_LINK_PATTERN = r"^https://www\.amazon\.com/.*sendtokindle(/|%2F)verification(/|%2F)confirm"
# Text on the page the link opens when verification failed (e.g. "That link isn't working! We were
# unable to verify your document request. Verification links expire after 48 hours and may only be
# used once."). Then the reply isn't trashed and the file isn't marked confirmed.
CONFIRM_FAILED_PATTERN = r"unable to verify|link isn.t working"

log = logging.getLogger("tpdownloader")


def load_credentials() -> dict[str, str]:
    dotenv = dotenv_values(ENV_PATH)
    creds = {}
    for name in CREDENTIALS:
        creds[name] = (dotenv.get(name) or os.environ.get(name)
                      or keyring.get_password(KEYCHAIN_SERVICE, name))
    missing = [key for key, value in creds.items() if not value]
    if missing:
        sys.exit(f"Missing credentials: {', '.join(missing)}. Put them in {ENV_PATH}, "
                 f"the environment, or the Keychain (keyring set {KEYCHAIN_SERVICE} <NAME>)")
    return creds


# --- Steps 1-3: website ---------------------------------------------------

def fail(message: str, page: requests.Response) -> None:
    LAST_PAGE.write_text(page.text)
    sys.exit(f"{message} (page {page.url} saved to {LAST_PAGE})")


def new_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    session.cookies = http.cookiejar.LWPCookieJar(COOKIES)
    if COOKIES.exists():
        session.cookies.load(ignore_discard=True)
    return session


def get_page(session: requests.Session, url: str) -> tuple[requests.Response, BeautifulSoup]:
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return response, BeautifulSoup(response.text, "html.parser")


def find_login_form(soup: BeautifulSoup):
    return soup.select_one(LOGIN_FORM_SELECTOR)


def log_in(session: requests.Session, page: requests.Response, form, username: str, password: str) -> None:
    # Start from the form's own fields, so hidden ones (CSRF tokens etc.) are sent back.
    fields = {}
    submit_sent = False
    for field in form.find_all("input"):
        name, kind = field.get("name"), field.get("type", "text").lower()
        if not name or kind in ("button", "image", "reset"):
            continue
        if kind == "submit":
            # Like a browser, send the (first) clicked button; Drupal reads it as "op".
            if not submit_sent:
                fields[name] = field.get("value", "")
                submit_sent = True
            continue
        if kind in ("checkbox", "radio") and not field.has_attr("checked"):
            continue
        fields[name] = field.get("value", "")

    username_field = USERNAME_FIELD or next(
        f["name"] for f in form.find_all("input")
        if f.get("name") and f.get("type", "text").lower() in ("text", "email"))
    fields[username_field] = username
    fields[form.find("input", attrs={"type": "password"})["name"]] = password

    action = urljoin(page.url, form.get("action") or page.url)
    response = session.post(action, data=fields, headers={"Referer": page.url}, timeout=60)
    response.raise_for_status()


def find_download_url(soup: BeautifulSoup, base_url: str) -> str | None:
    for link in soup.find_all("a", href=True):
        if (re.search(DOWNLOAD_LINK_PATTERN, link["href"])
                or re.search(DOWNLOAD_LINK_PATTERN, link.get_text(" ", strip=True))):
            return urljoin(base_url, link["href"])
    return None


def filename_of(response: requests.Response) -> str:
    disposition = Message()
    disposition["Content-Disposition"] = response.headers.get("Content-Disposition", "")
    name = disposition.get_filename() or urlparse(response.url).path
    # Path(...).name keeps a server-supplied name from escaping DOWNLOAD_DIR.
    return Path(name).name or "download"


def download_file(page_url: str, username: str, password: str) -> Path:
    STATE_DIR.mkdir(exist_ok=True)
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    session = new_session()

    page, soup = get_page(session, page_url)
    form = find_login_form(soup)
    if form:
        log.info("Not logged in, logging in as %s", username)
        log_in(session, page, form, username, password)
        page, soup = get_page(session, page_url)
        if find_login_form(soup):
            fail("Login failed, still seeing a login form", page)
        session.cookies.save(ignore_discard=True)
    else:
        log.info("Already logged in")

    download_url = find_download_url(soup, page.url)
    if not download_url:
        fail(f"No link matching {DOWNLOAD_LINK_PATTERN!r}", page)

    response = session.get(download_url, timeout=300)
    response.raise_for_status()
    if response.headers.get("Content-Type", "").startswith("text/html"):
        fail("Download link returned an HTML page instead of a file", response)
    target = DOWNLOAD_DIR / filename_of(response)
    target.write_bytes(response.content)
    session.cookies.save(ignore_discard=True)
    log.info("Downloaded %s (%d bytes)", target, len(response.content))
    return target


# --- Step 4: send ---------------------------------------------------------

def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_sent() -> dict[str, dict]:
    records = {}
    if SENT_LOG.exists():
        for line in SENT_LOG.read_text().splitlines():
            if not line.strip():
                continue
            # Older lines have only the first two columns.
            name, sent_at, uid, confirmed_at = (line.split("\t") + ["-"] * 3)[:4]
            records[name] = {"sent_at": sent_at,
                             "inbox_uid": None if uid == "-" else int(uid),
                             "confirmed_at": None if confirmed_at == "-" else confirmed_at}
    return records


def save_sent(records: dict[str, dict]) -> None:
    def col(value) -> str:
        return "-" if value is None else str(value)
    SENT_LOG.write_text("".join(
        f"{name}\t{r['sent_at']}\t{col(r['inbox_uid'])}\t{col(r['confirmed_at'])}\n"
        for name, r in records.items()))


def send_file(mail: dict, address: str, password: str, send_to: str, path: Path) -> None:
    msg = EmailMessage()
    msg["From"] = address
    msg["To"] = send_to
    msg["Subject"] = mail.get("send_subject") or path.name
    msg.set_content(mail.get("send_body", ""))
    ctype, _ = mimetypes.guess_type(path)
    maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
    msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(address, password)
        smtp.send_message(msg)
    log.info("Sent %s to %s", path.name, send_to)


# --- Steps 5-8: wait for reply, open link, trash --------------------------

def imap_connect(address: str, password: str) -> imaplib.IMAP4_SSL:
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(address, password)
    return imap


def inbox_uidnext(imap: imaplib.IMAP4_SSL) -> int:
    _, data = imap.status("INBOX", "(UIDNEXT)")
    return int(re.search(rb"UIDNEXT (\d+)", data[0]).group(1))


def quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


IMAP_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def wait_for_reply(imap: imaplib.IMAP4_SSL, mail: dict, record: dict) -> bytes:
    """Poll INBOX for a matching message that arrived after the file was sent; return its UID.

    A reply that came while the script wasn't running is found on the first check.
    """
    since_uid = record["inbox_uid"]
    if since_uid is not None:
        criteria = ["UID", f"{since_uid}:*"]
    else:
        # Records from before the UID was stored: fall back to the send day (IMAP SINCE is per day).
        day = datetime.fromisoformat(record["sent_at"])
        criteria = ["SINCE", f"{day.day}-{IMAP_MONTHS[day.month - 1]}-{day.year}"]
    if mail.get("wait_from"):
        criteria += ["FROM", quote(mail["wait_from"])]
    if mail.get("wait_subject"):
        criteria += ["SUBJECT", quote(mail["wait_subject"])]

    deadline = time.monotonic() + mail.get("wait_timeout_minutes", 30) * 60
    interval = mail.get("poll_interval_seconds", 30)
    log.info("Waiting for reply (%s)", " ".join(criteria))
    while time.monotonic() < deadline:
        imap.noop()
        _, data = imap.uid("SEARCH", *criteria)
        # "n:*" matches the last message even when n is past it, so filter again.
        uids = [u for u in data[0].split() if since_uid is None or int(u) >= since_uid]
        if uids:
            return uids[0]
        time.sleep(interval)
    sys.exit("Timed out waiting for the reply email; the next run will look for it again")


def extract_link(raw: bytes) -> str:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    body = msg.get_body(preferencelist=("html", "plain"))
    content = body.get_content() if body else ""
    candidates = [html.unescape(h) for h in re.findall(r'href=["\']([^"\']+)["\']', content)]
    candidates += re.findall(r"https?://[^\s<>\"']+", content)
    for url in candidates:
        if re.search(REPLY_LINK_PATTERN, url):
            return url
    sys.exit(f"No link matching {REPLY_LINK_PATTERN!r} found in the reply email")


def unwrap_link(url: str) -> str:
    """Return the target of an Amazon click-tracking link (/gp/f.html?...&U=<target>), else url."""
    parsed = urlparse(url)
    if (parsed.netloc == "amazon.com" or parsed.netloc.endswith(".amazon.com")) and parsed.path == "/gp/f.html":
        target = parse_qs(parsed.query).get("U", [None])[0]  # parse_qs already URL-decodes it
        if target and urlparse(target).scheme == "https":
            return target
    return url


def open_link(url: str) -> None:
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=60)
    # Kept (even on errors) so you can check what the page said: success, sign-in, or a block.
    LAST_CONFIRMATION.write_text(response.text)
    log.info("Opened link, HTTP %s, ended at %s (page saved to %s)",
             response.status_code, response.url, LAST_CONFIRMATION)
    response.raise_for_status()
    # Amazon answers a used or expired link with HTTP 200 too, so check what the page says.
    text = " ".join(BeautifulSoup(response.text, "html.parser").get_text(" ").split())
    if re.search(CONFIRM_FAILED_PATTERN, text, re.IGNORECASE):
        sys.exit(f"Confirmation failed, the page says: {text[:300]!r}. "
                 "Run with --force-send to send the file again and get a new confirmation.")


def handle_reply(imap: imaplib.IMAP4_SSL, mail: dict, record: dict) -> None:
    imap.select("INBOX")
    uid = wait_for_reply(imap, mail, record)
    _, data = imap.uid("FETCH", uid, "(BODY.PEEK[])")
    url = extract_link(data[0][1])
    log.info("Found link %s", url)
    target = unwrap_link(url)
    if target != url:
        log.info("Unwrapped tracking link to %s", target)
    open_link(target)
    # Gmail: adding the \Trash label moves the message to Trash, whatever the UI language.
    imap.uid("STORE", uid, "+X-GM-LABELS", "\\Trash")
    log.info("Moved reply to Trash")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--download-only", action="store_true",
                        help="only run the website part (useful while setting up)")
    parser.add_argument("--force-send", action="store_true",
                        help=f"email the file even if it was sent before (recorded in {SENT_LOG.name}), "
                             "then wait for a new confirmation")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with CONFIG_PATH.open("rb") as f:
        config = tomllib.load(f)
    creds = load_credentials()

    path = download_file(PAGE_URL, creds["TPDOWNLOADER_SITE_USERNAME"], creds["TPDOWNLOADER_SITE_PASSWORD"])
    if args.download_only:
        return
    records = load_sent()
    record = records.get(path.name)
    if record and record["confirmed_at"] and not args.force_send:
        log.info("%s was already sent and confirmed (see %s); use --force-send to send it again",
                 path.name, SENT_LOG)
        return

    mail, address, password = config["mail"], creds["TPDOWNLOADER_GMAIL_ADDRESS"], creds["TPDOWNLOADER_GMAIL_APP_PASSWORD"]
    imap = imap_connect(address, password)
    try:
        if record is None or args.force_send:
            # Remember where the inbox ends *before* sending, so a fast reply can't be missed.
            since_uid = inbox_uidnext(imap)
            send_file(mail, address, password, creds["TPDOWNLOADER_SEND_TO"], path)
            # Recorded as soon as the email is out: if the confirmation step fails or is
            # interrupted, the next run doesn't resend, it only looks for the confirmation.
            record = records[path.name] = {"sent_at": now(), "inbox_uid": since_uid, "confirmed_at": None}
            save_sent(records)
        else:
            log.info("%s was sent at %s but not confirmed yet; looking for the confirmation",
                     path.name, record["sent_at"])
        handle_reply(imap, mail, record)
        record["confirmed_at"] = now()
        save_sent(records)
    finally:
        imap.logout()
    log.info("Done")


if __name__ == "__main__":
    main()

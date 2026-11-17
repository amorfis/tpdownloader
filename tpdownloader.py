#!/usr/bin/env python3
"""Download a file from a website, email it, wait for a reply, open the link in the reply, trash the reply.

Credentials (TPDOWNLOADER_SITE_USERNAME, TPDOWNLOADER_SITE_PASSWORD, TPDOWNLOADER_GMAIL_ADDRESS,
TPDOWNLOADER_GMAIL_APP_PASSWORD) are looked up in .env next to this script, then in environment
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
from email.message import EmailMessage, Message
from pathlib import Path
from urllib.parse import urljoin, urlparse

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
DOWNLOAD_DIR = BASE_DIR / "downloads"
CREDENTIALS = ("TPDOWNLOADER_SITE_USERNAME", "TPDOWNLOADER_SITE_PASSWORD", "TPDOWNLOADER_GMAIL_ADDRESS", "TPDOWNLOADER_GMAIL_APP_PASSWORD")
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
# Regex picking the link to open out of the reply email.
REPLY_LINK_PATTERN = r"https://example\.com/confirm"

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

def send_file(mail: dict, address: str, password: str, path: Path) -> None:
    msg = EmailMessage()
    msg["From"] = address
    msg["To"] = mail["send_to"]
    msg["Subject"] = mail.get("send_subject") or path.name
    msg.set_content(mail.get("send_body", ""))
    ctype, _ = mimetypes.guess_type(path)
    maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
    msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(address, password)
        smtp.send_message(msg)
    log.info("Sent %s to %s", path.name, mail["send_to"])


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


def wait_for_reply(imap: imaplib.IMAP4_SSL, mail: dict, since_uid: int) -> bytes:
    """Poll INBOX for a matching message that arrived after since_uid; return its UID."""
    criteria = ["UID", f"{since_uid}:*"]
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
        uids = [u for u in data[0].split() if int(u) >= since_uid]
        if uids:
            return uids[0]
        time.sleep(interval)
    sys.exit("Timed out waiting for the reply email")


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


def open_link(url: str) -> None:
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=60)
    response.raise_for_status()
    log.info("Opened link, HTTP %s", response.status_code)


def handle_reply(imap: imaplib.IMAP4_SSL, mail: dict, since_uid: int) -> None:
    imap.select("INBOX")
    uid = wait_for_reply(imap, mail, since_uid)
    _, data = imap.uid("FETCH", uid, "(BODY.PEEK[])")
    url = extract_link(data[0][1])
    log.info("Found link %s", url)
    open_link(url)
    # Gmail: adding the \Trash label moves the message to Trash, whatever the UI language.
    imap.uid("STORE", uid, "+X-GM-LABELS", "\\Trash")
    log.info("Moved reply to Trash")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--download-only", action="store_true",
                        help="only run the website part (useful while setting up)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with CONFIG_PATH.open("rb") as f:
        config = tomllib.load(f)
    creds = load_credentials()

    path = download_file(PAGE_URL, creds["TPDOWNLOADER_SITE_USERNAME"], creds["TPDOWNLOADER_SITE_PASSWORD"])
    if args.download_only:
        return

    mail, address, password = config["mail"], creds["TPDOWNLOADER_GMAIL_ADDRESS"], creds["TPDOWNLOADER_GMAIL_APP_PASSWORD"]
    imap = imap_connect(address, password)
    try:
        # Remember where the inbox ends *before* sending, so a fast reply can't be missed.
        since_uid = inbox_uidnext(imap)
        send_file(mail, address, password, path)
        handle_reply(imap, mail, since_uid)
    finally:
        imap.logout()
    log.info("Done")


if __name__ == "__main__":
    main()

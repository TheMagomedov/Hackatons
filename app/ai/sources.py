"""Bounded business-source extraction; source text is data, never instructions."""

from __future__ import annotations

import asyncio
import importlib.util
import io
import ipaddress
import multiprocessing
import re
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from html.parser import HTMLParser

import httpx

from app.shared.errors import InvalidInput, ProviderUnavailable
from app.shared.models import SourceInput


@dataclass(frozen=True)
class SourceLimits:
    max_pdf_bytes: int = 8 * 1024 * 1024
    # Corporate pages often embed large SVG/style payloads; extracted text has
    # its own smaller limit below, and streamed HTML remains strictly bounded.
    max_html_bytes: int = 4 * 1024 * 1024
    max_text_chars: int = 20_000
    max_pdf_pages: int = 24
    timeout_seconds: float = 15
    max_redirects: int = 3

    def __post_init__(self) -> None:
        if min(self.max_pdf_bytes, self.max_html_bytes, self.max_text_chars, self.max_pdf_pages) < 1:
            raise ValueError("Source size limits must be positive")
        if not 0 < self.timeout_seconds <= 120 or not 0 <= self.max_redirects <= 10:
            raise ValueError("Source timeout/redirect limit is outside the supported range")


@dataclass(frozen=True)
class ExtractedSource:
    id: str
    text: str


Resolver = Callable[[str, int], Awaitable[list[str]]]


def public_ip(value: str) -> bool:
    """Reject private/special ranges, including IPv6 transition mechanisms."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped or address.sixtofour or address.teredo or "%" in value:
            return False
        if address in ipaddress.ip_network("64:ff9b::/96"):
            return False
    return True


async def _resolve(host: str, port: int) -> list[str]:
    records = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(record[4][0] for record in records))


def _public_url(value: str) -> httpx.URL:
    try:
        # Pasted URLs (including existing stored sources) may have surrounding
        # whitespace. Embedded controls still fail before any network request.
        value = value.strip()
        if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError
        url = httpx.URL(value)
        if (
            url.scheme not in {"http", "https"}
            or not url.host
            or url.userinfo
            or "%" in url.host
            or url.port not in {None, 443 if url.scheme == "https" else 80}
        ):
            raise ValueError
        try:
            ipaddress.ip_address(url.host)
        except ValueError:
            pass
        else:
            if not public_ip(url.host):
                raise ValueError
        return url.copy_with(fragment=None)
    except (ValueError, UnicodeError, httpx.InvalidURL):
        raise InvalidInput("Укажите публичный HTTP(S)-адрес без пароля и нестандартного порта.") from None


class _PageText(HTMLParser):
    hidden = {"script", "style", "noscript", "template", "svg"}
    breaks = {"p", "br", "h1", "h2", "h3", "h4", "div", "li", "section", "article", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skipping: list[str] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self.hidden:
            self.skipping.append(tag)
        if not self.skipping and tag in self.breaks:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.skipping and tag == self.skipping[-1]:
            self.skipping.pop()
        if not self.skipping and tag in self.breaks:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skipping:
            self.parts.append(data)


def _checked_text(value: str, limits: SourceLimits) -> str:
    text = "\n".join(line for part in value.splitlines() if (line := " ".join(part.split())))
    if not text:
        raise InvalidInput("В источнике не найден текст. Пришлите текстовое описание бизнеса.")
    if len(text) > limits.max_text_chars:
        raise InvalidInput("В источнике слишком много текста. Пришлите более короткий материал.")
    return text


def extract_html_text(value: str, limits: SourceLimits = SourceLimits()) -> str:
    parser = _PageText()
    try:
        parser.feed(value)
        parser.close()
    except (ValueError, AssertionError):
        raise InvalidInput("Разметку страницы не удалось прочитать.") from None
    return _checked_text("".join(parser.parts), limits)


async def fetch_public_page(
    value: str,
    *,
    limits: SourceLimits = SourceLimits(),
    resolver: Resolver | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """Fetch one page, validating every redirect and connecting to the checked IP.

    Host and TLS SNI retain the original hostname. A fresh connection pool for
    each hop prevents reuse of a TLS connection belonging to another hostname.
    The optional transport/resolver are trusted test dependencies, never input
    taken from a business source. Environment proxies and cookies are disabled.
    """
    resolve = resolver or _resolve
    try:
        async with asyncio.timeout(limits.timeout_seconds):
            url = _public_url(value)
            for hop in range(limits.max_redirects + 1):
                try:
                    ipaddress.ip_address(url.host)
                except ValueError:
                    addresses = await resolve(url.host, url.port or (443 if url.scheme == "https" else 80))
                else:
                    addresses = [url.host]
                if not addresses or any(not public_ip(address) for address in addresses):
                    raise InvalidInput("Адрес страницы ведёт в закрытую или служебную сеть.")
                # Connecting to a literal numeric IP avoids a second DNS lookup.
                pinned_url = url.copy_with(host=addresses[0])
                headers = {
                    "Host": url.netloc.decode("ascii"),
                    "Accept": "text/html, application/xhtml+xml",
                    "Accept-Encoding": "identity",
                    "User-Agent": "HackatonsBusinessSource/1.0",
                }
                async with httpx.AsyncClient(
                    transport=transport, trust_env=False, follow_redirects=False,
                    timeout=limits.timeout_seconds,
                ) as client:
                    async with client.stream(
                        "GET", pinned_url, headers=headers,
                        extensions={"sni_hostname": url.raw_host.decode("ascii")},
                    ) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            if hop == limits.max_redirects or not response.headers.get("location"):
                                raise InvalidInput("Слишком много перенаправлений или отсутствует адрес страницы.")
                            url = _public_url(str(url.join(response.headers["location"])))
                            continue
                        if response.status_code != 200:
                            raise ProviderUnavailable("Не удалось получить страницу сайта. Проверьте адрес.")
                        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                        if content_type not in {"text/html", "application/xhtml+xml"}:
                            raise InvalidInput("По указанному адресу ожидается HTML-страница.")
                        # Reject compressed responses rather than expose a decompression bomb.
                        if response.headers.get("content-encoding", "identity").lower() != "identity":
                            raise InvalidInput("Сайт не поддержал безопасную загрузку несжатой страницы.")
                        try:
                            declared_size = int(response.headers.get("content-length", "0"))
                        except ValueError:
                            raise InvalidInput("Сайт вернул неверный размер страницы.") from None
                        if declared_size < 0 or declared_size > limits.max_html_bytes:
                            raise InvalidInput("Страница превышает допустимый размер.")
                        data = bytearray()
                        async for chunk in response.aiter_bytes(chunk_size=16_384):
                            data.extend(chunk)
                            if len(data) > limits.max_html_bytes:
                                raise InvalidInput("Страница превышает допустимый размер.")
                        try:
                            html = data.decode(response.charset_encoding or "utf-8", errors="replace")
                        except LookupError:
                            html = data.decode("utf-8", errors="replace")
                return extract_html_text(html, limits)
    except (httpx.HTTPError, OSError, TimeoutError):
        raise ProviderUnavailable("Не удалось загрузить сайт за отведённое время. Попробуйте позже.") from None
    except (httpx.InvalidURL, UnicodeError, ValueError):
        raise InvalidInput("Сайт вернул некорректный адрес перенаправления.") from None
    raise InvalidInput("Не удалось найти конечную страницу сайта.")


def extract_pdf_text(data: bytes, limits: SourceLimits = SourceLimits()) -> str:
    """Synchronous parser. Async callers use an isolated, time-limited process."""
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ProviderUnavailable("Для чтения PDF установите зависимость pypdf из инструкции AI-модуля.") from None
    if not data.startswith(b"%PDF-") or len(data) > limits.max_pdf_bytes:
        raise InvalidInput("Нужен PDF-файл допустимого размера с текстовым слоем.")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise InvalidInput("Пришлите PDF без шифрования и пароля.")
        if len(reader.pages) > limits.max_pdf_pages:
            raise InvalidInput("В PDF слишком много страниц. Пришлите более короткий документ.")
        parts: list[str] = []
        count = 0
        for page in reader.pages:
            part = re.sub(r"[ \t]+", " ", page.extract_text() or "").strip()
            count += len(part) + 1
            if count > limits.max_text_chars + 1:
                raise InvalidInput("В PDF слишком много текста. Пришлите более короткий документ.")
            parts.append(part)
        if not any(parts):
            raise InvalidInput("В PDF нет текстового слоя. OCR не поддерживается; пришлите текстовый PDF.")
        return _checked_text("\n".join(parts), limits)
    except InvalidInput:
        raise
    except Exception:
        raise InvalidInput("PDF повреждён или его текстовый слой не удалось прочитать.") from None


def _pdf_worker(connection, data: bytes, limits: SourceLimits) -> None:
    try:
        connection.send((True, extract_pdf_text(data, limits)))
    except (InvalidInput, ProviderUnavailable) as error:
        connection.send((False, str(error)))
    finally:
        connection.close()


def _pdf_in_process(data: bytes, limits: SourceLimits) -> str:
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_pdf_worker, args=(child, data, limits), daemon=True)
    try:
        process.start()
        child.close()
        if not parent.poll(limits.timeout_seconds):
            raise InvalidInput("Разбор PDF занял слишком много времени. Пришлите более простой документ.")
        success, value = parent.recv()
        if not success:
            raise InvalidInput(value)
        return value
    except (EOFError, OSError):
        raise InvalidInput("Не удалось безопасно прочитать PDF.") from None
    finally:
        parent.close()
        child.close()
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=1)
            if process.is_alive():
                process.kill()
                process.join(timeout=1)
            process.close()


async def extract_source(
    source: SourceInput,
    *,
    limits: SourceLimits = SourceLimits(),
    resolver: Resolver | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ExtractedSource:
    if source.source.kind == "text":
        text = _checked_text(source.source.text or "", limits)
    elif source.source.kind == "url":
        text = await fetch_public_page(
            source.source.url or "", limits=limits, resolver=resolver, transport=transport,
        )
    else:
        data = source.content
        if not data or not data.startswith(b"%PDF-") or len(data) > limits.max_pdf_bytes:
            raise InvalidInput("Не получен PDF допустимого размера. Передайте содержимое файла.")
        if importlib.util.find_spec("pypdf") is None:
            raise ProviderUnavailable("Для чтения PDF установите зависимость pypdf из инструкции AI-модуля.")
        # The worker process is killed on its own deadline, even if this coroutine
        # is cancelled. A plain to_thread(PdfReader) could keep consuming CPU forever.
        text = await asyncio.to_thread(_pdf_in_process, data, limits)
    return ExtractedSource(id=source.source.id, text=text)
